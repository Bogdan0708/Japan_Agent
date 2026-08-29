from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from japan_agent.models import ResearchDecision
from japan_agent.risk import RiskRejected
from japan_agent.storage import Database
from japan_agent.workflow import ProposalWorkflow

from .helpers import NOW, decision, portfolio, settings, snapshot


def cited_decision(*evidence: str, run_id: str | None = "run-1") -> ResearchDecision:
    base = decision()
    return ResearchDecision(
        thesis=base.thesis,
        evidence=tuple(evidence),
        action=base.action,
        instrument=base.instrument,
        target_weight=base.target_weight,
        confidence=base.confidence,
        invalidation_condition=base.invalidation_condition,
        research_run_id=run_id,
    )


class CitationValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "config").mkdir()
        (root / "config" / "whitelist.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "verified_at": "2026-08-28T00:00:00Z",
                    "account_environment": "demo",
                    "instruments": [
                        {
                            "ticker": "TEST_EQ",
                            "display_name": "Test Japan Technology ETF",
                            "sleeve": "CORE",
                            "instrument_type": "ETF",
                            "native_currency": "USD",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.settings = settings(root)
        self.settings.ensure_directories()
        self.database = Database(self.settings.database_path)
        self.database.initialize()
        self.database.save_snapshot(snapshot(), raw={})
        self.database.save_research_item(
            source="EDINET",
            external_id="DOC123",
            headline="Test filing",
            published_at=NOW - timedelta(hours=2),
            retrieved_at=NOW - timedelta(hours=1),
            payload={},
        )
        # An old item that EXISTS in the database but was outside the model's
        # snapshot window — the audit's probe case. It must not be citable.
        self.database.save_research_item(
            source="EDINET",
            external_id="OLD200",
            headline="Two-hundred-day-old filing",
            published_at=NOW - timedelta(days=200),
            retrieved_at=NOW - timedelta(days=200),
            payload={},
        )
        self.database.save_research_run(
            run_id="run-1",
            assembled_at=NOW - timedelta(hours=1),
            snapshot_hash="a" * 64,
            permitted_citations=["PRICE:TEST_EQ", "EDINET:DOC123"],
        )
        self.workflow = ProposalWorkflow(settings=self.settings, database=self.database)

    def codes(self, error: RiskRejected) -> set[str]:
        return {violation.code for violation in error.violations}

    def propose(self, research: ResearchDecision):
        return self.workflow.propose(decision=research, portfolio=portfolio(), now=NOW)

    def test_uncited_evidence_blocks_the_proposal(self) -> None:
        with self.assertRaises(RiskRejected) as raised:
            self.propose(cited_decision("Automation demand is strong."))
        self.assertIn("EVIDENCE_FORMAT", self.codes(raised.exception))

    def test_nonexistent_citation_blocks_the_proposal(self) -> None:
        with self.assertRaises(RiskRejected) as raised:
            self.propose(
                cited_decision("[EDINET:DOES_NOT_EXIST] A filing that was never ingested.")
            )
        self.assertIn("EVIDENCE_UNKNOWN_SOURCE", self.codes(raised.exception))

    def test_item_in_database_but_outside_snapshot_is_rejected(self) -> None:
        # Exists in storage, absent from the run manifest: the model never saw it.
        with self.assertRaises(RiskRejected) as raised:
            self.propose(cited_decision("[EDINET:OLD200] An item outside the snapshot."))
        self.assertIn("EVIDENCE_UNKNOWN_SOURCE", self.codes(raised.exception))

    def test_decision_without_a_run_is_rejected(self) -> None:
        with self.assertRaises(RiskRejected) as raised:
            self.propose(cited_decision("[PRICE:TEST_EQ] Quote observed.", run_id=None))
        self.assertIn("EVIDENCE_NO_RUN", self.codes(raised.exception))

    def test_unrecorded_run_is_rejected(self) -> None:
        with self.assertRaises(RiskRejected) as raised:
            self.propose(
                cited_decision("[PRICE:TEST_EQ] Quote observed.", run_id="ghost-run")
            )
        self.assertIn("EVIDENCE_NO_RUN", self.codes(raised.exception))

    def test_stale_run_is_rejected(self) -> None:
        self.database.save_research_run(
            run_id="stale-run",
            assembled_at=NOW - timedelta(hours=30),
            snapshot_hash="b" * 64,
            permitted_citations=["PRICE:TEST_EQ"],
        )
        with self.assertRaises(RiskRejected) as raised:
            self.propose(
                cited_decision("[PRICE:TEST_EQ] Quote observed.", run_id="stale-run")
            )
        self.assertIn("EVIDENCE_STALE_RUN", self.codes(raised.exception))

    def test_future_run_is_rejected(self) -> None:
        self.database.save_research_run(
            run_id="future-run",
            assembled_at=NOW + timedelta(days=30),
            snapshot_hash="c" * 64,
            permitted_citations=["PRICE:TEST_EQ"],
        )
        with self.assertRaises(RiskRejected) as raised:
            self.propose(
                cited_decision("[PRICE:TEST_EQ] Quote observed.", run_id="future-run")
            )
        self.assertIn("EVIDENCE_FUTURE_RUN", self.codes(raised.exception))

    def test_valid_citations_produce_a_ticket(self) -> None:
        ticket = self.propose(
            cited_decision(
                "[PRICE:TEST_EQ] Quote observed in the snapshot window.",
                "[EDINET:DOC123] Filing supports the automation thesis.",
            )
        )
        self.assertEqual(ticket.ticker, "TEST_EQ")


if __name__ == "__main__":
    unittest.main()

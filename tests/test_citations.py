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


def cited_decision(*evidence: str) -> ResearchDecision:
    base = decision()
    return ResearchDecision(
        thesis=base.thesis,
        evidence=tuple(evidence),
        action=base.action,
        instrument=base.instrument,
        target_weight=base.target_weight,
        confidence=base.confidence,
        invalidation_condition=base.invalidation_condition,
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

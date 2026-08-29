from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from japan_agent.approve import ApprovalService
from japan_agent.models import ProposalStatus
from japan_agent.risk import ProposalBuilder
from japan_agent.storage import Database
from japan_agent.time import isoformat

from .helpers import NOW, decision, instrument, portfolio, snapshot


class ApprovalStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "agent.sqlite3")
        self.database.initialize()
        self.ticket = ProposalBuilder().build(
            decision=decision(),
            instrument=instrument(),
            snapshot=snapshot(),
            portfolio=portfolio(),
            submitted_trade_times=[],
            duplicate_open=False,
            now=NOW,
        )
        self.approvals = ApprovalService(self.database)
        self.approvals.submit(self.ticket)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_approval_binds_exact_ticket_hash(self) -> None:
        with self.assertRaises(ValueError):
            self.approvals.approve(
                self.ticket.proposal_id,
                approver="Bogdan",
                expected_hash="0" * 64,
                now=NOW + timedelta(minutes=1),
            )
        stored = self.approvals.approve(
            self.ticket.proposal_id,
            approver="Bogdan",
            expected_hash=self.ticket.fingerprint,
            now=NOW + timedelta(minutes=1),
        )
        self.assertEqual(stored.approved_by, "Bogdan")

    def test_expired_proposal_is_marked_expired_after_refusal(self) -> None:
        with self.assertRaises(ValueError):
            self.approvals.approve(
                self.ticket.proposal_id,
                approver="Bogdan",
                expected_hash=self.ticket.fingerprint,
                now=NOW + timedelta(hours=25),
            )
        stored = self.database.get_proposal(self.ticket.proposal_id)
        assert stored is not None
        self.assertIs(stored.status, ProposalStatus.EXPIRED)

    def test_expired_proposal_stops_blocking_duplicates(self) -> None:
        self.assertTrue(
            self.database.has_open_duplicate("TEST_EQ", "BUY", now=NOW + timedelta(minutes=5))
        )
        self.assertFalse(
            self.database.has_open_duplicate("TEST_EQ", "BUY", now=NOW + timedelta(hours=25))
        )

    def test_duplicate_matching_is_exact_not_a_like_pattern(self) -> None:
        # SQL LIKE treats "_" as a wildcard, and real T212 tickers contain it.
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO proposals(
                    proposal_id, status, ticket_json, ticket_hash, created_at, expires_at
                ) VALUES (?, 'PENDING', ?, ?, ?, ?)
                """,
                (
                    "lookalike",
                    '{"ticker":"TESTXEQ","side":"SELL"}',
                    "f" * 64,
                    isoformat(NOW),
                    isoformat(NOW + timedelta(days=1)),
                ),
            )
        self.assertTrue(self.database.has_open_duplicate("TESTXEQ", "SELL", now=NOW))
        self.assertFalse(self.database.has_open_duplicate("TEST_EQ", "SELL", now=NOW))

    def test_ticket_columns_are_immutable_in_sqlite(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE proposals SET ticket_json = ? WHERE proposal_id = ?",
                    (json.dumps({"tampered": True}), self.ticket.proposal_id),
                )

    def test_event_chain_and_jsonl_export(self) -> None:
        self.approvals.reject(
            self.ticket.proposal_id,
            approver="Bogdan",
            expected_hash=self.ticket.fingerprint,
            reason="Conviction too low",
            now=NOW + timedelta(minutes=2),
        )
        events = list(self.database.iter_events())
        self.assertEqual(events[1]["previous_hash"], events[0]["event_hash"])
        output = Path(self.temporary.name) / "journal.jsonl"
        self.assertEqual(self.database.sync_jsonl(output), 2)
        self.assertEqual(len(output.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()


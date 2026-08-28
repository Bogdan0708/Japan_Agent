from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from japan_agent.approve import ApprovalService
from japan_agent.risk import ProposalBuilder
from japan_agent.storage import Database

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

    def test_expired_ticket_cannot_be_approved(self) -> None:
        with self.assertRaises(ValueError):
            self.approvals.approve(
                self.ticket.proposal_id,
                approver="Bogdan",
                expected_hash=self.ticket.fingerprint,
                now=NOW + timedelta(hours=25),
            )

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


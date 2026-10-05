from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from predict_agent.artifacts import load_artifact, store_artifact
from predict_agent.cash import LedgerError, append_cash_entry, available_cash
from predict_agent.db import connect, transaction
from tests.predict.fixtures import NOW


class CashTestCase(unittest.TestCase):
    """A bare cohort with one portfolio, written directly: Task 3 adds the real API."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        policy = store_artifact(self.conn, "policy", "{}", NOW)
        self.conn.execute(
            "INSERT INTO cohorts (cohort_id, identity_json, code_version, starting_bankroll, "
            "baseline_window_seconds, status, started_at) "
            "VALUES ('c1', '{}', 'test', '1000.10', 1800, 'ACTIVE', '2026-10-05T12:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO portfolios (portfolio_id, cohort_id, variant, policy_hash, "
            "starting_bankroll) VALUES ('p1', 'c1', 'primary', ?, '1000.10')",
            (policy,),
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def entry(self, entry_type: str, amount: str, ticket_id: int | None = None) -> int:
        with transaction(self.conn):
            return append_cash_entry(
                self.conn, "p1", entry_type, Decimal(amount), ticket_id, NOW
            )


class ArtifactTests(CashTestCase):
    def test_store_is_content_addressed_and_idempotent(self) -> None:
        first = store_artifact(self.conn, "prompt", "Forecast {question}", NOW)
        second = store_artifact(self.conn, "prompt", "Forecast {question}", NOW)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertEqual(load_artifact(self.conn, first), ("prompt", "Forecast {question}"))

    def test_same_content_under_another_kind_is_refused(self) -> None:
        store_artifact(self.conn, "prompt", "x", NOW)
        with self.assertRaisesRegex(ValueError, "already stored as prompt"):
            store_artifact(self.conn, "policy", "x", NOW)

    def test_unknown_kind_and_missing_hash(self) -> None:
        with self.assertRaises(ValueError):
            store_artifact(self.conn, "notes", "x", NOW)
        with self.assertRaises(KeyError):
            load_artifact(self.conn, "0" * 64)

    def test_artifacts_are_immutable(self) -> None:
        digest = store_artifact(self.conn, "policy", '{"x": 1}', NOW)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "UPDATE artifacts SET content = 'y' WHERE artifact_hash = ?", (digest,)
            )


class FundingTests(CashTestCase):
    def test_funding_equals_bankroll_exactly_and_journals(self) -> None:
        self.entry("FUNDING", "1000.10")
        balance = available_cash(self.conn, "p1")
        self.assertEqual(str(balance), "1000.10")
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM journal ORDER BY seq")]
        self.assertEqual(kinds, ["CASH_FUNDING"])

    def test_second_or_wrong_funding_is_refused(self) -> None:
        with self.assertRaisesRegex(LedgerError, "starting bankroll"):
            self.entry("FUNDING", "5000")
        self.entry("FUNDING", "1000.10")
        with self.assertRaisesRegex(LedgerError, "already funded"):
            self.entry("FUNDING", "1000.10")
        self.assertEqual(available_cash(self.conn, "p1"), Decimal("1000.10"))

    def test_entries_need_their_backing_record(self) -> None:
        self.entry("FUNDING", "1000.10")
        cases = {
            "funding tied to a ticket": ("FUNDING", "1000.10", 1),
            "debit without a ticket": ("DEBIT", "1", None),
            "credit without a ticket": ("CREDIT", "1", None),
            "credit for an unknown ticket": ("CREDIT", "1", 999),
        }
        for label, (entry_type, amount, ticket_id) in cases.items():
            with self.subTest(label), self.assertRaises(LedgerError):
                self.entry(entry_type, amount, ticket_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cash_ledger").fetchone()[0], 1)

    def test_unknown_portfolio_and_invalid_amounts_rejected(self) -> None:
        with self.assertRaisesRegex(LedgerError, "unknown portfolio"), transaction(self.conn):
            append_cash_entry(self.conn, "nope", "FUNDING", Decimal("1"), None, NOW)
        for entry_type, amount in (
            ("FUNDING", "0"),
            ("CREDIT", "-1"),
            ("DEBIT", "NaN"),
            ("REFUND", "1"),
        ):
            with (
                self.subTest(entry_type=entry_type, amount=amount),
                self.assertRaises(LedgerError),
            ):
                self.entry(entry_type, amount, 1)

    def test_cash_ledger_is_append_only(self) -> None:
        self.entry("FUNDING", "1000.10")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE cash_ledger SET amount = '500'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM cash_ledger")


if __name__ == "__main__":
    unittest.main()

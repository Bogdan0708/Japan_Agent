from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from predict_agent.artifacts import store_artifact
from predict_agent.cash import InsufficientCash, LedgerError, append_cash_entry, available_cash
from predict_agent.db import connect, transaction
from predict_agent.tickets import (
    Fill,
    equity,
    open_cost,
    open_ticket,
    record_refusal_decision,
)
from tests.predict.fixtures import NOW
from tests.predict.ledger_fixtures import (
    portfolio,
    seed_baselined_forecast,
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_snapshot,
    ticket_draft,
)

LATER = NOW + timedelta(seconds=1)


class TicketTestCase(unittest.TestCase):
    bankroll = "1000"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.rules_hash = seed_market(self.conn)
        self.cohort = seed_cohort(self.conn, bankroll=self.bankroll, shadow=True)
        self.primary = portfolio(self.conn, self.cohort)
        self.shadow = portfolio(self.conn, self.cohort, "shadow_mid")
        self.forecast, self.yes, self.no = seed_baselined_forecast(
            self.conn, self.cohort, self.rules_hash
        )
        self.draft = ticket_draft(
            self.conn,
            self.primary,
            self.forecast,
            self.yes,
            fills=(Fill(Decimal("0.40"), Decimal("10")), Fill(Decimal("0.41"), Decimal("5"))),
            fee=Decimal("0.1"),
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def state(self) -> tuple[int, int, int, int]:
        return (
            self.count("paper_tickets"),
            self.count("decisions"),
            self.count("cash_ledger"),
            self.count("journal"),
        )


class OpenTicketTests(TicketTestCase):
    def test_open_ticket_debits_decides_and_keeps_equity(self) -> None:
        ticket_id = open_ticket(self.conn, self.draft, LATER)
        row = self.conn.execute(
            "SELECT shares, cost_total, status FROM paper_tickets WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("15", "6.15", "OPEN"))
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("993.85"))
        self.assertEqual(open_cost(self.conn, self.primary), Decimal("6.15"))
        self.assertEqual(equity(self.conn, self.primary), Decimal("1000"))
        decision = self.conn.execute("SELECT kind, ticket_id FROM decisions").fetchone()
        self.assertEqual(tuple(decision), ("TRADED", ticket_id))

    def test_open_ticket_failure_leaves_no_partial_state(self) -> None:
        before = self.state()
        with (
            mock.patch("predict_agent.tickets.append_journal", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            open_ticket(self.conn, self.draft, LATER)
        self.assertEqual(self.state(), before)
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("1000"))

    def test_policy_other_than_the_portfolios_is_refused_without_state(self) -> None:
        other = store_artifact(self.conn, "policy", '{"min_edge": "0.01"}', NOW)
        before = self.state()
        for policy in (other, self.conn.execute(
            "SELECT policy_hash FROM portfolios WHERE portfolio_id = ?", (self.shadow,)
        ).fetchone()[0]):
            with (
                self.subTest(policy=policy[:12]),
                self.assertRaisesRegex(LedgerError, "frozen policy"),
            ):
                open_ticket(self.conn, replace(self.draft, policy_hash=policy), LATER)
        self.assertEqual(self.state(), before)

    def test_shadow_portfolio_trades_the_same_forecast_with_its_own_cash(self) -> None:
        open_ticket(self.conn, self.draft, LATER)
        shadow_draft = ticket_draft(self.conn, self.shadow, self.forecast, self.no, outcome="NO")
        open_ticket(self.conn, shadow_draft, LATER)
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("993.85"))
        self.assertEqual(available_cash(self.conn, self.shadow), Decimal("996"))
        self.assertEqual(self.count("forecasts"), 1)
        self.assertEqual(self.count("decisions"), 2)

    def test_snapshot_must_be_the_forecast_baseline_for_that_side(self) -> None:
        with self.assertRaisesRegex(LedgerError, "baseline"):
            open_ticket(self.conn, replace(self.draft, snapshot_id=self.no), LATER)
        stray = seed_snapshot(self.conn, "YES", LATER)
        with self.assertRaisesRegex(LedgerError, "baseline"):
            open_ticket(self.conn, replace(self.draft, snapshot_id=stray), LATER)

    def test_forecast_without_timely_baseline_is_refused(self) -> None:
        other = "0x" + "7" * 64
        rules = seed_market(self.conn, other)
        bare = seed_entry_forecast(self.conn, self.cohort, rules, condition_id=other)
        late_yes = seed_snapshot(self.conn, "YES", NOW + timedelta(minutes=31), other)
        draft = replace(
            self.draft, forecast_id=bare, condition_id=other, rules_hash=rules, snapshot_id=late_yes
        )
        with self.assertRaisesRegex(LedgerError, "baseline"):
            open_ticket(self.conn, draft, NOW + timedelta(minutes=31))

    def test_second_ticket_on_same_forecast_is_refused(self) -> None:
        open_ticket(self.conn, self.draft, LATER)
        with self.assertRaisesRegex(LedgerError, "already decided"):
            open_ticket(self.conn, self.draft, LATER)
        self.assertEqual(self.count("paper_tickets"), 1)

    def test_rules_mismatch_and_closed_cohort_refused(self) -> None:
        with self.assertRaisesRegex(LedgerError, "rules"):
            open_ticket(self.conn, replace(self.draft, rules_hash="1" * 64), LATER)
        seed_cohort(self.conn, model_id="m2")
        with self.assertRaisesRegex(LedgerError, "not active"):
            open_ticket(self.conn, self.draft, LATER)

    def test_invalid_fills_rejected(self) -> None:
        bad = {
            "no fills": replace(self.draft, fills=()),
            "price 1": replace(self.draft, fills=(Fill(Decimal("1"), Decimal("1")),)),
            "zero shares": replace(self.draft, fills=(Fill(Decimal("0.5"), Decimal("0")),)),
            "negative fee": replace(self.draft, fee=Decimal("-0.01")),
            "nan price": replace(self.draft, fills=(Fill(Decimal("NaN"), Decimal("1")),)),
            "bad outcome": replace(self.draft, outcome="MAYBE"),
        }
        for label, draft in bad.items():
            with self.subTest(label), self.assertRaises(LedgerError):
                open_ticket(self.conn, draft, LATER)
        self.assertEqual(self.count("paper_tickets"), 0)

    def test_tickets_are_frozen_except_settlement(self) -> None:
        open_ticket(self.conn, self.draft, LATER)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE paper_tickets SET cost_total = '0.01'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM paper_tickets")


class CashBackingTests(TicketTestCase):
    def cash_entry(self, portfolio_id: str, entry_type: str, amount: str, ticket: int) -> None:
        with transaction(self.conn):
            append_cash_entry(self.conn, portfolio_id, entry_type, Decimal(amount), ticket, LATER)

    def test_credit_against_an_open_ticket_is_refused(self) -> None:
        ticket = open_ticket(self.conn, self.draft, LATER)
        with self.assertRaisesRegex(LedgerError, "no settlement"):
            self.cash_entry(self.primary, "CREDIT", "15", ticket)
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("993.85"))

    def test_second_or_foreign_debit_is_refused(self) -> None:
        ticket = open_ticket(self.conn, self.draft, LATER)
        with self.assertRaisesRegex(LedgerError, "already has a DEBIT"):
            self.cash_entry(self.primary, "DEBIT", "6.15", ticket)
        with self.assertRaisesRegex(LedgerError, "another portfolio"):
            self.cash_entry(self.shadow, "DEBIT", "6.15", ticket)

    def test_cash_sums_are_exact_decimal(self) -> None:
        # Three 0.1 debits must leave exactly 0.7 of 1.0, not 0.7000000000000001.
        cohort = seed_cohort(self.conn, model_id="exact", bankroll="1.0")
        primary = portfolio(self.conn, cohort)
        for digit in "234":
            condition = "0x" + digit * 64
            rules = seed_market(self.conn, condition)
            forecast, yes, _ = seed_baselined_forecast(
                self.conn, cohort, rules, condition_id=condition
            )
            draft = ticket_draft(
                self.conn, primary, forecast, yes, fills=(Fill(Decimal("0.1"), Decimal("1")),)
            )
            open_ticket(self.conn, draft, LATER)
        balance = available_cash(self.conn, primary)
        self.assertEqual(balance, Decimal("0.7"))
        self.assertEqual(str(balance), "0.7")


class SmallBankrollTests(TicketTestCase):
    bankroll = "5"

    def test_insufficient_cash_rolls_back(self) -> None:
        before = self.state()
        with self.assertRaises(InsufficientCash):
            open_ticket(self.conn, self.draft, LATER)
        self.assertEqual(self.state(), before)
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("5"))


class RefusalDecisionTests(TicketTestCase):
    def test_refusal_decision_is_recorded_once_per_portfolio(self) -> None:
        record_refusal_decision(self.conn, self.primary, self.forecast, "EDGE_BELOW_MIN", LATER)
        row = self.conn.execute("SELECT kind, reason, ticket_id FROM decisions").fetchone()
        self.assertEqual(tuple(row), ("REFUSED", "EDGE_BELOW_MIN", None))
        with self.assertRaisesRegex(LedgerError, "already decided"):
            record_refusal_decision(self.conn, self.primary, self.forecast, "AGAIN", LATER)
        with self.assertRaisesRegex(LedgerError, "already decided"):
            open_ticket(self.conn, self.draft, LATER)
        record_refusal_decision(self.conn, self.shadow, self.forecast, "EDGE_BELOW_MIN", LATER)
        self.assertEqual(self.count("decisions"), 2)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from predict_agent import db
from predict_agent.cash import available_cash
from predict_agent.db import connect
from predict_agent.forecasts import ForecastError
from predict_agent.settlement import PendingReason, payout_per_share, settle_open_tickets
from predict_agent.tickets import open_ticket
from predict_agent.util import isoformat
from tests.predict.fixtures import CONDITION_ID, NOW
from tests.predict.ledger_fixtures import (
    portfolio,
    seed_baselined_forecast,
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_observation,
    ticket_draft,
)

LATER = NOW + timedelta(seconds=1)
RESOLVED_AT = NOW + timedelta(days=20)
SETTLE_AT = NOW + timedelta(days=30)
D_HALF = Decimal("0.5")


class SettlementTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "data" / "predict.sqlite3"
        self.conn = connect(self.path)
        self.rules_hash = seed_market(self.conn)
        self.cohort = seed_cohort(self.conn)
        self.primary = portfolio(self.conn, self.cohort)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def open_test_ticket(self, outcome: str) -> int:
        """Buys 10 shares of `outcome` at 0.40 with no fee: cost 4."""
        forecast, yes, no = seed_baselined_forecast(self.conn, self.cohort, self.rules_hash)
        snapshot = yes if outcome == "YES" else no
        draft = ticket_draft(self.conn, self.primary, forecast, snapshot, outcome=outcome)
        return open_ticket(self.conn, draft, LATER)

    def observe(self, outcome: str | None, minutes: int = 0, **kwargs: object) -> int:
        at = RESOLVED_AT + timedelta(minutes=minutes)
        return seed_observation(self.conn, outcome, fetched_at=at, **kwargs)  # type: ignore[arg-type]

    def settlement(self, ticket_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM settlements WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        assert row is not None
        return row

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class PayoutTests(unittest.TestCase):
    def test_payout_table(self) -> None:
        cases = {
            ("YES", "YES"): "1",
            ("YES", "NO"): "0",
            ("NO", "NO"): "1",
            ("NO", "YES"): "0",
            ("YES", "HALF"): "0.5",
            ("NO", "HALF"): "0.5",
        }
        for (held, outcome), expected in cases.items():
            with self.subTest(held=held, outcome=outcome):
                self.assertEqual(payout_per_share(held, outcome), Decimal(expected))
        for held, outcome in (("YES", "UNKNOWN"), ("MAYBE", "HALF")):
            with self.subTest(held=held, outcome=outcome), self.assertRaises(ValueError):
                payout_per_share(held, outcome)


class SettleTests(SettlementTestCase):
    def test_winning_yes_ticket_settles_at_one(self) -> None:
        ticket = self.open_test_ticket("YES")
        self.observe("YES")
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual((summary.settled, summary.pending), (1, ()))
        row = self.settlement(ticket)
        self.assertEqual(Decimal(row["payout"]), Decimal("10"))
        self.assertEqual(Decimal(row["net_pnl"]), Decimal("6"))
        self.assertEqual(row["outcome"], "YES")
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("1006"))
        status = self.conn.execute("SELECT status FROM paper_tickets").fetchone()[0]
        self.assertEqual(status, "SETTLED")

    def test_losing_ticket_settles_with_zero_credit(self) -> None:
        ticket = self.open_test_ticket("NO")
        self.observe("YES")
        settle_open_tickets(self.conn, SETTLE_AT)
        row = self.settlement(ticket)
        self.assertEqual(Decimal(row["payout"]), Decimal("0"))
        self.assertEqual(Decimal(row["net_pnl"]), Decimal("-4"))
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("996"))

    def test_half_pays_half_on_either_side(self) -> None:
        ticket = self.open_test_ticket("NO")
        self.observe("HALF")
        settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual(Decimal(self.settlement(ticket)["net_pnl"]), Decimal("1"))
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("1001"))

    def test_half_on_the_yes_side_pays_half(self) -> None:
        ticket = self.open_test_ticket("YES")
        self.observe("HALF")
        settle_open_tickets(self.conn, SETTLE_AT)
        row = self.settlement(ticket)
        self.assertEqual((row["outcome"], Decimal(row["payout_per_share"])), ("HALF", D_HALF))
        self.assertEqual(Decimal(row["net_pnl"]), Decimal("1"))

    def test_resolved_yes_that_gamma_contradicts_never_settles(self) -> None:
        self.open_test_ticket("YES")
        self.observe("YES", cross_check="MISMATCH")
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual((summary.settled, summary.count(PendingReason.UNSETTLEABLE)), (0, 1))

    def legacy_observation(self, outcome: str, cross_check: str, minutes: float) -> None:
        """A pre-v3 row: no resolution_requested_at, so its evidence is a point in time."""
        at = isoformat(RESOLVED_AT + timedelta(minutes=minutes))
        self.conn.execute(
            "INSERT INTO resolution_observations (run_id, condition_id, "
            "resolution_fetched_at, gamma_fetched_at, gamma_json, status, outcome, "
            "cross_check, was_disputed, new_version_q, raw_json) "
            "VALUES ('r', ?, ?, ?, '{}', 'resolved', ?, ?, 0, 0, '{}')",
            (CONDITION_ID, at, at, outcome, cross_check),
        )

    def test_legacy_rows_govern_and_order_as_points(self) -> None:
        self.open_test_ticket("YES")
        self.legacy_observation("UNKNOWN", "MISMATCH", minutes=5)
        self.legacy_observation("YES", "CONFIRMED", minutes=6)  # later point supersedes
        self.assertEqual(settle_open_tickets(self.conn, SETTLE_AT).settled, 1)

    def test_legacy_contradiction_inside_a_newer_poll_interval_waits(self) -> None:
        self.open_test_ticket("YES")
        self.observe("YES", minutes=7, requested_at=RESOLVED_AT + timedelta(minutes=6))
        self.legacy_observation("UNKNOWN", "MISMATCH", minutes=6.5)
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual(summary.count(PendingReason.AMBIGUOUS_EVIDENCE), 1)

    def test_waiting_tickets_report_reason_and_age(self) -> None:
        cases = (
            (dict(outcome=None, status="posed", cross_check="NOT_APPLICABLE"),
             PendingReason.AWAITING_RESOLUTION, None),
            (dict(outcome="YES", cross_check="UNCHECKED"),
             PendingReason.AWAITING_CONFIRMATION, timedelta(days=10)),
            (dict(outcome="UNKNOWN", cross_check="MISMATCH"),
             PendingReason.UNSETTLEABLE, timedelta(days=10)),
        )
        for kwargs, reason, age in cases:
            with self.subTest(reason=reason):
                self.tearDown()
                self.setUp()
                self.open_test_ticket("YES")
                self.observe(**kwargs)  # type: ignore[arg-type]
                summary = settle_open_tickets(self.conn, SETTLE_AT)
                self.assertEqual(summary.settled, 0)
                (item,) = summary.pending
                self.assertEqual(item.reason, reason)
                self.assertEqual(item.age(SETTLE_AT), age)
                self.assertEqual(self.count("settlements"), 0)

    def test_no_observation_awaits_resolution(self) -> None:
        self.open_test_ticket("YES")
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual(summary.count(PendingReason.AWAITING_RESOLUTION), 1)

    def test_latest_observation_by_time_governs(self) -> None:
        self.open_test_ticket("YES")
        self.observe("YES", minutes=0)
        self.observe("YES", minutes=30, cross_check="UNCHECKED")
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual(summary.count(PendingReason.AWAITING_CONFIRMATION), 1)

    def test_delayed_poll_inserted_last_does_not_displace_newer_evidence(self) -> None:
        self.open_test_ticket("YES")
        self.observe("UNKNOWN", minutes=60, cross_check="MISMATCH")
        self.observe("YES", minutes=0)  # older evidence, inserted later (higher id)
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual((summary.settled, summary.count(PendingReason.UNSETTLEABLE)), (0, 1))

    def test_overlapping_polls_with_different_evidence_wait(self) -> None:
        self.open_test_ticket("YES")
        # A slow poll requested at 0 and finished at +10; a fast one ran +2..+3.
        self.observe("UNKNOWN", minutes=3, cross_check="MISMATCH",
                     requested_at=RESOLVED_AT + timedelta(minutes=2))
        self.observe("YES", minutes=10, requested_at=RESOLVED_AT)
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual(summary.count(PendingReason.AMBIGUOUS_EVIDENCE), 1)
        self.observe("YES", minutes=40)  # a later, non-overlapping confirmation settles
        self.assertEqual(settle_open_tickets(self.conn, SETTLE_AT).settled, 1)

    def test_contradiction_from_a_later_gamma_cross_check_waits(self) -> None:
        # A slow poll fetched resolutions at 0:01 but finished its Gamma cross-check at
        # 10:00 (MISMATCH); a fast poll ran 2:00..2:02 (CONFIRMED). The cross-check is part
        # of the evidence, so the slow poll overlaps the fast one and nothing settles.
        self.open_test_ticket("YES")
        seed_observation(
            self.conn,
            "UNKNOWN",
            cross_check="MISMATCH",
            requested_at=RESOLVED_AT,
            fetched_at=RESOLVED_AT + timedelta(seconds=1),
            gamma_fetched_at=RESOLVED_AT + timedelta(minutes=10),
        )
        seed_observation(
            self.conn,
            "YES",
            requested_at=RESOLVED_AT + timedelta(minutes=2),
            fetched_at=RESOLVED_AT + timedelta(minutes=2, seconds=1),
            gamma_fetched_at=RESOLVED_AT + timedelta(minutes=2, seconds=2),
        )
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual((summary.settled, summary.count(PendingReason.AMBIGUOUS_EVIDENCE)), (0, 1))
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("996"))
        self.observe("YES", minutes=40)  # a later, non-overlapping confirmation settles
        self.assertEqual(settle_open_tickets(self.conn, SETTLE_AT).settled, 1)

    def test_tied_agreeing_observations_decide_the_same_in_any_insertion_order(self) -> None:
        # Two CONFIRMED YES polls finish together at +10; one started at 0 (overlapping a
        # MISMATCH that finished at +5), the other at +9 (after it). The poll that began
        # after the contradiction ended supersedes it, whichever row was inserted first.
        for order in ("early-first", "late-first"):
            with self.subTest(order=order):
                self.tearDown()
                self.setUp()
                self.open_test_ticket("YES")
                self.observe("UNKNOWN", minutes=5, cross_check="MISMATCH",
                             requested_at=RESOLVED_AT + timedelta(minutes=4))
                starts = [RESOLVED_AT, RESOLVED_AT + timedelta(minutes=9)]
                if order == "late-first":
                    starts.reverse()
                for start in starts:
                    self.observe("YES", minutes=10, requested_at=start)
                self.assertEqual(settle_open_tickets(self.conn, SETTLE_AT).settled, 1)

    def test_superseding_observation_committed_before_settlement_wins(self) -> None:
        # Another process records a newer MISMATCH just before settlement takes its lock:
        # the governing observation must be read inside the settlement transaction.
        self.open_test_ticket("YES")
        self.observe("YES")
        other = connect(self.path)
        original = db.transaction

        @contextlib.contextmanager
        def racing_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
            seed_observation(
                other, "UNKNOWN", cross_check="MISMATCH",
                fetched_at=RESOLVED_AT + timedelta(hours=1),
            )
            with original(conn) as inner:
                yield inner

        try:
            with mock.patch("predict_agent.settlement.transaction", racing_transaction):
                summary = settle_open_tickets(self.conn, SETTLE_AT)
        finally:
            other.close()
        self.assertEqual((summary.settled, summary.count(PendingReason.UNSETTLEABLE)), (0, 1))
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("996"))

    def test_settle_is_idempotent(self) -> None:
        self.open_test_ticket("YES")
        self.observe("YES")
        settle_open_tickets(self.conn, SETTLE_AT)
        second = settle_open_tickets(self.conn, SETTLE_AT + timedelta(hours=1))
        self.assertEqual((second.settled, second.pending), (0, ()))
        self.assertEqual(self.count("settlements"), 1)
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("1006"))

    def test_closed_cohort_still_settles_but_cannot_open(self) -> None:
        self.open_test_ticket("YES")
        seed_cohort(self.conn, model_id="m2")
        self.observe("YES")
        self.assertEqual(settle_open_tickets(self.conn, SETTLE_AT).settled, 1)
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("1006"))
        other = "0x" + "8" * 64
        rules = seed_market(self.conn, other)
        with self.assertRaisesRegex(ForecastError, "not active"):
            seed_entry_forecast(self.conn, self.cohort, rules, condition_id=other)

    def test_settlement_rows_are_immutable(self) -> None:
        self.open_test_ticket("YES")
        self.observe("YES")
        settle_open_tickets(self.conn, SETTLE_AT)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE settlements SET payout = '999'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE paper_tickets SET status = 'OPEN'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM resolution_observations")


if __name__ == "__main__":
    unittest.main()

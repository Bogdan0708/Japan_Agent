from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from predict_agent.artifacts import store_artifact
from predict_agent.cash import cash_entry_hash
from predict_agent.cli import main
from predict_agent.db import GENESIS_HASH, connect
from predict_agent.invariants import verify_ledger
from predict_agent.settlement import settle_open_tickets
from predict_agent.tickets import Fill, open_ticket, ticket_hash
from predict_agent.util import isoformat
from tests.predict.fixtures import NOW
from tests.predict.ledger_fixtures import (
    portfolio,
    seed_baselined_forecast,
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_observation,
    seed_snapshot,
    ticket_draft,
)

REPO = Path(__file__).resolve().parents[2]
LATER = NOW + timedelta(seconds=1)
SETTLE_AT = NOW + timedelta(days=30)


class InvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        shutil.copy(
            REPO / "config" / "predict-policy.example.json",
            self.root / "config" / "predict-policy.json",
        )
        self.conn = connect(self.root / "data" / "predict.sqlite3")
        self.rules_hash = seed_market(self.conn)
        self.cohort = seed_cohort(self.conn, shadow=True)
        self.primary = portfolio(self.conn, self.cohort)
        self.shadow = portfolio(self.conn, self.cohort, "shadow_mid")
        self.forecast, yes, no = seed_baselined_forecast(self.conn, self.cohort, self.rules_hash)
        self.ticket = open_ticket(
            self.conn,
            ticket_draft(
                self.conn,
                self.primary,
                self.forecast,
                yes,
                fills=(Fill(Decimal("0.40"), Decimal("10")),),
                fee=Decimal("0"),  # the snapshot is fee-free
            ),
            LATER,
        )
        self.shadow_ticket = open_ticket(
            self.conn, ticket_draft(self.conn, self.shadow, self.forecast, no, outcome="NO"), LATER
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(argv, root=self.root, now_fn=lambda: SETTLE_AT)
        return code, out.getvalue()

    def assert_problem(self, fragment: str) -> None:
        problems = verify_ledger(self.conn)
        self.assertTrue(any(fragment in p for p in problems), problems)

    def append_raw_cash(self, portfolio_id: str, entry_type: str, amount: str, ticket: int | None):
        """Append a correctly chained entry, bypassing the API's backing checks."""
        last = self.conn.execute(
            "SELECT entry_hash FROM cash_ledger WHERE portfolio_id = ? "
            "ORDER BY entry_id DESC LIMIT 1",
            (portfolio_id,),
        ).fetchone()
        prev_hash = last[0] if last else GENESIS_HASH
        at = isoformat(LATER)
        digest = cash_entry_hash(portfolio_id, entry_type, amount, ticket, at, prev_hash)
        self.conn.execute(
            "INSERT INTO cash_ledger (portfolio_id, entry_type, amount, ticket_id, at, "
            "entry_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (portfolio_id, entry_type, amount, ticket, at, digest),
        )

    def test_sound_ledger_has_no_problems_before_and_after_settlement(self) -> None:
        self.assertEqual(verify_ledger(self.conn), [])
        seed_observation(self.conn, "YES", fetched_at=NOW + timedelta(days=20))
        self.assertEqual(settle_open_tickets(self.conn, SETTLE_AT).settled, 2)
        self.assertEqual(verify_ledger(self.conn), [])

    # Hash tampering (a trigger was dropped first).

    def test_dropped_trigger_is_reported(self) -> None:
        self.conn.execute("DROP TRIGGER cash_no_update")
        self.assert_problem("missing immutability trigger cash_no_update")

    def test_tampered_cash_entry_breaks_chain(self) -> None:
        self.conn.execute("DROP TRIGGER cash_no_update")
        self.conn.execute("UPDATE cash_ledger SET amount = '5000' WHERE entry_type = 'FUNDING'")
        self.assert_problem("cash chain")

    def test_tampered_ticket_is_detected(self) -> None:
        self.conn.execute("DROP TRIGGER tickets_open_to_settled_only")
        self.conn.execute("UPDATE paper_tickets SET cost_total = '0.01'")
        self.assert_problem("ticket hash")
        self.assert_problem("debit")

    def test_altered_prompt_artifact_is_detected(self) -> None:
        self.conn.execute("DROP TRIGGER artifacts_no_update")
        self.conn.execute(
            "UPDATE artifacts SET content = 'Use market prices.' WHERE kind = 'prompt'"
        )
        self.assert_problem("does not match its hash")

    def test_altered_forecast_probability_is_detected(self) -> None:
        self.conn.execute("DROP TRIGGER forecasts_no_update")
        self.conn.execute("UPDATE forecasts SET p_mid = '0.90'")
        self.assert_problem("forecast hash")

    def test_settled_without_settlement_is_detected(self) -> None:
        self.conn.execute("DROP TRIGGER tickets_open_to_settled_only")
        self.conn.execute("UPDATE paper_tickets SET status = 'SETTLED'")
        self.assert_problem("without a settlement")

    # Every hash valid, relationship broken.

    def test_second_funding_is_detected(self) -> None:
        self.conn.execute("DROP INDEX one_funding_per_portfolio")
        self.append_raw_cash(self.primary, "FUNDING", "1000", None)
        self.assert_problem("exactly one first FUNDING")

    def test_credit_against_open_ticket_is_detected(self) -> None:
        self.append_raw_cash(self.primary, "CREDIT", "10", self.ticket)
        self.assert_problem("credit without a settlement")

    def test_entry_attributed_to_another_portfolios_ticket_is_detected(self) -> None:
        self.append_raw_cash(self.primary, "CREDIT", "0", self.shadow_ticket)
        self.assert_problem("ticket of another portfolio")

    def test_ticket_with_another_policy_and_valid_hash_is_detected(self) -> None:
        other = store_artifact(self.conn, "policy", '{"min_edge": "0.01"}', NOW)
        self.conn.execute("DROP TRIGGER tickets_open_to_settled_only")
        row = dict(
            self.conn.execute(
                "SELECT * FROM paper_tickets WHERE ticket_id = ?", (self.ticket,)
            ).fetchone()
        )
        row["policy_hash"] = other
        self.conn.execute(
            "UPDATE paper_tickets SET policy_hash = ?, ticket_hash = ? WHERE ticket_id = ?",
            (other, ticket_hash(row), self.ticket),
        )
        problems = verify_ledger(self.conn)
        self.assertFalse(any("ticket hash" in p for p in problems), problems)
        self.assert_problem("frozen policy")

    def test_wrong_settlement_arithmetic_is_detected(self) -> None:
        seed_observation(self.conn, "YES", fetched_at=NOW + timedelta(days=20))
        settle_open_tickets(self.conn, SETTLE_AT)
        self.conn.execute("DROP TRIGGER settlements_no_update")
        self.conn.execute(
            "UPDATE settlements SET payout_per_share = '0.5' WHERE ticket_id = ?", (self.ticket,)
        )
        self.assert_problem("payout arithmetic")

    def test_settlement_from_an_unconfirmed_observation_is_detected(self) -> None:
        seed_observation(self.conn, "YES", fetched_at=NOW + timedelta(days=20))
        settle_open_tickets(self.conn, SETTLE_AT)
        unchecked = seed_observation(
            self.conn, "YES", cross_check="UNCHECKED", fetched_at=NOW + timedelta(days=21)
        )
        self.conn.execute("DROP TRIGGER settlements_no_update")
        self.conn.execute("UPDATE settlements SET observation_id = ?", (unchecked,))
        self.assert_problem("not backed by a confirmed resolution")

    def test_late_baseline_inserted_directly_is_detected(self) -> None:
        other = "0x" + "6" * 64
        rules = seed_market(self.conn, other)
        forecast = seed_entry_forecast(self.conn, self.cohort, rules, condition_id=other)
        late = NOW + timedelta(minutes=31)
        self.conn.execute(
            "INSERT INTO forecast_baselines (forecast_id, yes_snapshot_id, no_snapshot_id, "
            "reason, attached_at) VALUES (?, ?, ?, NULL, ?)",
            (
                forecast,
                seed_snapshot(self.conn, "YES", late, other),
                seed_snapshot(self.conn, "NO", late, other),
                isoformat(late),
            ),
        )
        self.assert_problem("outside the baseline window")

    def test_two_active_cohorts_are_reported(self) -> None:
        self.conn.execute("DROP INDEX one_active_cohort")
        self.conn.execute("DROP TRIGGER cohorts_active_to_closed_only")
        seed_cohort(self.conn, model_id="m2")
        self.conn.execute("UPDATE cohorts SET status = 'ACTIVE', closed_at = NULL")
        self.assert_problem("more than one ACTIVE cohort")

    def corrupt_funding(self) -> None:
        self.conn.execute("DROP TRIGGER cash_no_update")
        self.conn.execute("UPDATE cash_ledger SET amount = 'abc' WHERE entry_type = 'FUNDING'")

    def test_verify_ledger_reports_unreadable_rows_instead_of_raising(self) -> None:
        self.corrupt_funding()
        problems = verify_ledger(self.conn)
        self.assertTrue(any("missing immutability trigger cash_no_update" in p for p in problems))
        self.assertTrue(any("unreadable" in p for p in problems), problems)
        self.conn.execute("DROP TRIGGER cohorts_active_to_closed_only")
        self.conn.execute("UPDATE cohorts SET identity_json = '{not json'")
        problems = verify_ledger(self.conn)
        self.assertTrue(any(p.startswith("cohort") and "unreadable" in p for p in problems))

    def test_verify_ledger_leaves_no_transaction_open(self) -> None:
        verify_ledger(self.conn)
        self.assertFalse(self.conn.in_transaction)
        self.corrupt_funding()
        verify_ledger(self.conn)
        self.assertFalse(self.conn.in_transaction)

    def test_doctor_reports_unreadable_rows_and_still_prints_the_journal_line(self) -> None:
        self.corrupt_funding()
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 3, output)
        self.assertIn("journal chain", output)
        self.assertIn("unreadable", output)

    def test_orphan_cash_entry_is_detected(self) -> None:
        # Written with foreign keys off, as a corrupting tool might; no trigger is removed.
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.append_raw_cash("no-such-portfolio", "FUNDING", "1000", None)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.assert_problem("foreign key violation: cash_ledger")

    def test_orphan_decision_is_detected(self) -> None:
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute(
            "INSERT INTO decisions (portfolio_id, forecast_id, condition_id, kind, reason, "
            "ticket_id, decided_at) VALUES ('ghost', 999, 'c', 'REFUSED', 'x', NULL, ?)",
            (isoformat(LATER),),
        )
        self.conn.execute("PRAGMA foreign_keys = ON")
        problems = verify_ledger(self.conn)
        violations = [p for p in problems if p.startswith("foreign key violation: decisions")]
        self.assertEqual(len(violations), 2, problems)  # missing portfolio and forecast

    # Commands.

    def test_settle_command_runs_offline_and_reports_pending_age(self) -> None:
        seed_observation(
            self.conn, "YES", cross_check="UNCHECKED", fetched_at=NOW + timedelta(days=20)
        )
        code, output = self.run_cli(["settle"])
        self.assertEqual(code, 0, output)
        self.assertIn("settled 0; pending 2", output)
        self.assertIn("AWAITING_CONFIRMATION (resolved 10 days, 0:00:00 ago)", output)

    def test_doctor_reports_an_unreadable_journal_as_broken(self) -> None:
        self.conn.execute("DROP TRIGGER journal_no_update")
        self.conn.execute("UPDATE journal SET payload_json = 'not json' WHERE seq = 1")
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 3, output)
        self.assertIn("journal chain BROKEN", output)

    def test_doctor_fails_on_ledger_problem(self) -> None:
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)
        self.conn.execute("DROP TRIGGER forecasts_no_update")
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 3)
        self.assertIn("missing immutability trigger forecasts_no_update", output)


if __name__ == "__main__":
    unittest.main()

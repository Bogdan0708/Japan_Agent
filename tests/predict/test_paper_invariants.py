from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from predict_agent.cli import main
from predict_agent.cohorts import cohort_portfolios
from predict_agent.db import connect
from predict_agent.fills import level_fee
from predict_agent.invariants import verify_ledger
from predict_agent.tickets import ticket_hash
from predict_agent.util import canonical_json, isoformat
from tests.predict.fixtures import CONDITION_ID, NOW
from tests.predict.trade_fixtures import (
    POLITICS_FEES,
    seed_discovery,
    seed_policy_cohort,
    seed_ready_forecast,
    seed_tradeable_market,
)

REPO = Path(__file__).resolve().parents[2]
DECIDE_AT = NOW + timedelta(seconds=30)


class FillInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        shutil.copy(
            REPO / "config" / "predict-policy.example.json",
            self.root / "config" / "predict-policy.json",
        )
        self.conn = connect(self.root / "data" / "predict.sqlite3")
        rules = seed_tradeable_market(self.conn)
        seed_discovery(self.conn, [CONDITION_ID])
        self.cohort = seed_policy_cohort(self.conn)
        seed_ready_forecast(
            self.conn, self.cohort, rules, fee_schedule=POLITICS_FEES,
            yes_asks=[("0.50", "20"), ("0.51", "100"), ("0.60", "100")],
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(argv, root=self.root, now_fn=lambda: DECIDE_AT)
        return code, out.getvalue()

    def rewrite_ticket(self, **changes: Any) -> None:
        """Change a primary ticket's columns and re-hash it, so only the fill check can
        notice (the hash stays valid)."""
        self.conn.execute("DROP TRIGGER tickets_open_to_settled_only")
        primary = cohort_portfolios(self.conn, self.cohort)["primary"]
        row = dict(
            self.conn.execute(
                "SELECT * FROM paper_tickets WHERE portfolio_id = ?", (primary,)
            ).fetchone()
        )
        row.update(changes)
        assignments = ", ".join(f"{column} = ?" for column in changes)
        self.conn.execute(
            f"UPDATE paper_tickets SET {assignments}, ticket_hash = ? WHERE ticket_id = ?",
            (*changes.values(), ticket_hash(row), row["ticket_id"]),
        )

    def assert_problem(self, fragment: str) -> None:
        problems = verify_ledger(self.conn)
        self.assertTrue(any(fragment in p for p in problems), problems)
        self.assertFalse(any("ticket hash" in p for p in problems), problems)

    def test_trade_command_trades_and_doctor_stays_clean(self) -> None:
        code, output = self.run_cli(["trade"])
        self.assertEqual(code, 0, output)
        self.assertIn("traded 2; refused 0; waiting 0", output)
        self.assertEqual(verify_ledger(self.conn), [])
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)

    def test_fills_beyond_recorded_depth_are_detected(self) -> None:
        self.run_cli(["trade"])
        self.rewrite_ticket(fills_json=canonical_json([["0.50", "25"]]))
        self.assert_problem("fills are not within the recorded book")

    def test_non_positive_share_fills_are_detected(self) -> None:
        self.run_cli(["trade"])
        fills = [(Decimal("0.50"), Decimal("20")), (Decimal("0.51"), Decimal("-5"))]
        rate = Decimal("0.04")
        fee = sum((level_fee(s, p, rate) for p, s in fills), Decimal("0"))
        notional = sum((p * s for p, s in fills), Decimal("0"))
        self.rewrite_ticket(
            fills_json=canonical_json([[str(p), str(s)] for p, s in fills]),
            shares=str(sum((s for _, s in fills), Decimal("0"))),
            fee=str(fee),
            cost_total=str(notional + fee),
        )
        self.assert_problem("fills are not within the recorded book")

    def test_fee_not_from_the_snapshot_schedule_is_detected(self) -> None:
        self.run_cli(["trade"])
        self.rewrite_ticket(fee="0")
        self.assert_problem("fee differs from the snapshot's per-level fee")

    def test_fills_past_the_slippage_limit_are_detected(self) -> None:
        self.run_cli(["trade"])
        self.rewrite_ticket(fills_json=canonical_json([["0.60", "10"]]))
        self.assert_problem("slippage limit")

    def test_decision_on_a_stale_book_is_detected(self) -> None:
        self.run_cli(["trade"])
        self.rewrite_ticket(created_at=isoformat(NOW + timedelta(minutes=5)))
        self.assert_problem("decided on a book")

    def test_trade_command_reports_refusals_by_reason(self) -> None:
        seed_discovery(self.conn, [], at=NOW + timedelta(seconds=5))
        code, output = self.run_cli(["trade"])
        self.assertEqual(code, 0, output)
        self.assertIn("refused 2 (ELIGIBILITY_LOST 2)", output)
        decision = self.conn.execute("SELECT reason FROM decisions LIMIT 1").fetchone()[0]
        self.assertEqual(decision, "ELIGIBILITY_LOST")
        self.assertTrue(json.loads(self.conn.execute(
            "SELECT payload_json FROM journal WHERE kind = 'DECISION_REFUSED' LIMIT 1"
        ).fetchone()[0])["detail"])
        tickets = self.conn.execute("SELECT COUNT(*) FROM paper_tickets").fetchone()[0]
        self.assertEqual(tickets, 0)


if __name__ == "__main__":
    unittest.main()

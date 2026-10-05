from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from predict_agent.cash import available_cash
from predict_agent.cohorts import cohort_portfolios
from predict_agent.db import connect
from predict_agent.forecasts import ResumeStep, resume_step
from predict_agent.invariants import verify_ledger
from predict_agent.paper import decide_portfolio, trade_ready
from predict_agent.settlement import settle_open_tickets
from predict_agent.tickets import equity
from tests.predict.fixtures import CONDITION_ID, NOW
from tests.predict.ledger_fixtures import seed_cohort, seed_entry_forecast, seed_observation
from tests.predict.trade_fixtures import (
    POLITICS_FEES,
    seed_discovery,
    seed_policy_cohort,
    seed_ready_forecast,
    seed_tradeable_market,
)

DECIDE_AT = NOW + timedelta(seconds=30)
D = Decimal


class PaperTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.rules_hash = seed_tradeable_market(self.conn)
        seed_discovery(self.conn, [CONDITION_ID])
        self.cohort = seed_policy_cohort(self.conn)
        self.portfolios = cohort_portfolios(self.conn, self.cohort)
        self.primary = self.portfolios["primary"]
        self.shadow = self.portfolios["shadow_mid"]

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def decisions(self) -> dict[str, tuple[str, str | None]]:
        rows = self.conn.execute("SELECT portfolio_id, kind, reason FROM decisions").fetchall()
        return {row["portfolio_id"]: (row["kind"], row["reason"]) for row in rows}

    def ticket(self, portfolio_id: str) -> dict[str, str]:
        row = self.conn.execute(
            "SELECT outcome, shares, cost_total, fills_json FROM paper_tickets "
            "WHERE portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()
        return dict(row)


class TradeTests(PaperTestCase):
    def test_spec_worked_example_end_to_end_in_both_portfolios(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        summary = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual((summary.traded, summary.refused, summary.waiting), (2, {}, 0))
        ticket = self.ticket(self.primary)
        self.assertEqual(ticket["outcome"], "YES")
        self.assertEqual(D(ticket["shares"]), D("40"))
        self.assertEqual(D(ticket["cost_total"]), D("20"))
        fills = [(D(p), D(s)) for p, s in json.loads(ticket["fills_json"])]
        self.assertEqual(fills, [(D("0.50"), D("40"))])
        self.assertEqual(available_cash(self.conn, self.primary), D("980"))
        self.assertEqual(available_cash(self.conn, self.shadow), D("980"))
        self.assertEqual(verify_ledger(self.conn), [])

    def test_primary_refuses_where_the_shadow_mid_policy_trades(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash, p=("0.52", "0.60", "0.70"))
        summary = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual((summary.traded, summary.refused), (1, {"NO_EDGE": 1}))
        self.assertEqual(
            self.decisions(),
            {self.primary: ("REFUSED", "NO_EDGE"), self.shadow: ("TRADED", None)},
        )
        self.assertEqual(available_cash(self.conn, self.primary), D("1000"))
        self.assertEqual(verify_ledger(self.conn), [])

    def test_fees_are_charged_per_level_from_the_snapshot_schedule(self) -> None:
        seed_ready_forecast(
            self.conn, self.cohort, self.rules_hash, fee_schedule=POLITICS_FEES,
            yes_asks=[("0.50", "20"), ("0.51", "100")],
        )
        trade_ready(self.conn, DECIDE_AT)
        row = self.conn.execute(
            "SELECT fee, cost_total, fills_json FROM paper_tickets WHERE portfolio_id = ?",
            (self.primary,),
        ).fetchone()
        fills = [(D(p), D(s)) for p, s in json.loads(row["fills_json"])]
        expected = sum((s * D("0.04") * p * (1 - p) for p, s in fills), D("0"))
        self.assertEqual(D(row["fee"]), expected)
        self.assertLessEqual(D(row["cost_total"]), D("20"))
        self.assertEqual(verify_ledger(self.conn), [])

    def test_refusals_are_recorded_with_code_and_journal_detail(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        seed_discovery(self.conn, [], at=NOW + timedelta(seconds=5))  # market dropped out
        summary = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual(summary.refused, {"ELIGIBILITY_LOST": 2})
        payload = json.loads(
            self.conn.execute(
                "SELECT payload_json FROM journal WHERE kind = 'DECISION_REFUSED' LIMIT 1"
            ).fetchone()[0]
        )
        self.assertIn("discovery run", payload["detail"])

    def test_open_statuses_initialized_and_active_do_not_refuse(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        seed_observation(self.conn, None, status="initialized", cross_check="NOT_APPLICABLE",
                        fetched_at=NOW + timedelta(seconds=10))
        summary = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual((summary.traded, summary.refused), (2, {}))
        seed_observation(self.conn, None, status="active", cross_check="NOT_APPLICABLE",
                        fetched_at=NOW + timedelta(seconds=11))
        summary = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual((summary.traded, summary.refused), (0, {}))

    def test_resolution_started_refuses(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        seed_observation(self.conn, "YES", fetched_at=NOW + timedelta(seconds=10))
        self.assertEqual(trade_ready(self.conn, DECIDE_AT).refused, {"RESOLUTION_STARTED": 2})

    def test_rules_changed_since_the_forecast_refuses(self) -> None:
        from predict_agent.util import canonical_json, sha256_json

        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        # Insert a new rules version and update the market to point to it
        new_payload = {
            "question": "Will it happen?",
            "rules_text": "Updated rules.",
            "resolution_source": "",
            "end_date": (NOW + timedelta(days=20)).isoformat(),
        }
        new_hash = sha256_json(new_payload)
        updated_at = (NOW + timedelta(seconds=5)).isoformat()
        self.conn.execute(
            "INSERT INTO rules_versions (condition_id, rules_hash, rules_json, first_seen_at) "
            "VALUES (?, ?, ?, ?)",
            (CONDITION_ID, new_hash, canonical_json(new_payload), updated_at),
        )
        self.conn.execute(
            "UPDATE markets SET current_rules_hash = ? WHERE condition_id = ?",
            (new_hash, CONDITION_ID),
        )
        self.assertEqual(trade_ready(self.conn, DECIDE_AT).refused, {"RULES_CHANGED": 2})

    def test_a_late_decision_on_a_stale_book_refuses_and_is_final(self) -> None:
        forecast = seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        late = NOW + timedelta(minutes=10)
        self.assertEqual(trade_ready(self.conn, late).refused, {"STALE_BOOK": 2})
        self.assertEqual(resume_step(self.conn, forecast, late), ResumeStep.DONE)

    def test_closed_cohort_forecasts_are_refused_cohort_closed(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        seed_cohort(self.conn, model_id="successor")
        summary = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual((summary.traded, summary.refused), (0, {"COHORT_CLOSED": 2}))

    def test_rerun_is_idempotent_and_unbaselined_forecasts_wait(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        other = "0x" + "4" * 64
        seed_entry_forecast(
            self.conn, self.cohort, seed_tradeable_market(self.conn, other), condition_id=other
        )
        first = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual((first.traded, first.waiting), (2, 1))
        second = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual((second.traded, second.refused, second.waiting), (0, {}, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_tickets").fetchone()[0], 2)

    def test_failure_while_writing_leaves_no_decision_and_can_be_retried(self) -> None:
        forecast = seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        with (
            mock.patch("predict_agent.tickets.append_journal", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            decide_portfolio(self.conn, self.primary, forecast, DECIDE_AT)
        self.assertEqual(self.decisions(), {})
        self.assertEqual(available_cash(self.conn, self.primary), D("1000"))
        result = decide_portfolio(self.conn, self.primary, forecast, DECIDE_AT)
        self.assertIsNotNone(result.ticket_id)


class PortfolioStateTests(PaperTestCase):
    def test_losses_shrink_equity_and_the_next_size(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        trade_ready(self.conn, DECIDE_AT)
        seed_observation(self.conn, "NO", fetched_at=NOW + timedelta(days=1))
        settle_open_tickets(self.conn, NOW + timedelta(days=1))
        self.assertEqual(equity(self.conn, self.primary), D("980"))
        later = NOW + timedelta(days=2)
        other = "0x" + "6" * 64
        rules = seed_tradeable_market(self.conn, other, event_id="e2",
                                      end_date=later + timedelta(days=20))
        seed_discovery(self.conn, [other], at=later)
        seed_ready_forecast(self.conn, self.cohort, rules, condition_id=other, at=later)
        trade_ready(self.conn, later + timedelta(seconds=30))
        row = self.conn.execute(
            "SELECT cost_total FROM paper_tickets WHERE portfolio_id = ? AND condition_id = ?",
            (self.primary, other),
        ).fetchone()
        self.assertEqual(D(row["cost_total"]), D("19.60"))  # 2% of $980

    def test_event_cap_counts_open_tickets_in_the_same_event(self) -> None:
        markets = ["0x" + digit * 64 for digit in "abc"]
        for condition_id in markets:
            rules = seed_tradeable_market(self.conn, condition_id, event_id="shared")
            seed_ready_forecast(self.conn, self.cohort, rules, condition_id=condition_id)
        seed_discovery(self.conn, markets)
        trade_ready(self.conn, DECIDE_AT)
        costs = [
            D(row["cost_total"])
            for row in self.conn.execute(
                "SELECT cost_total FROM paper_tickets WHERE portfolio_id = ? ORDER BY ticket_id",
                (self.primary,),
            )
        ]
        # 2% market cap each ($20), 5% event cap ($50): $20 + $20 + $10.
        self.assertEqual(costs, [D("20"), D("20"), D("10")])


if __name__ == "__main__":
    unittest.main()

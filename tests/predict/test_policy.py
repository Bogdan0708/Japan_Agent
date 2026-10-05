from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from predict_agent.fills import AskLevel
from predict_agent.policy import (
    Exposure,
    ForecastView,
    PolicyInputs,
    Refusal,
    SideBook,
    Trade,
    budget_for,
    decide,
    kelly,
)
from predict_agent.policy_params import PolicyParams, parse_policy
from tests.predict.fixtures import NOW

D = Decimal
REPO = Path(__file__).resolve().parents[2]
SECTION = json.loads((REPO / "config" / "predict-policy.example.json").read_text())["policy"]
BOUNDS = parse_policy(SECTION, "bounds")
MID = parse_policy(SECTION, "mid")
ZERO_EXPOSURE = Exposure(D("0"), D("0"), D("0"), D("0"))
FETCHED = NOW - timedelta(seconds=30)


def side(outcome: str, *levels: tuple[str, str], **overrides: Any) -> SideBook:
    book = SideBook(
        outcome=outcome,
        snapshot_id=1 if outcome == "YES" else 2,
        fetched_at=FETCHED,
        asks=tuple(AskLevel(D(p), D(s)) for p, s in levels),
        tick_size=D("0.01"),
        min_order_size=D("5"),
        fees_enabled=False,
        fee_schedule=None,
    )
    return replace(book, **overrides)


def inputs(
    p: tuple[str, str, str] = ("0.80", "0.85", "0.90"),
    yes: SideBook | None = None,
    no: SideBook | None = None,
    **overrides: Any,
) -> PolicyInputs:
    base = PolicyInputs(
        forecast=ForecastView(
            abstained=False,
            p_low=D(p[0]),
            p_mid=D(p[1]),
            p_high=D(p[2]),
            confidence="medium",
            rules_hash="r1",
            end_date=NOW + timedelta(days=10),
        ),
        current_rules_hash="r1",
        eligible=True,
        resolution_started=False,
        already_traded=False,
        books={
            "YES": yes or side("YES", ("0.50", "50")),
            "NO": no or side("NO", ("0.45", "50")),
        },
        equity=D("1000"),
        available_cash=D("1000"),
        exposure=ZERO_EXPOSURE,
        now=NOW,
    )
    return replace(base, **overrides)


def loose(params: PolicyParams = BOUNDS) -> PolicyParams:
    return replace(params, cap_market=D("1"), cap_event=D("1"), cap_category=D("1"),
                   cap_total_open=D("1"))


class SizingTests(unittest.TestCase):
    def test_spec_worked_example(self) -> None:
        # q=0.80, ask 0.50, equity $1,000: quarter Kelly is $150 (300 shares), the 2% market
        # cap limits the budget to $20, so 40 shares fill from the 50-share book.
        self.assertEqual(kelly(D("0.80"), D("0.50")), D("0.6"))
        self.assertEqual(budget_for(D("0.80"), D("0.50"), inputs(), loose()), D("150.00"))
        trade = decide(inputs(), BOUNDS)
        assert isinstance(trade, Trade), trade
        self.assertEqual((trade.outcome, trade.snapshot_id), ("YES", 1))
        self.assertEqual(trade.budget, D("20.00"))
        self.assertEqual(trade.walk.shares, D("40"))
        self.assertEqual(trade.walk.cost, D("20.00"))
        self.assertEqual(trade.edge, D("0.30"))

    def test_kelly_budget_is_limited_by_recorded_depth(self) -> None:
        trade = decide(inputs(), loose())
        assert isinstance(trade, Trade), trade
        self.assertEqual(trade.walk.shares, D("50"))  # $150 budget, only 50 shares exist

    def test_equity_shrinks_every_size(self) -> None:
        trade = decide(inputs(equity=D("500"), available_cash=D("500")), BOUNDS)
        assert isinstance(trade, Trade), trade
        self.assertEqual(trade.walk.shares, D("20"))  # 2% of $500 = $10

    def test_each_cap_and_cash_can_exhaust_the_budget(self) -> None:
        cases = {
            "market": Exposure(D("20"), D("20"), D("20"), D("20")),
            "event": Exposure(D("0"), D("50"), D("50"), D("50")),
            "category": Exposure(D("0"), D("0"), D("150"), D("150")),
            "total": Exposure(D("0"), D("0"), D("0"), D("500")),
        }
        for label, exposure in cases.items():
            with self.subTest(label):
                result = decide(inputs(exposure=exposure), BOUNDS)
                self.assertIsInstance(result, Refusal)
                self.assertEqual(result.reason, "NO_BUDGET")  # type: ignore[union-attr]
        broke = decide(inputs(available_cash=D("0")), BOUNDS)
        self.assertEqual(broke, Refusal("NO_BUDGET", broke.detail))  # type: ignore[union-attr]

    def test_cap_headroom_is_what_remains(self) -> None:
        trade = decide(inputs(exposure=Exposure(D("0"), D("44"), D("44"), D("44"))), BOUNDS)
        assert isinstance(trade, Trade), trade
        self.assertEqual(trade.budget, D("6.00"))  # event cap $50 - $44 open


class ProbabilityTests(unittest.TestCase):
    def test_primary_uses_conservative_bounds_and_shadow_uses_p_mid(self) -> None:
        narrow_edge = inputs(p=("0.52", "0.60", "0.70"))
        self.assertEqual(decide(narrow_edge, BOUNDS).reason, "NO_EDGE")  # type: ignore[union-attr]
        trade = decide(narrow_edge, MID)
        assert isinstance(trade, Trade), trade
        self.assertEqual((trade.outcome, trade.q), ("YES", D("0.60")))

    def test_no_side_uses_one_minus_p_high(self) -> None:
        result = decide(
            inputs(p=("0.10", "0.15", "0.20"), yes=side("YES", ("0.30", "50")),
                   no=side("NO", ("0.60", "50"))),
            BOUNDS,
        )
        assert isinstance(result, Trade), result
        self.assertEqual((result.outcome, result.q, result.edge), ("NO", D("0.80"), D("0.20")))

    def test_the_side_with_the_larger_realized_edge_wins(self) -> None:
        result = decide(
            inputs(p=("0.50", "0.50", "0.50"), yes=side("YES", ("0.40", "50")),
                   no=side("NO", ("0.30", "50"))),
            MID,
        )
        assert isinstance(result, Trade), result
        self.assertEqual(result.outcome, "NO")

    def test_fees_count_against_the_edge(self) -> None:
        politics = {"exponent": 1, "rate": 0.04, "takerOnly": True, "rebateRate": 0.25}
        yes = side("YES", ("0.50", "50"), fees_enabled=True, fee_schedule=politics)
        # q 0.555 - ask 0.50 - fee 0.04*0.5*0.5 (0.01) = 0.045 < 0.05.
        self.assertEqual(
            decide(inputs(p=("0.555", "0.6", "0.7"), yes=yes), BOUNDS).reason,  # type: ignore[union-attr]
            "NO_EDGE",
        )
        trade = decide(inputs(yes=yes), BOUNDS)
        assert isinstance(trade, Trade), trade
        self.assertEqual(trade.walk.fee, trade.walk.shares * D("0.01"))
        self.assertLessEqual(trade.walk.cost, trade.budget)


class BookTests(unittest.TestCase):
    def test_walk_stops_at_the_slippage_limit(self) -> None:
        yes = side("YES", ("0.52", "100"), ("0.50", "10"))
        trade = decide(inputs(yes=yes), BOUNDS)  # limit 0.50 * 1.02 = 0.51
        assert isinstance(trade, Trade), trade
        self.assertEqual(trade.walk.shares, D("10"))

    def test_minimum_order_size_is_met_in_shares_and_in_dollars(self) -> None:
        # Budget $4 at 0.50 buys 8 shares: >= 5 shares but < $5.
        tight = replace(BOUNDS, cap_market=D("0.004"))
        self.assertEqual(decide(inputs(), tight).reason, "BELOW_MIN_ORDER")  # type: ignore[union-attr]
        free = side("YES", ("0.50", "50"), min_order_size=D("0"))
        self.assertIsInstance(decide(inputs(yes=free), tight), Trade)

    def test_book_problems_refuse(self) -> None:
        cases = {
            "NO_DEPTH": side("YES"),
            "TICK_MISMATCH": side("YES", ("0.505", "50")),
        }
        for reason, yes in cases.items():
            with self.subTest(reason):
                result = decide(inputs(yes=yes), BOUNDS)
                self.assertIsInstance(result, Refusal)
                self.assertIn(reason, result.detail)  # type: ignore[union-attr]

    def test_stale_or_future_books_refuse(self) -> None:
        for fetched in (NOW - timedelta(seconds=121), NOW + timedelta(seconds=1)):
            with self.subTest(fetched=fetched):
                no = side("NO", ("0.45", "50"), fetched_at=fetched)
                self.assertEqual(decide(inputs(no=no), BOUNDS).reason, "STALE_BOOK")  # type: ignore[union-attr]

    def test_fees_enabled_without_a_rate_refuses(self) -> None:
        yes = side("YES", ("0.50", "50"), fees_enabled=True, fee_schedule=None)
        self.assertEqual(decide(inputs(yes=yes), BOUNDS).reason, "FEE_UNKNOWN")  # type: ignore[union-attr]


class GateTests(unittest.TestCase):
    def test_each_gate_refuses_with_its_code(self) -> None:
        forecast = inputs().forecast
        cases = {
            "ABSTAINED": inputs(forecast=replace(
                forecast, abstained=True, p_low=None, p_mid=None, p_high=None, confidence=None
            )),
            "RULES_CHANGED": inputs(current_rules_hash="r2"),
            "ELIGIBILITY_LOST": inputs(eligible=False),
            "RESOLUTION_STARTED": inputs(resolution_started=True),
            "ALREADY_TRADED": inputs(already_traded=True),
            "LOW_CONFIDENCE": inputs(forecast=replace(forecast, confidence="low")),
            "CLOSING_SOON": inputs(forecast=replace(
                forecast, end_date=NOW + timedelta(hours=47)
            )),
            "NO_BOOK": inputs(books={"YES": side("YES", ("0.5", "50"))}),
        }
        for reason, case in cases.items():
            with self.subTest(reason):
                self.assertEqual(decide(case, BOUNDS).reason, reason)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()

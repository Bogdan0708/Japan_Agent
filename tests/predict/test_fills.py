from __future__ import annotations

import unittest
from decimal import Decimal

from predict_agent.fills import (
    AskLevel,
    FillError,
    fee_per_share,
    level_fee,
    round_shares,
    walk_asks,
)
from predict_agent.tickets import Fill

D = Decimal
TICK = D("0.01")


def book(*levels: tuple[str, str]) -> list[AskLevel]:
    return [AskLevel(D(price), D(size)) for price, size in levels]


class FeeTests(unittest.TestCase):
    def test_fees_are_summed_per_level_not_at_the_average_price(self) -> None:
        # Spec §8: 20 @ 0.50 + 20 @ 0.51 at rate 0.05 costs 0.49990 in fees, not 0.49995.
        walk = walk_asks(
            book(("0.50", "20"), ("0.51", "20")),
            budget=D("100"),
            fee_rate=D("0.05"),
            limit_price=D("0.99"),
            tick_size=TICK,
        )
        self.assertEqual(walk.shares, D("40"))
        self.assertEqual(walk.fee, D("0.49990"))
        at_average = level_fee(D("40"), walk.average_price, D("0.05"))
        self.assertEqual(at_average, D("0.49995"))
        self.assertEqual(walk.notional, D("20.20"))
        self.assertEqual(walk.cost, D("20.69990"))

    def test_fee_free_book_has_zero_fee(self) -> None:
        self.assertEqual(fee_per_share(D("0.4"), D("0")), D("0"))


class WalkTests(unittest.TestCase):
    def test_spec_worked_example_fills_forty_shares_from_a_fifty_share_book(self) -> None:
        # Spec §8: budget capped to $20 at ask 0.50 buys 40 of the 50 recorded shares.
        walk = walk_asks(book(("0.50", "50")), budget=D("20"), fee_rate=D("0"),
                         limit_price=D("0.51"), tick_size=TICK)
        self.assertEqual(walk.fills, (Fill(D("0.50"), D("40")),))
        self.assertEqual(walk.cost, D("20"))

    def test_multi_level_walk_from_worst_first_input(self) -> None:
        # The live API serves asks worst-first; the walk must start at the best price.
        walk = walk_asks(book(("0.43", "100"), ("0.42", "10"), ("0.41", "5")),
                         budget=D("6.2543"), fee_rate=D("0"), limit_price=D("0.43"),
                         tick_size=TICK)
        self.assertEqual(
            walk.fills,
            (Fill(D("0.41"), D("5")), Fill(D("0.42"), D("10")), Fill(D("0.43"), D("0.01"))),
        )
        self.assertEqual(walk.cost, D("6.2543"))

    def test_never_walks_past_the_limit_price_or_the_recorded_depth(self) -> None:
        walk = walk_asks(book(("0.40", "10"), ("0.45", "1000")), budget=D("1000"),
                         fee_rate=D("0"), limit_price=D("0.408"), tick_size=TICK)
        self.assertEqual(walk.fills, (Fill(D("0.40"), D("10")),))
        thin = walk_asks(book(("0.40", "3.5")), budget=D("1000"), fee_rate=D("0"),
                         limit_price=D("0.99"), tick_size=TICK)
        self.assertEqual(thin.shares, D("3.5"))

    def test_shares_round_down_to_the_share_precision(self) -> None:
        self.assertEqual(round_shares(D("2.999")), D("2.99"))
        walk = walk_asks(book(("0.30", "100")), budget=D("1"), fee_rate=D("0"),
                         limit_price=D("0.99"), tick_size=TICK)
        self.assertEqual(walk.shares, D("3.33"))  # 1 / 0.30 = 3.333...

    def test_budget_below_one_share_unit_buys_nothing(self) -> None:
        walk = walk_asks(book(("0.50", "10")), budget=D("0.004"), fee_rate=D("0"),
                         limit_price=D("0.99"), tick_size=TICK)
        self.assertEqual((walk.fills, walk.shares), ((), D("0")))

    def test_bad_books_are_refused_with_a_code(self) -> None:
        cases = {
            "TICK_MISMATCH": (book(("0.505", "10")), TICK),
            "BAD_BOOK": (book(("0.50", "0")), TICK),
        }
        for code, (levels, tick) in cases.items():
            with self.subTest(code), self.assertRaises(FillError) as caught:
                walk_asks(levels, budget=D("1"), fee_rate=D("0"), limit_price=D("0.99"),
                          tick_size=tick)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(FillError) as caught:
            walk_asks(book(("0.5", "1")), budget=D("1"), fee_rate=D("1"),
                      limit_price=D("0.99"), tick_size=TICK)
        self.assertEqual(caught.exception.code, "FEE_UNKNOWN")


if __name__ == "__main__":
    unittest.main()

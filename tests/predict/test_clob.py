from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from predict_agent.clob import book_refusal, parse_book, snapshot_hash
from predict_agent.gamma import ParseError
from tests.predict.fixtures import CONDITION_ID, NOW, YES_TOKEN, clob_book


def parsed(**overrides: object):  # type: ignore[no-untyped-def]
    return parse_book(clob_book(**overrides), fetched_at=NOW + timedelta(seconds=1))


class ParseBookTests(unittest.TestCase):
    def test_sorts_both_sides_best_first(self) -> None:
        book = parsed()
        self.assertEqual([lvl.price for lvl in book.bids][0], Decimal("0.36"))
        expected_asks = [Decimal(p) for p in ("0.37", "0.38", "0.45")]
        self.assertEqual([lvl.price for lvl in book.asks], expected_asks)
        self.assertEqual(book.best_bid, Decimal("0.36"))
        self.assertEqual(book.best_ask, Decimal("0.37"))

    def test_observed_and_fetched_times_are_distinct(self) -> None:
        book = parsed()
        self.assertEqual(book.observed_at, NOW)
        self.assertEqual(book.fetched_at, NOW + timedelta(seconds=1))

    def test_invalid_levels_rejected(self) -> None:
        bad_levels = (
            {"price": "1.2", "size": "1"},
            {"price": "0.5", "size": "0"},
            {"price": "x", "size": "1"},
        )
        for bad in bad_levels:
            with self.subTest(bad=bad), self.assertRaises(ParseError):
                parsed(bids=[bad])

    def test_missing_timestamp_rejected(self) -> None:
        raw = clob_book()
        del raw["timestamp"]
        with self.assertRaises(ParseError):
            parse_book(raw, fetched_at=NOW)


class RefusalTests(unittest.TestCase):
    def test_fresh_book_has_no_refusal(self) -> None:
        self.assertIsNone(book_refusal(parsed(), CONDITION_ID, YES_TOKEN))

    def test_quiet_book_with_old_last_change_is_accepted(self) -> None:
        quiet = parse_book(clob_book(), fetched_at=NOW + timedelta(hours=6))
        self.assertIsNone(book_refusal(quiet, CONDITION_ID, YES_TOKEN))

    def test_refusals(self) -> None:
        skewed = parse_book(clob_book(), fetched_at=NOW - timedelta(seconds=6))
        cases = {
            "BOOK_MISMATCH": parsed(asset_id="999"),
            "CLOCK_SKEW": skewed,
            "EMPTY_SIDE": parsed(asks=[]),
            "CROSSED_BOOK": parsed(bids=[{"price": "0.40", "size": "5"}]),
        }
        for reason, book in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(book_refusal(book, CONDITION_ID, YES_TOKEN), reason)

    def test_snapshot_hash_is_stable_and_content_bound(self) -> None:
        self.assertEqual(snapshot_hash(parsed()), snapshot_hash(parsed()))
        self.assertNotEqual(snapshot_hash(parsed()), snapshot_hash(parsed(hash="other")))


class NonFiniteBookTests(unittest.TestCase):
    def test_nan_price_is_parse_error(self) -> None:
        with self.assertRaises(ParseError):
            parsed(asks=[{"price": "NaN", "size": "5"}])


class BookValidationTests(unittest.TestCase):
    def test_non_positive_tick_or_min_size_rejected(self) -> None:
        for overrides in ({"tick_size": "0"}, {"min_order_size": "-2"}):
            with self.subTest(overrides=overrides), self.assertRaises(ParseError):
                parsed(**overrides)

    def test_zero_min_order_size_is_accepted(self) -> None:
        # Observed live 2026-10-05: 13 liquid markets serve min_order_size "0" (Gamma null).
        self.assertEqual(parsed(min_order_size="0").min_order_size, Decimal("0"))

    def test_out_of_range_timestamp_is_parse_error(self) -> None:
        for stamp in ("9" * 30, "-5", "0"):
            with self.subTest(stamp=stamp), self.assertRaises(ParseError):
                parsed(timestamp=stamp)


if __name__ == "__main__":
    unittest.main()

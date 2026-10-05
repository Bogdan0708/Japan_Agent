from __future__ import annotations

import json
import unittest

from tests.predict.fixtures import clob_book, gamma_event, gamma_market, resolution_row


class FixtureContractTests(unittest.TestCase):
    """Pins the live API shapes observed on 2026-10-05 (see plan 'Contract facts')."""

    def test_gamma_market_encodes_lists_as_json_strings(self) -> None:
        market = gamma_market()
        self.assertIsInstance(market["outcomes"], str)
        self.assertEqual(json.loads(market["outcomes"]), ["Yes", "No"])
        self.assertEqual(len(json.loads(market["clobTokenIds"])), 2)
        self.assertEqual(market["version"], "v1")
        self.assertIsNone(market["category"])

    def test_gamma_event_tags_carry_slugs(self) -> None:
        event = gamma_event([gamma_market()], tags=("politics", "geopolitics"))
        self.assertEqual([tag["slug"] for tag in event["tags"]], ["politics", "geopolitics"])

    def test_clob_book_arrives_worst_first_with_ms_timestamp(self) -> None:
        book = clob_book()
        bids = [level["price"] for level in book["bids"]]
        asks = [level["price"] for level in book["asks"]]
        self.assertEqual(bids, sorted(bids))
        self.assertEqual(asks, sorted(asks, reverse=True))
        self.assertEqual(len(book["timestamp"]), 13)

    def test_resolved_row_has_price_but_no_payouts(self) -> None:
        row = resolution_row(status="resolved", price="0")
        self.assertNotIn("payouts", row)
        self.assertNotIn("resolved_at", row)
        self.assertEqual(row["proposed_price"], "69")


if __name__ == "__main__":
    unittest.main()

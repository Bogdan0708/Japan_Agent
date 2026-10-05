from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from predict_agent.config import load_discovery_config
from predict_agent.gamma import (
    ParseError,
    category_for,
    eligibility_refusal,
    fee_rate,
    fetch_events,
    parse_market,
    rules_hash,
)
from predict_agent.http import JsonClient
from tests.predict.fakes import ScriptedOpener
from tests.predict.fixtures import NOW, YES_TOKEN, gamma_event, gamma_market

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "predict-policy.example.json"
CONFIG, _ = load_discovery_config(EXAMPLE)


def candidate(tags: tuple[str, ...] = ("politics",), **overrides: object):  # type: ignore[no-untyped-def]
    market = gamma_market(**overrides)
    return parse_market(market, gamma_event([market], tags=tags))


class ParseTests(unittest.TestCase):
    def test_parses_json_string_fields_and_decimals(self) -> None:
        c = candidate()
        self.assertEqual(c.outcomes, ("Yes", "No"))
        self.assertEqual(c.token_ids[0], YES_TOKEN)
        self.assertEqual(c.best_ask, Decimal("0.37"))
        self.assertEqual(c.liquidity, Decimal("76109.0796"))
        self.assertEqual(c.tag_slugs, ("politics",))

    def test_missing_condition_id_is_parse_error(self) -> None:
        market = gamma_market()
        del market["conditionId"]
        with self.assertRaises(ParseError):
            parse_market(market, gamma_event([market]))

    def test_non_list_outcomes_is_parse_error(self) -> None:
        with self.assertRaises(ParseError):
            candidate(outcomes='{"a": 1}')

    def test_event_level_neg_risk_propagates(self) -> None:
        market = gamma_market()
        event = gamma_event([market])
        event["negRisk"] = True
        self.assertTrue(parse_market(market, event).neg_risk)


class CategoryAndFeeTests(unittest.TestCase):
    def test_category_precedence_follows_config_order(self) -> None:
        self.assertEqual(category_for(("politics", "geopolitics"), CONFIG), "geopolitics")
        self.assertIsNone(category_for(("sports",), CONFIG))

    def test_fee_rate_requires_exponent_one_and_taker_only(self) -> None:
        good = {"exponent": 1, "rate": 0.04, "takerOnly": True, "rebateRate": 0.25}
        self.assertEqual(fee_rate(good), Decimal("0.04"))
        self.assertIsNone(fee_rate({**good, "exponent": 2}))
        self.assertIsNone(fee_rate({**good, "takerOnly": False}))
        self.assertIsNone(fee_rate({**good, "rate": "abc"}))
        self.assertIsNone(fee_rate(None))


class EligibilityTests(unittest.TestCase):
    def test_eligible_market_has_no_refusal(self) -> None:
        self.assertIsNone(eligibility_refusal(candidate(), CONFIG, NOW))

    def test_refusal_reasons(self) -> None:
        cases = {
            "NOT_OPEN": {"closed": True},
            "UNSUPPORTED_VERSION": {"version": "v2"},
            "NEG_RISK": {"negRisk": True},
            "NON_BINARY": {"outcomes": '["A", "B", "C"]'},
            "NO_RULES": {"description": "   "},
            "LOW_LIQUIDITY": {"liquidityNum": 10},
            "NO_QUOTE": {"bestAsk": None},
            "OUTCOME_KNOWN": {"bestAsk": 0.99, "bestBid": 0.985},
            "FEE_UNKNOWN": {"feeSchedule": None},
        }
        for reason, overrides in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(eligibility_refusal(candidate(**overrides), CONFIG, NOW), reason)

    def test_not_accepting_orders_is_not_open(self) -> None:
        self.assertEqual(
            eligibility_refusal(candidate(acceptingOrders=False), CONFIG, NOW), "NOT_OPEN"
        )

    def test_low_bid_means_no_side_outcome_known(self) -> None:
        refusal = eligibility_refusal(candidate(bestBid=0.01, bestAsk=0.02), CONFIG, NOW)
        self.assertEqual(refusal, "OUTCOME_KNOWN")

    def test_unmapped_tag_has_no_category(self) -> None:
        self.assertEqual(eligibility_refusal(candidate(("sports",)), CONFIG, NOW), "NO_CATEGORY")

    def test_end_window_bounds(self) -> None:
        soon = (NOW + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        far = (NOW + timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for end in (soon, far):
            with self.subTest(end=end):
                self.assertEqual(
                    eligibility_refusal(candidate(endDate=end), CONFIG, NOW), "END_WINDOW"
                )
        self.assertEqual(eligibility_refusal(candidate(endDate=None), CONFIG, NOW), "END_WINDOW")

    def test_fee_free_market_without_schedule_is_eligible(self) -> None:
        c = candidate(("geopolitics",), feesEnabled=False, feeSchedule=None)
        self.assertIsNone(eligibility_refusal(c, CONFIG, NOW))


class RulesHashTests(unittest.TestCase):
    def test_rules_hash_changes_with_description_only(self) -> None:
        base = rules_hash(candidate())
        self.assertEqual(base, rules_hash(candidate(bestAsk=0.5)))
        self.assertNotEqual(base, rules_hash(candidate(description="Clarified wording.")))


class FetchEventsTests(unittest.TestCase):
    def test_paginates_with_offset_and_dedupes_across_tags(self) -> None:
        page_size = CONFIG.page_size
        first_page = [gamma_event([], event_id=str(i)) for i in range(page_size)]
        last_page = [gamma_event([], event_id="last")]
        duplicate = [gamma_event([], event_id="0")]
        opener = ScriptedOpener([first_page, last_page, [], duplicate])
        events = fetch_events(JsonClient(opener=opener, sleep=lambda _: None), CONFIG)
        self.assertEqual(len(events), page_size + 1)
        self.assertIn("offset=100", opener.requests[1])
        self.assertIn("tag_slug=geopolitics", opener.requests[0])
        self.assertIn("tag_slug=economics", opener.requests[2])

    def test_non_list_response_is_parse_error(self) -> None:
        opener = ScriptedOpener([{"error": "x"}])
        with self.assertRaises(ParseError):
            fetch_events(JsonClient(opener=opener, sleep=lambda _: None), CONFIG)


if __name__ == "__main__":
    unittest.main()

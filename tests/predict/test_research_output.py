from __future__ import annotations

import re
import unittest
from decimal import Decimal
from typing import Any

from predict_agent.research.exposure import scan
from predict_agent.research.schema import (
    DECIMAL_PATTERN,
    OUTPUT_SCHEMA,
    OutputError,
    parse_output,
)

FETCHED = frozenset({"https://www.reuters.com/a", "https://apnews.com/b"})


def forecast(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "abstain": False,
        "abstain_reason": None,
        "p_low": "0.55",
        "p_mid": "0.60",
        "p_high": "0.70",
        "confidence": "medium",
        "base_rate": "0.30",
        "rules_interpretation": "Resolves on the official count.",
        "evidence": [{"claim": "Polls lead by 5", "url": "https://www.reuters.com/a"}],
    }
    raw.update(changes)
    return raw


class SchemaTests(unittest.TestCase):
    def test_schema_requires_every_property(self) -> None:
        self.assertEqual(set(OUTPUT_SCHEMA["required"]), set(OUTPUT_SCHEMA["properties"]))
        self.assertIs(OUTPUT_SCHEMA["additionalProperties"], False)

    def test_probability_fields_carry_the_decimal_pattern(self) -> None:
        for key in ("p_low", "p_mid", "p_high", "base_rate"):
            self.assertEqual(OUTPUT_SCHEMA["properties"][key]["pattern"], DECIMAL_PATTERN, key)
            self.assertEqual(OUTPUT_SCHEMA["properties"][key]["type"], ["string", "null"], key)

    def test_decimal_pattern_accepts_decimals_and_rejects_prose(self) -> None:
        pattern = re.compile(DECIMAL_PATTERN)
        for good in ("0.07", "0", "1", "0.5", "1.0"):
            self.assertIsNotNone(pattern.search(good), good)
        for bad in ("5-10%", "0.07 (approx)", ".5", "1.5", "07"):
            self.assertIsNone(pattern.search(bad), bad)

    def test_valid_forecast_parses_to_exact_decimals(self) -> None:
        parsed = parse_output(forecast(), FETCHED)
        self.assertEqual((parsed.p_low, parsed.p_mid, parsed.p_high),
                         (Decimal("0.55"), Decimal("0.60"), Decimal("0.70")))
        self.assertEqual(parsed.base_rate, Decimal("0.30"))
        self.assertEqual(parsed.evidence, (("Polls lead by 5", "https://www.reuters.com/a"),))

    def test_abstention_parses_without_probabilities(self) -> None:
        parsed = parse_output(forecast(abstain=True, abstain_reason="rules ambiguous",
                                       p_low=None, p_mid=None, p_high=None, confidence=None,
                                       base_rate=None, evidence=[]), FETCHED)
        self.assertTrue(parsed.abstained)
        self.assertIsNone(parsed.p_mid)

    def test_citation_must_be_a_url_fetched_this_session(self) -> None:
        with self.assertRaises(OutputError) as caught:
            parse_output(forecast(evidence=[{"claim": "c", "url": "https://other.org/x"}]),
                         FETCHED)
        self.assertEqual(caught.exception.code, "UNFETCHED_CITATION")

    def test_invalid_outputs_are_schema_invalid(self) -> None:
        cases = {
            "not an object": "text",
            "extra key": {**forecast(), "price": "0.4"},
            "float probability": forecast(p_mid=0.6),
            "probability above 0.99": forecast(p_high="0.995"),
            "nan": forecast(p_low="NaN"),
            "unordered": forecast(p_low="0.75"),
            "bad confidence": forecast(confidence="certain"),
            "missing base rate": forecast(base_rate=None),
            "base rate above 1": forecast(base_rate="1.2"),
            "no evidence": forecast(evidence=[]),
            "empty claim": forecast(evidence=[{"claim": " ", "url": "https://apnews.com/b"}]),
            "empty interpretation": forecast(rules_interpretation=" "),
            "abstain with probabilities": forecast(abstain=True, abstain_reason="x"),
            "abstain without reason": forecast(abstain=True, abstain_reason=None, p_low=None,
                                               p_mid=None, p_high=None, confidence=None),
            "reason without abstain": forecast(abstain_reason="x"),
        }
        for label, raw in cases.items():
            with self.subTest(label), self.assertRaises(OutputError) as caught:
                parse_output(raw, FETCHED)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID", label)


class ExposureTests(unittest.TestCase):
    def test_venue_names_and_odds_phrasing_are_flagged(self) -> None:
        flags = scan([
            "Traders on Polymarket give it 62%.",
            "Kalshi lists the contract at 40 cents a share.",
            "The betting odds shortened overnight; bookmakers' odds now 2/1.",
        ])
        for flag in ("venue:polymarket", "venue:kalshi", "phrase:traders_price",
                     "phrase:betting_odds", "phrase:cents_per_share"):
            self.assertIn(flag, flags)
        self.assertEqual(list(flags), sorted(set(flags)))

    def test_clean_reporting_is_not_flagged(self) -> None:
        self.assertEqual(
            scan(["The senate vote is scheduled for Tuesday; 51 senators support it.",
                  "Manifold of a car engine is unrelated."]),
            (),
        )


if __name__ == "__main__":
    unittest.main()

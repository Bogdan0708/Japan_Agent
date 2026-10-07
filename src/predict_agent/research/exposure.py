"""Price-exposure detection over everything the model saw (spec §6).

Detection is best-effort: a clean scan means "no detected price exposure", never "proven
blind". The scanner looks for prediction-market and betting venue names and for phrasing
that reports market odds or crowd probabilities. Flags are stored on the forecast; flagged
forecasts are reported separately (Plan 5)."""

from __future__ import annotations

import re
from collections.abc import Iterable

VENUES = (
    "polymarket",
    "kalshi",
    "manifold markets",
    "manifold.markets",
    "metaculus",
    "predictit",
    "betfair",
    "smarkets",
    "oddschecker",
    "good judgment open",
    "insight prediction",
)
_PHRASES: dict[str, re.Pattern[str]] = {
    "prediction_market": re.compile(r"prediction[- ]markets?", re.I),
    "betting_odds": re.compile(r"\b(?:betting|bookmakers?'?|bookies'?)\s+odds\b", re.I),
    "odds_of": re.compile(r"\bodds\s+(?:of|on|for)\b", re.I),
    "implied_probability": re.compile(r"implied\s+(?:probability|odds|chance)", re.I),
    "traders_price": re.compile(
        r"\btraders?\b(?:\s+\w+){0,4}?\s+(?:give|gives|price|prices|see|sees|put|puts|bet|bets)\b",
        re.I,
    ),
    "market_chance": re.compile(r"\bmarkets?\s+(?:give|gives|price|prices|put|puts|see|sees)\b",
                                re.I),
    "percent_chance_market": re.compile(
        r"\b\d{1,3}(?:\.\d+)?\s*%\s+(?:chance|probability)\s+(?:on|at|according to)\b", re.I
    ),
    "cents_per_share": re.compile(r"\b\d{1,2}\s*(?:¢|cents?)\s+(?:a|per)\s+share\b", re.I),
}


def scan(texts: Iterable[str]) -> tuple[str, ...]:
    """Sorted, de-duplicated flags such as 'venue:polymarket' or 'phrase:odds_of'."""
    flags: set[str] = set()
    for text in texts:
        lowered = text.lower()
        for venue in VENUES:
            if venue in lowered:
                flags.add(f"venue:{venue.replace(' ', '_')}")
        for name, pattern in _PHRASES.items():
            if pattern.search(text):
                flags.add(f"phrase:{name}")
    return tuple(sorted(flags))

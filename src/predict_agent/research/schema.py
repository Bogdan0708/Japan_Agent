"""The forecast output schema and its validation (spec §6: strict JSON schema, citations
must be URLs actually fetched in that session).

Probabilities travel as decimal strings so they never pass through float. The schema
keeps to features structured outputs support (no numeric or string-length constraints);
ranges and ordering are checked here."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

P_MIN = Decimal("0.01")
P_MAX = Decimal("0.99")
CONFIDENCE = ("low", "medium", "high")

_NULLABLE_STRING = {"type": ["string", "null"]}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "abstain",
        "abstain_reason",
        "p_low",
        "p_mid",
        "p_high",
        "confidence",
        "base_rate",
        "rules_interpretation",
        "evidence",
    ],
    "properties": {
        "abstain": {"type": "boolean"},
        "abstain_reason": _NULLABLE_STRING,
        "p_low": _NULLABLE_STRING,
        "p_mid": _NULLABLE_STRING,
        "p_high": _NULLABLE_STRING,
        "confidence": {"type": ["string", "null"], "enum": ["low", "medium", "high", None]},
        "base_rate": _NULLABLE_STRING,
        "rules_interpretation": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim", "url"],
                "properties": {"claim": {"type": "string"}, "url": {"type": "string"}},
            },
        },
    },
}


class OutputError(ValueError):
    """The model's output cannot become a forecast. `code` is the refusal reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class ParsedForecast:
    abstained: bool
    abstain_reason: str | None
    p_low: Decimal | None
    p_mid: Decimal | None
    p_high: Decimal | None
    confidence: str | None
    base_rate: Decimal | None
    rules_interpretation: str
    evidence: tuple[tuple[str, str], ...]  # (claim, url)


def _probability(raw: Mapping[str, Any], key: str, low: Decimal, high: Decimal) -> Decimal:
    value = raw.get(key)
    if not isinstance(value, str):
        raise OutputError("SCHEMA_INVALID", f"{key} must be a decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise OutputError("SCHEMA_INVALID", f"{key} {value!r} is not a decimal") from None
    if not number.is_finite() or not low <= number <= high:
        raise OutputError("SCHEMA_INVALID", f"{key} {value} outside [{low}, {high}]")
    return number


def parse_output(raw: object, fetched_urls: frozenset[str]) -> ParsedForecast:
    """Validate the structured output. Raises OutputError: SCHEMA_INVALID for anything
    malformed, UNFETCHED_CITATION for evidence citing a URL not fetched this session."""
    if not isinstance(raw, Mapping):
        raise OutputError("SCHEMA_INVALID", "output is not an object")
    if set(raw) != set(OUTPUT_SCHEMA["required"]):
        raise OutputError("SCHEMA_INVALID", f"unexpected keys {sorted(raw)}")
    interpretation = raw["rules_interpretation"]
    if not isinstance(interpretation, str) or not interpretation.strip():
        raise OutputError("SCHEMA_INVALID", "rules_interpretation is required")
    evidence_raw = raw["evidence"]
    if not isinstance(evidence_raw, list):
        raise OutputError("SCHEMA_INVALID", "evidence must be a list")
    evidence: list[tuple[str, str]] = []
    for item in evidence_raw:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"claim", "url"}
            or not isinstance(item["claim"], str)
            or not isinstance(item["url"], str)
            or not item["claim"].strip()
        ):
            raise OutputError("SCHEMA_INVALID", "evidence items need a claim and a url")
        if item["url"] not in fetched_urls:
            raise OutputError("UNFETCHED_CITATION", f"{item['url']} was not fetched")
        evidence.append((item["claim"], item["url"]))
    if raw["abstain"] is True:
        reason = raw["abstain_reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise OutputError("SCHEMA_INVALID", "an abstention needs a reason")
        if any(raw[key] is not None for key in ("p_low", "p_mid", "p_high", "confidence")):
            raise OutputError("SCHEMA_INVALID", "an abstention carries no probabilities")
        return ParsedForecast(True, reason, None, None, None, None, None, interpretation,
                              tuple(evidence))
    if raw["abstain"] is not False or raw["abstain_reason"] is not None:
        raise OutputError("SCHEMA_INVALID", "abstain must be false with no abstain_reason")
    p_low = _probability(raw, "p_low", P_MIN, P_MAX)
    p_mid = _probability(raw, "p_mid", P_MIN, P_MAX)
    p_high = _probability(raw, "p_high", P_MIN, P_MAX)
    if not p_low <= p_mid <= p_high:
        raise OutputError("SCHEMA_INVALID", "probabilities must satisfy p_low <= p_mid <= p_high")
    if raw["confidence"] not in CONFIDENCE:
        raise OutputError("SCHEMA_INVALID", f"confidence must be one of {CONFIDENCE}")
    base_rate = _probability(raw, "base_rate", Decimal("0"), Decimal("1"))
    if not evidence:
        raise OutputError("SCHEMA_INVALID", "a forecast needs at least one cited evidence item")
    return ParsedForecast(
        False,
        None,
        p_low,
        p_mid,
        p_high,
        raw["confidence"],
        base_rate,
        interpretation,
        tuple(evidence),
    )

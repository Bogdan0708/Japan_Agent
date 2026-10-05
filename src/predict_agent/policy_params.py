"""Paper-policy parameters (spec §5).

The human-owned config file holds one `policy` section. Each cohort freezes it as one
policy artifact per portfolio variant: `primary` prices sides from the conservative bounds
(q_YES = p_low, q_NO = 1 - p_high) and the pre-registered shadow `shadow_mid` from p_mid.
Decisions always read the portfolio's frozen artifact, never the config file."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .config import ConfigError
from .util import canonical_json

PROBABILITY_SOURCES = ("bounds", "mid")
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
VARIANT_SOURCES = {"primary": "bounds", "shadow_mid": "mid"}

_DECIMAL_FIELDS = (
    "starting_bankroll",
    "min_edge",
    "kelly_fraction",
    "max_slippage",
)
_CAP_FIELDS = ("market", "event", "category", "total_open")
_INT_FIELDS = ("min_hours_to_close", "max_book_age_seconds")
_CONFIG_KEYS = frozenset({*_DECIMAL_FIELDS, *_INT_FIELDS, "min_confidence", "caps"})


@dataclass(frozen=True)
class PolicyParams:
    probability: str  # "bounds" | "mid"
    starting_bankroll: Decimal
    min_edge: Decimal
    min_confidence: str
    kelly_fraction: Decimal
    cap_market: Decimal
    cap_event: Decimal
    cap_category: Decimal
    cap_total_open: Decimal
    max_slippage: Decimal
    min_hours_to_close: int
    max_book_age_seconds: int

    def record(self) -> dict[str, Any]:
        return {
            "probability": self.probability,
            "starting_bankroll": format(self.starting_bankroll, "f"),
            "min_edge": format(self.min_edge, "f"),
            "min_confidence": self.min_confidence,
            "kelly_fraction": format(self.kelly_fraction, "f"),
            "caps": {
                "market": format(self.cap_market, "f"),
                "event": format(self.cap_event, "f"),
                "category": format(self.cap_category, "f"),
                "total_open": format(self.cap_total_open, "f"),
            },
            "max_slippage": format(self.max_slippage, "f"),
            "min_hours_to_close": self.min_hours_to_close,
            "max_book_age_seconds": self.max_book_age_seconds,
        }


def _decimal(raw: Mapping[str, Any], key: str, label: str) -> Decimal:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"{label}.{key} must be a decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ConfigError(f"{label}.{key} must be a decimal string") from None
    if not number.is_finite():
        raise ConfigError(f"{label}.{key} must be finite")
    return number


def _fraction(raw: Mapping[str, Any], key: str, label: str) -> Decimal:
    number = _decimal(raw, key, label)
    if not Decimal("0") < number <= Decimal("1"):
        raise ConfigError(f"{label}.{key} must be in (0, 1]")
    return number


def _positive_int(raw: Mapping[str, Any], key: str, label: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{label}.{key} must be a positive integer")
    return value


def parse_policy(raw: object, probability: str, label: str = "policy") -> PolicyParams:
    """Strict: every key required, no unknown keys, every value range-checked."""
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{label} must be an object")
    if probability not in PROBABILITY_SOURCES:
        raise ConfigError(f"{label}.probability must be one of {PROBABILITY_SOURCES}")
    keys = set(raw) - {"probability"}
    if keys != _CONFIG_KEYS:
        missing = sorted(_CONFIG_KEYS - keys)
        unknown = sorted(keys - _CONFIG_KEYS)
        raise ConfigError(f"{label}: missing keys {missing}, unknown keys {unknown}")
    caps = raw["caps"]
    if not isinstance(caps, Mapping) or set(caps) != set(_CAP_FIELDS):
        raise ConfigError(f"{label}.caps must have exactly {list(_CAP_FIELDS)}")
    bankroll = _decimal(raw, "starting_bankroll", label)
    if bankroll <= 0:
        raise ConfigError(f"{label}.starting_bankroll must be positive")
    min_edge = _decimal(raw, "min_edge", label)
    if not Decimal("0") < min_edge < Decimal("1"):
        raise ConfigError(f"{label}.min_edge must be in (0, 1)")
    max_slippage = _decimal(raw, "max_slippage", label)
    if not Decimal("0") <= max_slippage < Decimal("1"):
        raise ConfigError(f"{label}.max_slippage must be in [0, 1)")
    confidence = raw["min_confidence"]
    if confidence not in CONFIDENCE_RANK:
        raise ConfigError(f"{label}.min_confidence must be one of {list(CONFIDENCE_RANK)}")
    return PolicyParams(
        probability=probability,
        starting_bankroll=bankroll,
        min_edge=min_edge,
        min_confidence=confidence,
        kelly_fraction=_fraction(raw, "kelly_fraction", label),
        cap_market=_fraction(caps, "market", f"{label}.caps"),
        cap_event=_fraction(caps, "event", f"{label}.caps"),
        cap_category=_fraction(caps, "category", f"{label}.caps"),
        cap_total_open=_fraction(caps, "total_open", f"{label}.caps"),
        max_slippage=max_slippage,
        min_hours_to_close=_positive_int(raw, "min_hours_to_close", label),
        max_book_age_seconds=_positive_int(raw, "max_book_age_seconds", label),
    )


def load_policy_config(path: Path) -> PolicyParams:
    """The `policy` section of the human-owned config, as the primary (bounds) policy."""
    if not path.exists():
        raise ConfigError(
            f"{path} is missing: copy config/predict-policy.example.json to "
            "config/predict-policy.json and review every value"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path} is not valid JSON") from error
    if not isinstance(raw, dict) or "policy" not in raw:
        raise ConfigError(
            f"{path} has no 'policy' section: copy it from config/predict-policy.example.json "
            "and review every value"
        )
    section = raw["policy"]
    if isinstance(section, Mapping) and "probability" in section:
        raise ConfigError("policy.probability is set per portfolio variant, not in config")
    return parse_policy(section, "bounds")


def variant_policies(params: PolicyParams) -> dict[str, str]:
    """variant -> canonical policy artifact content, for CohortIdentity.portfolios."""
    return {
        variant: canonical_json(replace(params, probability=source).record())
        for variant, source in VARIANT_SOURCES.items()
    }


def policy_from_artifact(content: str) -> PolicyParams:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as error:
        raise ConfigError("policy artifact is not JSON") from error
    if not isinstance(raw, Mapping):
        raise ConfigError("policy artifact must be an object")
    return parse_policy(raw, str(raw.get("probability")), "policy artifact")

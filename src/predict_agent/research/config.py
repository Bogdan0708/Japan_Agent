"""The human-owned `research` section of config/predict-policy.json (spec §6, §7).

The model, `max_turns`, blocked domains, the per-forecast USD budget, baseline window,
scoring version and generation are part of the cohort identity (via `settings_record()`
and `CohortIdentity`), so changing any of them opens a new cohort: the per-forecast
budget caps how much research one forecast may do, so it changes what a forecast means.
The daily USD budget and the daily entry-forecast cap are operational limits and may
change between runs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..config import ConfigError

_KEYS = frozenset(
    {
        "model",
        "max_turns",
        "per_forecast_usd",
        "daily_usd",
        "max_entry_forecasts_per_day",
        "blocked_domains",
        "baseline_window_seconds",
        "scoring_version",
        "generation",
    }
)
_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


@dataclass(frozen=True)
class ResearchConfig:
    model: str
    max_turns: int
    per_forecast_usd: Decimal
    daily_usd: Decimal
    max_entry_forecasts_per_day: int
    blocked_domains: tuple[str, ...]
    baseline_window_seconds: int
    scoring_version: str
    generation: int

    def settings_record(self) -> dict[str, Any]:
        """The research settings frozen into the cohort identity: the per-forecast budget
        is included (Decimal text as configured); no daily budget or volume cap."""
        return {
            "tools": ["WebSearch", "WebFetch"],
            "max_turns": self.max_turns,
            "per_forecast_usd": str(self.per_forecast_usd),
            "blocked_domains": list(self.blocked_domains),
        }


def _positive_int(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"research.{key} must be a positive integer")
    return value


def _usd(raw: Mapping[str, Any], key: str) -> Decimal:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"research.{key} must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ConfigError(f"research.{key} must be a decimal string") from None
    if not amount.is_finite() or amount <= 0:
        raise ConfigError(f"research.{key} must be a positive amount")
    return amount


def parse_research(raw: object) -> ResearchConfig:
    if not isinstance(raw, Mapping):
        raise ConfigError("research must be an object")
    if set(raw) != _KEYS:
        missing = sorted(_KEYS - set(raw))
        unknown = sorted(set(raw) - _KEYS)
        raise ConfigError(f"research: missing keys {missing}, unknown keys {unknown}")
    model = raw["model"]
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("research.model must be a model id")
    scoring_version = raw["scoring_version"]
    if not isinstance(scoring_version, str) or not scoring_version:
        raise ConfigError("research.scoring_version must be a non-empty string")
    domains = raw["blocked_domains"]
    if not isinstance(domains, list) or not domains:
        raise ConfigError("research.blocked_domains must be a non-empty list")
    for domain in domains:
        if not isinstance(domain, str) or not _DOMAIN.fullmatch(domain):
            raise ConfigError(f"research.blocked_domains: {domain!r} is not a bare domain")
    per_forecast = _usd(raw, "per_forecast_usd")
    daily = _usd(raw, "daily_usd")
    if per_forecast > daily:
        raise ConfigError("research.per_forecast_usd must not exceed research.daily_usd")
    return ResearchConfig(
        model=model,
        max_turns=_positive_int(raw, "max_turns"),
        per_forecast_usd=per_forecast,
        daily_usd=daily,
        max_entry_forecasts_per_day=_positive_int(raw, "max_entry_forecasts_per_day"),
        blocked_domains=tuple(sorted(set(domains))),
        baseline_window_seconds=_positive_int(raw, "baseline_window_seconds"),
        scoring_version=scoring_version,
        generation=_positive_int(raw, "generation"),
    )


def load_research_config(path: Path) -> ResearchConfig:
    if not path.exists():
        raise ConfigError(
            f"{path} is missing: copy config/predict-policy.example.json to "
            "config/predict-policy.json and review every value"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path} is not valid JSON") from error
    if not isinstance(raw, dict) or "research" not in raw:
        raise ConfigError(
            f"{path} has no 'research' section: copy it from "
            "config/predict-policy.example.json and review every value"
        )
    return parse_research(raw["research"])


def blocked_host(host: str, blocked: tuple[str, ...]) -> bool:
    """True when `host` is a blocked domain or a subdomain of one."""
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in blocked)

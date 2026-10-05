from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .util import sha256_text


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DiscoveryConfig:
    tag_categories: tuple[tuple[str, str], ...]
    min_liquidity: Decimal
    min_days_to_end: int
    max_days_to_end: int
    known_outcome_threshold: Decimal
    max_book_age_seconds: int
    page_size: int
    max_pages: int


@dataclass(frozen=True)
class Settings:
    root: Path
    database_path: Path
    reports_dir: Path
    policy_path: Path

    @classmethod
    def from_root(cls, root: Path) -> Settings:
        return cls(
            root=root,
            database_path=root / "data" / "predict.sqlite3",
            reports_dir=root / "data" / "reports",
            policy_path=root / "config" / "predict-policy.json",
        )


def _decimal(raw: dict[str, Any], key: str) -> Decimal:
    try:
        return Decimal(str(raw[key]))
    except (KeyError, InvalidOperation) as error:
        raise ConfigError(f"discovery.{key} must be a decimal") from error


def _positive_int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"discovery.{key} must be a positive integer")
    return value


def load_discovery_config(path: Path) -> tuple[DiscoveryConfig, str]:
    if not path.exists():
        raise ConfigError(
            f"{path} is missing: copy config/predict-policy.example.json to "
            "config/predict-policy.json and review every value"
        )
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)["discovery"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ConfigError(f"{path} must be JSON with a 'discovery' object") from error
    pairs = raw.get("tag_categories")
    if not isinstance(pairs, list) or not pairs:
        raise ConfigError("discovery.tag_categories must be a non-empty list")
    tag_categories: list[tuple[str, str]] = []
    for pair in pairs:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(item, str) and item for item in pair)
        ):
            raise ConfigError("discovery.tag_categories entries must be [tag_slug, category]")
        tag_categories.append((pair[0], pair[1]))
    config = DiscoveryConfig(
        tag_categories=tuple(tag_categories),
        min_liquidity=_decimal(raw, "min_liquidity"),
        min_days_to_end=_positive_int(raw, "min_days_to_end"),
        max_days_to_end=_positive_int(raw, "max_days_to_end"),
        known_outcome_threshold=_decimal(raw, "known_outcome_threshold"),
        max_book_age_seconds=_positive_int(raw, "max_book_age_seconds"),
        page_size=_positive_int(raw, "page_size"),
        max_pages=_positive_int(raw, "max_pages"),
    )
    if config.min_days_to_end >= config.max_days_to_end:
        raise ConfigError("discovery.min_days_to_end must be below max_days_to_end")
    if not Decimal("0.5") < config.known_outcome_threshold < Decimal("1"):
        raise ConfigError("discovery.known_outcome_threshold must be in (0.5, 1)")
    if config.min_liquidity < 0:
        raise ConfigError("discovery.min_liquidity must not be negative")
    return config, sha256_text(text)

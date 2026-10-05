from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import DiscoveryConfig
from .http import JsonClient
from .util import isoformat, parse_datetime, sha256_json

GAMMA_URL = "https://gamma-api.polymarket.com"
ELIGIBILITY_REASONS = (
    "NOT_OPEN",
    "UNSUPPORTED_VERSION",
    "NEG_RISK",
    "NON_BINARY",
    "NO_CATEGORY",
    "NO_RULES",
    "END_WINDOW",
    "LOW_LIQUIDITY",
    "NO_QUOTE",
    "OUTCOME_KNOWN",
    "FEE_UNKNOWN",
)


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class MarketCandidate:
    condition_id: str
    event_id: str
    question: str
    rules_text: str
    resolution_source: str
    end_date: datetime | None
    version: str
    closed: bool
    accepting_orders: bool
    neg_risk: bool
    outcomes: tuple[str, ...]
    token_ids: tuple[str, ...]
    tag_slugs: tuple[str, ...]
    liquidity: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    fees_enabled: bool
    fee_schedule: dict[str, Any] | None


def _string_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ParseError(f"invalid JSON list: {error}") from None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ParseError("expected a list of strings")
    return tuple(value)


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ParseError("boolean where a number was expected")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ParseError(f"not a number: {value!r}") from None
    if not number.is_finite():
        raise ParseError(f"not a finite number: {value!r}")
    return number


def parse_market(raw_market: Mapping[str, Any], raw_event: Mapping[str, Any]) -> MarketCandidate:
    try:
        condition_id = raw_market["conditionId"]
        if not isinstance(condition_id, str) or not condition_id.startswith("0x"):
            raise ParseError("conditionId missing or malformed")
        end_raw = raw_market.get("endDate")
        tags = raw_event.get("tags") or []
        schedule = raw_market.get("feeSchedule")
        return MarketCandidate(
            condition_id=condition_id,
            event_id=str(raw_event["id"]),
            question=str(raw_market.get("question") or ""),
            rules_text=str(raw_market.get("description") or ""),
            resolution_source=str(raw_market.get("resolutionSource") or ""),
            end_date=parse_datetime(end_raw) if isinstance(end_raw, str) and end_raw else None,
            version=str(raw_market.get("version") or ""),
            closed=bool(raw_market.get("closed")),
            accepting_orders=bool(raw_market.get("acceptingOrders")),
            neg_risk=bool(raw_market.get("negRisk")) or bool(raw_event.get("negRisk")),
            outcomes=_string_list(raw_market.get("outcomes")),
            token_ids=_string_list(raw_market.get("clobTokenIds")),
            tag_slugs=tuple(str(tag["slug"]) for tag in tags if isinstance(tag, Mapping)),
            liquidity=_optional_decimal(raw_market.get("liquidityNum")),
            best_bid=_optional_decimal(raw_market.get("bestBid")),
            best_ask=_optional_decimal(raw_market.get("bestAsk")),
            fees_enabled=bool(raw_market.get("feesEnabled")),
            fee_schedule=dict(schedule) if isinstance(schedule, Mapping) else None,
        )
    except KeyError as error:
        raise ParseError(f"missing field {error}") from None
    except ValueError as error:
        if isinstance(error, ParseError):
            raise
        raise ParseError(str(error)) from None


def category_for(tag_slugs: tuple[str, ...], config: DiscoveryConfig) -> str | None:
    for slug, category in config.tag_categories:
        if slug in tag_slugs:
            return category
    return None


def fee_rate(schedule: object) -> Decimal | None:
    if not isinstance(schedule, Mapping):
        return None
    if schedule.get("exponent") != 1 or schedule.get("takerOnly") is not True:
        return None
    try:
        rate = Decimal(str(schedule.get("rate")))
    except InvalidOperation:
        return None
    if not Decimal("0") <= rate < Decimal("1"):
        return None
    return rate


def rules_payload(candidate: MarketCandidate) -> dict[str, str | None]:
    return {
        "question": candidate.question,
        "rules_text": candidate.rules_text,
        "resolution_source": candidate.resolution_source,
        "end_date": isoformat(candidate.end_date) if candidate.end_date else None,
    }


def rules_hash(candidate: MarketCandidate) -> str:
    return sha256_json(rules_payload(candidate))


def eligibility_refusal(
    candidate: MarketCandidate, config: DiscoveryConfig, now: datetime
) -> str | None:
    if candidate.closed or not candidate.accepting_orders:
        return "NOT_OPEN"
    if candidate.version != "v1":
        return "UNSUPPORTED_VERSION"
    if candidate.neg_risk:
        return "NEG_RISK"
    if candidate.outcomes != ("Yes", "No") or len(candidate.token_ids) != 2:
        return "NON_BINARY"
    if category_for(candidate.tag_slugs, config) is None:
        return "NO_CATEGORY"
    if not candidate.rules_text.strip():
        return "NO_RULES"
    if candidate.end_date is None:
        return "END_WINDOW"
    remaining = candidate.end_date - now
    if not (
        timedelta(days=config.min_days_to_end)
        <= remaining
        <= timedelta(days=config.max_days_to_end)
    ):
        return "END_WINDOW"
    if candidate.liquidity is None or candidate.liquidity < config.min_liquidity:
        return "LOW_LIQUIDITY"
    if candidate.best_bid is None or candidate.best_ask is None:
        return "NO_QUOTE"
    threshold = config.known_outcome_threshold
    if candidate.best_ask >= threshold or candidate.best_bid <= Decimal("1") - threshold:
        return "OUTCOME_KNOWN"
    if candidate.fees_enabled and fee_rate(candidate.fee_schedule) is None:
        return "FEE_UNKNOWN"
    return None


def fetch_events(client: JsonClient, config: DiscoveryConfig) -> list[dict[str, Any]]:
    """Walk /events/keyset per tag. Offset paging is capped server-side (verified 2026-10-05:
    offset 3000 is rejected) and politics alone had 2,484 active events, so keyset is required.
    Still having a next_cursor after max_pages fails closed rather than reporting a partial
    universe as complete."""
    seen: dict[str, dict[str, Any]] = {}
    for slug, _category in config.tag_categories:
        cursor: str | None = None
        for _page in range(config.max_pages):
            params: dict[str, str | int] = {
                "active": "true",
                "closed": "false",
                "tag_slug": slug,
                "order": "id",
                "ascending": "true",
                "limit": config.page_size,
            }
            if cursor is not None:
                params["after_cursor"] = cursor
            payload = client.get(f"{GAMMA_URL}/events/keyset", params)
            batch = payload.get("events") if isinstance(payload, dict) else None
            if not isinstance(batch, list):
                raise ParseError("Gamma /events/keyset did not return an events list")
            for event in batch:
                if isinstance(event, dict) and "id" in event:
                    seen.setdefault(str(event["id"]), event)
            next_cursor = payload.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        else:
            raise ParseError(
                f"discovery truncated: tag {slug!r} still had more pages after "
                f"{config.max_pages} pages of {config.page_size}; raise discovery.max_pages"
            )
    return list(seen.values())

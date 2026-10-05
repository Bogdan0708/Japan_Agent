from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .gamma import ParseError
from .http import JsonClient
from .util import from_epoch_ms, isoformat, sha256_json

CLOB_URL = "https://clob.polymarket.com"
CLOCK_SKEW_TOLERANCE = timedelta(seconds=5)
MIN_TIMESTAMP_MS = 1_577_836_800_000  # 2020-01-01T00:00:00Z
MAX_TIMESTAMP_MS = 4_102_444_800_000  # 2100-01-01T00:00:00Z


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class BookSnapshot:
    condition_id: str
    token_id: str
    observed_at: datetime
    fetched_at: datetime
    book_hash: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    tick_size: Decimal
    min_order_size: Decimal

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    def record(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "observed_at": isoformat(self.observed_at),
            "fetched_at": isoformat(self.fetched_at),
            "book_hash": self.book_hash,
            "bids": [[lvl.price, lvl.size] for lvl in self.bids],
            "asks": [[lvl.price, lvl.size] for lvl in self.asks],
            "tick_size": self.tick_size,
            "min_order_size": self.min_order_size,
        }


def _decimal(value: object, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ParseError(f"{field} is not a number: {value!r}") from None
    if not number.is_finite():
        raise ParseError(f"{field} is not a finite number: {value!r}")
    return number


def _levels(raw: object) -> list[Level]:
    if not isinstance(raw, list):
        raise ParseError("book side is not a list")
    levels: list[Level] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ParseError("book level is not an object")
        price = _decimal(item.get("price"), "price")
        size = _decimal(item.get("size"), "size")
        if not Decimal("0") < price < Decimal("1") or size <= 0:
            raise ParseError(f"book level out of range: {price} x {size}")
        levels.append(Level(price, size))
    return levels


def parse_book(raw: Mapping[str, Any], fetched_at: datetime) -> BookSnapshot:
    try:
        millis = int(raw["timestamp"])
    except (KeyError, ValueError, TypeError):
        raise ParseError("book timestamp missing or malformed") from None
    if not MIN_TIMESTAMP_MS <= millis <= MAX_TIMESTAMP_MS:
        raise ParseError(f"book timestamp out of range: {millis}")
    observed_at = from_epoch_ms(millis)
    tick_size = _decimal(raw.get("tick_size"), "tick_size")
    min_order_size = _decimal(raw.get("min_order_size"), "min_order_size")
    # min_order_size "0" is served live for some liquid markets; only negatives are invalid.
    if tick_size <= 0 or min_order_size < 0:
        raise ParseError(f"invalid tick_size {tick_size} or min_order_size {min_order_size}")
    return BookSnapshot(
        condition_id=str(raw.get("market") or ""),
        token_id=str(raw.get("asset_id") or ""),
        observed_at=observed_at,
        fetched_at=fetched_at,
        book_hash=str(raw.get("hash") or ""),
        bids=tuple(sorted(_levels(raw.get("bids")), key=lambda lvl: lvl.price, reverse=True)),
        asks=tuple(sorted(_levels(raw.get("asks")), key=lambda lvl: lvl.price)),
        tick_size=tick_size,
        min_order_size=min_order_size,
    )


def book_refusal(
    snapshot: BookSnapshot, expected_condition_id: str, expected_token_id: str
) -> str | None:
    # Observed behaviour (2026-10-05, not documented): the book timestamp stayed fixed while
    # the hash was unchanged, so it appears to be the last-change time. An old value is
    # therefore not treated as stale here; both times are stored and freshness from
    # fetched_at is enforced at decision time (Plan 3). Only a future timestamp is refused.
    if (
        snapshot.condition_id != expected_condition_id
        or snapshot.token_id != expected_token_id
    ):
        return "BOOK_MISMATCH"
    if snapshot.observed_at - snapshot.fetched_at > CLOCK_SKEW_TOLERANCE:
        return "CLOCK_SKEW"
    if not snapshot.bids or not snapshot.asks:
        return "EMPTY_SIDE"
    if snapshot.bids[0].price >= snapshot.asks[0].price:
        return "CROSSED_BOOK"
    return None


def snapshot_hash(snapshot: BookSnapshot) -> str:
    return sha256_json(snapshot.record())


def fetch_book(client: JsonClient, token_id: str) -> Any:
    return client.get(f"{CLOB_URL}/book", {"token_id": token_id})

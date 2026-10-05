"""Fixtures shaped from live Polymarket responses observed 2026-10-05 (field names,
types and encodings are real; values are trimmed or invented)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CONDITION_ID = "0x5186ed9650f7023d1e0d4ecfd782bff720de56f62c7b7eacfe1af33d3a7cbe1c"
YES_TOKEN = "92583605307183622503994220835856867832397015693472702018901335560560344783116"
NO_TOKEN = "11111111111111111111111111111111111111111111111111111111111111111111111111111"
NOW_MS = "1791201600000"  # == NOW


def gamma_market(**overrides: Any) -> dict[str, Any]:
    market: dict[str, Any] = {
        "id": "702001",
        "conditionId": CONDITION_ID,
        "questionID": "0x29fc63d66c840ff5e6f190f3b7f1eae0f6cb5c3b37b41cc63dad121632c80c4b",
        "question": "Will the IEA ask Germany to release oil stocks by October 31?",
        "description": "This market will resolve to \"Yes\" if the IEA formally asks ...",
        "resolutionSource": "",
        "endDate": "2026-11-01T03:59:00Z",
        "version": "v1",
        "category": None,
        "closed": False,
        "active": True,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "negRisk": False,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.36", "0.64"]',
        "clobTokenIds": json.dumps([YES_TOKEN, NO_TOKEN]),
        "liquidityNum": 76109.0796,
        "bestBid": 0.36,
        "bestAsk": 0.37,
        "feesEnabled": True,
        "feeType": "politics_fees",
        "feeSchedule": {"exponent": 1, "rate": 0.04, "takerOnly": True, "rebateRate": 0.25},
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
        "umaResolutionStatuses": "[]",
    }
    market.update(overrides)
    return market


def gamma_event(
    markets: list[dict[str, Any]],
    tags: tuple[str, ...] = ("politics",),
    event_id: str = "17526",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "title": "IEA oil stocks release",
        "active": True,
        "closed": False,
        "negRisk": False,
        "tags": [
            {"id": str(i), "slug": slug, "label": slug.title()} for i, slug in enumerate(tags)
        ],
        "markets": markets,
    }


def clob_book(**overrides: Any) -> dict[str, Any]:
    book: dict[str, Any] = {
        "market": CONDITION_ID,
        "asset_id": YES_TOKEN,
        "timestamp": NOW_MS,
        "hash": "f1f3a531ef41321b35a0c1ddc6e4ba78556646d7",
        "bids": [
            {"price": "0.30", "size": "100"},
            {"price": "0.35", "size": "12.33"},
            {"price": "0.36", "size": "50"},
        ],
        "asks": [
            {"price": "0.45", "size": "125.08"},
            {"price": "0.38", "size": "57.12"},
            {"price": "0.37", "size": "25"},
        ],
        "tick_size": "0.01",
        "min_order_size": "5",
        "neg_risk": False,
        "last_trade_price": "0.37",
    }
    book.update(overrides)
    return book


def resolution_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "question_id": "0x29fc63d66c840ff5e6f190f3b7f1eae0f6cb5c3b37b41cc63dad121632c80c4b",
        "condition_id": CONDITION_ID,
        "status": "posed",
        "extended_review": False,
        "was_disputed": False,
        "new_version_q": False,
        "proposed_price": "69",
        "reproposed_price": "69",
        "price": "69",
        "transaction_hash": "0x85786708c6034c31d9b76f15047ede9949658742b6d342fce204175230b468ff",
        "log_index": "372",
        "last_update_timestamp": "1790978871",
    }
    row.update(overrides)
    return row

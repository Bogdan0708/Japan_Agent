from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .gamma import GAMMA_URL, ParseError
from .http import JsonClient

DATA_API_URL = "https://data-api.polymarket.com"
OPEN_STATUSES = frozenset({"initialized", "posed", "active"})
BATCH = 20
MICRO = 1_000_000
LEAN_THRESHOLD = Decimal("0.95")
_UMA_PRICES = {
    "1000000000000000000": "YES",
    "0": "NO",
    "500000000000000000": "HALF",
}
_PAYOUTS = {(MICRO, 0): "YES", (0, MICRO): "NO", (MICRO // 2, MICRO // 2): "HALF"}
_GAMMA_PRICES = {("1", "0"): "YES", ("0", "1"): "NO", ("0.5", "0.5"): "HALF"}


@dataclass(frozen=True)
class ResolutionState:
    condition_id: str
    status: str
    outcome: str | None
    was_disputed: bool
    new_version_q: bool
    raw: dict[str, Any]


def _outcome(row: Mapping[str, Any]) -> str | None:
    if row.get("status") != "resolved":
        return None
    payouts = row.get("payouts")
    if isinstance(payouts, list):
        if len(payouts) != 2 or not all(isinstance(p, int) for p in payouts):
            return "UNKNOWN"
        return _PAYOUTS.get((payouts[0], payouts[1]), "UNKNOWN")
    return _UMA_PRICES.get(str(row.get("price")), "UNKNOWN")


def parse_resolution(row: Mapping[str, Any]) -> ResolutionState:
    status = row.get("status")
    condition_id = row.get("condition_id")
    if not isinstance(status, str) or not isinstance(condition_id, str):
        raise ParseError("resolution row lacks status or condition_id")
    return ResolutionState(
        condition_id=condition_id,
        status=status,
        outcome=_outcome(row),
        was_disputed=bool(row.get("was_disputed")),
        new_version_q=bool(row.get("new_version_q")),
        raw=dict(row),
    )


def gamma_outcome(outcome_prices: object) -> str | None:
    if not isinstance(outcome_prices, str):
        return None
    try:
        prices = json.loads(outcome_prices)
    except json.JSONDecodeError:
        return None
    if not isinstance(prices, list) or len(prices) != 2:
        return None
    return _GAMMA_PRICES.get((str(prices[0]), str(prices[1])))


def _gamma_lean(outcome_prices: object) -> str | None:
    """YES/NO when Gamma's YES price is near-certain (>= 0.95 or <= 0.05), else None."""
    if not isinstance(outcome_prices, str):
        return None
    try:
        prices = json.loads(outcome_prices)
        yes = Decimal(str(prices[0]))
    except (json.JSONDecodeError, IndexError, KeyError, TypeError, InvalidOperation):
        return None
    if not yes.is_finite():
        return None
    if yes >= LEAN_THRESHOLD:
        return "YES"
    if yes <= 1 - LEAN_THRESHOLD:
        return "NO"
    return None


def reconcile_outcome(state: ResolutionState, outcome_prices: object) -> tuple[str | None, str]:
    """Return (outcome, cross_check). Only an exact Gamma 0/1 (or 0.5/0.5) agreement is
    CONFIRMED; any disagreement, including a near-certain opposite price, is a MISMATCH and
    the outcome becomes UNKNOWN. Anything else stays UNCHECKED."""
    if state.outcome in (None, "UNKNOWN"):
        return state.outcome, "NOT_APPLICABLE"
    exact = gamma_outcome(outcome_prices)
    if exact is not None:
        return (state.outcome, "CONFIRMED") if exact == state.outcome else ("UNKNOWN", "MISMATCH")
    lean = _gamma_lean(outcome_prices)
    if lean is not None and state.outcome != "HALF" and lean != state.outcome:
        return "UNKNOWN", "MISMATCH"
    return state.outcome, "UNCHECKED"


def resolution_refusal(row: Mapping[str, Any] | None) -> str | None:
    if row is None:
        return "RESOLUTION_STATE_MISSING"
    if row.get("status") not in OPEN_STATUSES:
        return "RESOLUTION_IN_PROGRESS"
    return None


def _batches(ids: list[str]) -> list[list[str]]:
    return [ids[start : start + BATCH] for start in range(0, len(ids), BATCH)]


def fetch_resolutions(client: JsonClient, condition_ids: list[str]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for batch in _batches(condition_ids):
        payload = client.get(f"{DATA_API_URL}/v2/resolutions", {"condition": ",".join(batch)})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ParseError("/v2/resolutions did not return a data list")
        for row in data:
            if isinstance(row, dict) and isinstance(row.get("condition_id"), str):
                rows[row["condition_id"]] = row
    return rows


def fetch_closed_gamma_markets(
    client: JsonClient, condition_ids: list[str]
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for batch in _batches(condition_ids):
        payload = client.get(
            f"{GAMMA_URL}/markets", {"condition_ids": ",".join(batch), "closed": "true"}
        )
        if not isinstance(payload, list):
            raise ParseError("Gamma /markets did not return a list")
        for market in payload:
            if isinstance(market, dict) and isinstance(market.get("conditionId"), str):
                rows[market["conditionId"]] = market
    return rows

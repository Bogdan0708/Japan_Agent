from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .time import isoformat, parse_datetime


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Sleeve(StrEnum):
    CORE = "CORE"
    SATELLITE = "SATELLITE"


class ProposalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


def decimal(value: Decimal | str | int | float) -> Decimal:
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def canonical_json(value: Any) -> str:
    def default(item: Any) -> Any:
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, datetime):
            return isoformat(item)
        if isinstance(item, StrEnum):
            return item.value
        raise TypeError(f"cannot serialize {type(item)!r}")

    return json.dumps(value, default=default, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InstrumentRule:
    ticker: str
    display_name: str
    sleeve: Sleeve
    instrument_type: str
    native_currency: str
    max_position_fraction: Decimal = Decimal("0.40")
    max_position_gbp: Decimal | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> InstrumentRule:
        return cls(
            ticker=str(value["ticker"]),
            display_name=str(value["display_name"]),
            sleeve=Sleeve(value["sleeve"]),
            instrument_type=str(value["instrument_type"]),
            native_currency=str(value["native_currency"]),
            max_position_fraction=decimal(value.get("max_position_fraction", "0.40")),
            max_position_gbp=(
                decimal(value["max_position_gbp"])
                if value.get("max_position_gbp") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class PriceSnapshot:
    ticker: str
    native_price: Decimal
    native_currency: str
    price_gbp: Decimal
    observed_at: datetime
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Position:
    ticker: str
    quantity: Decimal
    market_value_gbp: Decimal


@dataclass(frozen=True)
class Portfolio:
    cash_gbp: Decimal
    positions: tuple[Position, ...]

    @property
    def nav_gbp(self) -> Decimal:
        return self.cash_gbp + sum(
            (position.market_value_gbp for position in self.positions), Decimal("0")
        )

    def position(self, ticker: str) -> Position:
        return next(
            (item for item in self.positions if item.ticker == ticker),
            Position(ticker=ticker, quantity=Decimal("0"), market_value_gbp=Decimal("0")),
        )


@dataclass(frozen=True)
class PortfolioSnapshot:
    portfolio: Portfolio
    observed_at: datetime
    source: str


@dataclass(frozen=True)
class ResearchDecision:
    thesis: str
    evidence: tuple[str, ...]
    action: Action
    instrument: str
    target_weight: Decimal
    confidence: Decimal
    invalidation_condition: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ResearchDecision:
        evidence = value.get("evidence")
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise ValueError("evidence must be a list of strings")
        decision = cls(
            thesis=str(value["thesis"]).strip(),
            evidence=tuple(item.strip() for item in evidence if item.strip()),
            action=Action(value["action"]),
            instrument=str(value["instrument"]).strip(),
            target_weight=decimal(value["target_weight"]),
            confidence=decimal(value["confidence"]),
            invalidation_condition=str(value["invalidation_condition"]).strip(),
        )
        if not Decimal("0") <= decision.target_weight <= Decimal("1"):
            raise ValueError("target_weight must be between 0 and 1")
        if not Decimal("0") <= decision.confidence <= Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")
        if not decision.thesis or not decision.evidence or not decision.invalidation_condition:
            raise ValueError("thesis, evidence and invalidation_condition are required")
        return decision


@dataclass(frozen=True)
class TradeTicket:
    proposal_id: str
    created_at: datetime
    expires_at: datetime
    ticker: str
    side: Action
    quantity: Decimal
    reference_price_gbp: Decimal
    estimated_value_gbp: Decimal
    target_weight: Decimal
    confidence: Decimal
    max_price_deviation_fraction: Decimal
    thesis: str
    evidence: tuple[str, ...]
    invalidation_condition: str
    snapshot_observed_at: datetime

    def __post_init__(self) -> None:
        if self.side not in (Action.BUY, Action.SELL):
            raise ValueError("a trade ticket must be BUY or SELL")
        if self.side is Action.BUY and self.quantity <= 0:
            raise ValueError("BUY quantity must be positive")
        if self.side is Action.SELL and self.quantity >= 0:
            raise ValueError("SELL quantity must be negative")
        if self.expires_at <= self.created_at:
            raise ValueError("ticket expiry must be after creation")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        return sha256_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TradeTicket:
        return cls(
            proposal_id=str(value["proposal_id"]),
            created_at=parse_datetime(str(value["created_at"])),
            expires_at=parse_datetime(str(value["expires_at"])),
            ticker=str(value["ticker"]),
            side=Action(value["side"]),
            quantity=decimal(value["quantity"]),
            reference_price_gbp=decimal(value["reference_price_gbp"]),
            estimated_value_gbp=decimal(value["estimated_value_gbp"]),
            target_weight=decimal(value["target_weight"]),
            confidence=decimal(value["confidence"]),
            max_price_deviation_fraction=decimal(value["max_price_deviation_fraction"]),
            thesis=str(value["thesis"]),
            evidence=tuple(str(item) for item in value["evidence"]),
            invalidation_condition=str(value["invalidation_condition"]),
            snapshot_observed_at=parse_datetime(str(value["snapshot_observed_at"])),
        )

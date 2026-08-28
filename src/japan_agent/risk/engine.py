from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from ..models import (
    Action,
    InstrumentRule,
    Portfolio,
    PriceSnapshot,
    ResearchDecision,
    Sleeve,
    TradeTicket,
)
from ..time import ensure_utc


@dataclass(frozen=True)
class RiskPolicy:
    max_position_fraction: Decimal = Decimal("0.40")
    satellite_max_gbp: Decimal = Decimal("15")
    max_trades_per_week: int = 3
    cash_floor_gbp: Decimal = Decimal("5")
    max_snapshot_age: timedelta = timedelta(hours=72)
    max_price_deviation_fraction: Decimal = Decimal("0.03")
    approval_ttl: timedelta = timedelta(hours=24)
    quantity_places: Decimal = Decimal("0.0001")


@dataclass(frozen=True)
class RiskViolation:
    code: str
    message: str


class RiskRejected(ValueError):
    def __init__(self, violations: list[RiskViolation]):
        self.violations = tuple(violations)
        super().__init__("; ".join(f"{item.code}: {item.message}" for item in violations))


class ProposalBuilder:
    """Pure deterministic construction and risk checks.

    ResearchDecision is untrusted LLM output. This class is the only path from
    a decision to an approvable ticket and has no network or broker access.
    """

    def __init__(self, policy: RiskPolicy | None = None):
        self.policy = policy or RiskPolicy()

    def build(
        self,
        *,
        decision: ResearchDecision,
        instrument: InstrumentRule | None,
        snapshot: PriceSnapshot | None,
        portfolio: Portfolio,
        submitted_trade_times: list[datetime],
        duplicate_open: bool,
        now: datetime,
    ) -> TradeTicket:
        now = ensure_utc(now)
        violations: list[RiskViolation] = []
        if decision.action is Action.HOLD:
            violations.append(RiskViolation("HOLD", "a HOLD decision does not create an order"))
        if instrument is None:
            violations.append(
                RiskViolation("NOT_WHITELISTED", f"{decision.instrument!r} is not whitelisted")
            )
        elif instrument.instrument_type.upper() not in {"STOCK", "ETF"}:
            violations.append(
                RiskViolation(
                    "TYPE_BLOCKED",
                    f"{instrument.instrument_type} is not a cash equity/ETF",
                )
            )
        if snapshot is None:
            violations.append(RiskViolation("MISSING_PRICE", "no normalized GBP price snapshot"))
        else:
            if snapshot.ticker != decision.instrument:
                violations.append(RiskViolation("PRICE_TICKER", "snapshot ticker mismatch"))
            if snapshot.price_gbp <= 0 or snapshot.native_price <= 0:
                violations.append(RiskViolation("PRICE_INVALID", "snapshot price must be positive"))
            age = now - ensure_utc(snapshot.observed_at)
            if age < timedelta(0):
                violations.append(
                    RiskViolation("PRICE_FUTURE", "snapshot timestamp is in the future")
                )
            elif age > self.policy.max_snapshot_age:
                violations.append(
                    RiskViolation(
                        "PRICE_STALE",
                        f"snapshot is {age} old; maximum is {self.policy.max_snapshot_age}",
                    )
                )
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        this_week = sum(ensure_utc(item) >= monday for item in submitted_trade_times)
        if this_week >= self.policy.max_trades_per_week:
            violations.append(
                RiskViolation(
                    "WEEKLY_TRADE_LIMIT",
                    f"already submitted {this_week} trades this week",
                )
            )
        if duplicate_open:
            violations.append(
                RiskViolation(
                    "DUPLICATE_OPEN",
                    "an open ticket/order already exists for ticker and side",
                )
            )
        if violations:
            raise RiskRejected(violations)
        assert instrument is not None and snapshot is not None

        nav = portfolio.nav_gbp
        if nav <= 0:
            raise RiskRejected([RiskViolation("NAV_INVALID", "portfolio NAV must be positive")])
        current = portfolio.position(instrument.ticker)
        desired_value = (decision.target_weight * nav).quantize(Decimal("0.01"))
        delta_value = desired_value - current.market_value_gbp
        if decision.action is Action.BUY and delta_value <= 0:
            violations.append(
                RiskViolation("ACTION_TARGET_MISMATCH", "BUY target is not above current position")
            )
        if decision.action is Action.SELL and delta_value >= 0:
            violations.append(
                RiskViolation("ACTION_TARGET_MISMATCH", "SELL target is not below current position")
            )

        absolute_quantity = (abs(delta_value) / snapshot.price_gbp).quantize(
            self.policy.quantity_places, rounding=ROUND_DOWN
        )
        if absolute_quantity < self.policy.quantity_places:
            violations.append(RiskViolation("ORDER_TOO_SMALL", "order rounds below 0.0001 shares"))
        quantity = absolute_quantity if decision.action is Action.BUY else -absolute_quantity
        if decision.action is Action.SELL and absolute_quantity > current.quantity:
            violations.append(RiskViolation("INSUFFICIENT_SHARES", "sell exceeds current holding"))

        estimated_value = (absolute_quantity * snapshot.price_gbp).quantize(Decimal("0.01"))
        projected_value = (
            current.market_value_gbp + estimated_value
            if decision.action is Action.BUY
            else current.market_value_gbp - estimated_value
        )
        max_fraction = min(instrument.max_position_fraction, self.policy.max_position_fraction)
        if projected_value > nav * max_fraction:
            violations.append(
                RiskViolation(
                    "POSITION_LIMIT",
                    f"projected £{projected_value} exceeds {max_fraction:.0%} NAV cap",
                )
            )
        absolute_cap = instrument.max_position_gbp
        if absolute_cap is None and instrument.sleeve in {Sleeve.SATELLITE, Sleeve.FRONTIER}:
            absolute_cap = self.policy.satellite_max_gbp
        if absolute_cap is not None and projected_value > absolute_cap:
            violations.append(
                RiskViolation(
                    "ABSOLUTE_POSITION_LIMIT",
                    f"projected £{projected_value} exceeds £{absolute_cap} cap",
                )
            )
        if decision.action is Action.BUY:
            cash_after = portfolio.cash_gbp - estimated_value
            if cash_after < self.policy.cash_floor_gbp:
                violations.append(
                    RiskViolation(
                        "CASH_FLOOR",
                        f"estimated cash £{cash_after} is below £{self.policy.cash_floor_gbp}",
                    )
                )
        if projected_value < 0:
            violations.append(RiskViolation("NEGATIVE_POSITION", "projected position is negative"))
        if violations:
            raise RiskRejected(violations)

        return TradeTicket(
            proposal_id=str(uuid.uuid4()),
            created_at=now,
            expires_at=now + self.policy.approval_ttl,
            ticker=instrument.ticker,
            side=decision.action,
            quantity=quantity,
            reference_price_gbp=snapshot.price_gbp,
            estimated_value_gbp=estimated_value,
            target_weight=decision.target_weight,
            confidence=decision.confidence,
            max_price_deviation_fraction=self.policy.max_price_deviation_fraction,
            thesis=decision.thesis,
            evidence=decision.evidence,
            invalidation_condition=decision.invalidation_condition,
            snapshot_observed_at=snapshot.observed_at,
        )

    def validate_execution(
        self,
        *,
        ticket: TradeTicket,
        instrument: InstrumentRule | None,
        portfolio: Portfolio,
        current_price_gbp: Decimal,
        submitted_trade_times: list[datetime],
        now: datetime,
    ) -> None:
        """Re-check mutable portfolio limits immediately before broker submission."""
        violations: list[RiskViolation] = []
        if instrument is None or instrument.ticker != ticket.ticker:
            violations.append(RiskViolation("NOT_WHITELISTED", "ticket is no longer whitelisted"))
        elif instrument.instrument_type.upper() not in {"STOCK", "ETF"}:
            violations.append(RiskViolation("TYPE_BLOCKED", "instrument type is no longer allowed"))
        nav = portfolio.nav_gbp
        if nav <= 0:
            violations.append(RiskViolation("NAV_INVALID", "portfolio NAV must be positive"))
        current = portfolio.position(ticket.ticker)
        value = (abs(ticket.quantity) * current_price_gbp).quantize(Decimal("0.01"))
        projected = (
            current.market_value_gbp + value
            if ticket.side is Action.BUY
            else current.market_value_gbp - value
        )
        monday = ensure_utc(now).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=ensure_utc(now).weekday()
        )
        this_week = sum(ensure_utc(item) >= monday for item in submitted_trade_times)
        if this_week >= self.policy.max_trades_per_week:
            violations.append(
                RiskViolation("WEEKLY_TRADE_LIMIT", "weekly trade limit reached before execution")
            )
        if ticket.side is Action.BUY and portfolio.cash_gbp - value < self.policy.cash_floor_gbp:
            violations.append(
                RiskViolation("CASH_FLOOR", "fresh portfolio would breach the cash floor")
            )
        if ticket.side is Action.SELL and abs(ticket.quantity) > current.quantity:
            violations.append(
                RiskViolation("INSUFFICIENT_SHARES", "fresh portfolio has fewer available shares")
            )
        if projected < 0:
            violations.append(
                RiskViolation("NEGATIVE_POSITION", "fresh projected position is negative")
            )
        if instrument is not None and nav > 0:
            max_fraction = min(instrument.max_position_fraction, self.policy.max_position_fraction)
            if projected > nav * max_fraction:
                violations.append(
                    RiskViolation("POSITION_LIMIT", "fresh portfolio would breach the NAV cap")
                )
            absolute_cap = instrument.max_position_gbp
            if absolute_cap is None and instrument.sleeve in {Sleeve.SATELLITE, Sleeve.FRONTIER}:
                absolute_cap = self.policy.satellite_max_gbp
            if absolute_cap is not None and projected > absolute_cap:
                violations.append(
                    RiskViolation(
                        "ABSOLUTE_POSITION_LIMIT", "fresh portfolio would breach the cash cap"
                    )
                )
        if violations:
            raise RiskRejected(violations)

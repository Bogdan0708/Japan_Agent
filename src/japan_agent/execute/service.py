from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import PortfolioSnapshot, PriceSnapshot, ProposalStatus
from ..risk import ProposalBuilder, RiskRejected
from ..storage import Database
from ..time import ensure_utc, parse_datetime
from .t212 import Broker, BrokerRejected, BrokerTransportUncertain


class ExecutionBlocked(RuntimeError):
    pass


class ManualReconciliationRequired(ExecutionBlocked):
    pass


@dataclass(frozen=True)
class LiveGate:
    approved_by: str
    reviewed_at: datetime
    paper_tracking_started_at: datetime
    manual_fix_free_days: int
    no_risk_breaches: bool
    journal_verified: bool
    compliance_confirmation_reference: str

    @classmethod
    def from_file(cls, path: Path) -> LiveGate:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ExecutionBlocked("live gate file must not be accessible by group or others")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                approved_by=str(value["approved_by"]),
                reviewed_at=parse_datetime(str(value["reviewed_at"])),
                paper_tracking_started_at=parse_datetime(str(value["paper_tracking_started_at"])),
                manual_fix_free_days=int(value["manual_fix_free_days"]),
                no_risk_breaches=value["no_risk_breaches"] is True,
                journal_verified=value["journal_verified"] is True,
                compliance_confirmation_reference=str(value["compliance_confirmation_reference"]),
            )
        except (ValueError, KeyError, TypeError) as error:
            raise ExecutionBlocked("live gate file is malformed") from error


def assert_environment_allowed(settings: Settings, now: datetime) -> None:
    if settings.killswitch_path.exists():
        raise ExecutionBlocked(f"kill switch is engaged at {settings.killswitch_path}")
    if settings.t212_environment == "demo":
        return
    if settings.allow_live_trading != "I_ACKNOWLEDGE_REAL_MONEY":
        raise ExecutionBlocked("ALLOW_LIVE_TRADING acknowledgement is missing")
    if not settings.written_consent_reference:
        raise ExecutionBlocked("Trading 212 written-consent reference is missing")
    if settings.live_gate_file is None or not settings.live_gate_file.is_file():
        raise ExecutionBlocked("signed live gate file is missing")
    gate = LiveGate.from_file(settings.live_gate_file)
    if not gate.approved_by.strip():
        raise ExecutionBlocked("live gate has no human approver")
    if gate.compliance_confirmation_reference != settings.written_consent_reference:
        raise ExecutionBlocked("live gate compliance reference mismatch")
    if gate.manual_fix_free_days < 14:
        raise ExecutionBlocked("paper track record is shorter than 14 manual-fix-free days")
    elapsed = gate.reviewed_at - gate.paper_tracking_started_at
    if elapsed < timedelta(days=14) or gate.manual_fix_free_days > elapsed.days:
        raise ExecutionBlocked("live gate paper dates do not support the claimed clean-day count")
    if not gate.no_risk_breaches or not gate.journal_verified:
        raise ExecutionBlocked("paper risk and journal gates have not passed")
    if gate.reviewed_at > ensure_utc(now):
        raise ExecutionBlocked("live gate review timestamp is in the future")


def _quantity_mismatch(reported: Any, approved: Decimal) -> bool:
    """An absent quantity is tolerated; a present one must equal the approved amount."""
    if reported is None:
        return False
    try:
        return Decimal(str(reported)) != approved
    except InvalidOperation:
        return True


class ExecutionService:
    def __init__(self, *, database: Database, broker: Broker, settings: Settings):
        self.database = database
        self.broker = broker
        self.settings = settings
        self.risk = ProposalBuilder()

    def execute(
        self,
        proposal_id: str,
        *,
        current_snapshot: PriceSnapshot,
        portfolio_snapshot: PortfolioSnapshot,
        now: datetime,
    ) -> dict[str, Any]:
        now = ensure_utc(now)
        assert_environment_allowed(self.settings, now)
        stored = self.database.get_proposal(proposal_id)
        if stored is None:
            raise ExecutionBlocked("proposal does not exist")
        if stored.status in {ProposalStatus.SUBMITTED, ProposalStatus.FILLED}:
            claim = self.database.get_execution(proposal_id)
            if claim.response is not None:
                return claim.response
            raise ManualReconciliationRequired("submitted order has no stored broker response")
        if stored.status is ProposalStatus.RECONCILIATION_REQUIRED:
            raise ManualReconciliationRequired(
                "an earlier submission has an unknown outcome; it will not be resubmitted"
            )
        if stored.status is not ProposalStatus.APPROVED:
            raise ExecutionBlocked(f"proposal status is {stored.status}, not APPROVED")
        ticket = stored.ticket
        if stored.ticket_hash != ticket.fingerprint:
            raise ExecutionBlocked("stored ticket integrity check failed")
        if stored.approved_at is None or stored.approved_by is None:
            raise ExecutionBlocked("approval identity or timestamp is missing")
        if now >= ticket.expires_at:
            self.database.mark_expired(proposal_id)
            raise ExecutionBlocked("approved ticket has expired")
        if current_snapshot.ticker != ticket.ticker:
            raise ExecutionBlocked("execution quote ticker does not match approved ticket")
        quote_age = now - ensure_utc(current_snapshot.observed_at)
        if quote_age < timedelta(0) or quote_age > timedelta(minutes=20):
            raise ExecutionBlocked("execution quote must be timestamped within the last 20 minutes")
        current_price_gbp = current_snapshot.price_gbp
        if current_price_gbp <= 0:
            raise ExecutionBlocked("current normalized GBP price is invalid")
        deviation = abs(current_price_gbp - ticket.reference_price_gbp) / ticket.reference_price_gbp
        if deviation > ticket.max_price_deviation_fraction:
            raise ExecutionBlocked(
                f"price moved {deviation:.2%}; a fresh human-approved ticket is required"
            )
        portfolio_age = now - ensure_utc(portfolio_snapshot.observed_at)
        if portfolio_snapshot.source != "TRADING212":
            raise ExecutionBlocked("execution portfolio must be reconciled from Trading 212")
        if portfolio_age < timedelta(0) or portfolio_age > timedelta(minutes=2):
            raise ExecutionBlocked("Trading 212 portfolio snapshot must be less than 2 minutes old")
        whitelist = self.settings.load_whitelist()
        try:
            self.risk.validate_execution(
                ticket=ticket,
                instrument=whitelist.get(ticket.ticker),
                portfolio=portfolio_snapshot.portfolio,
                current_price_gbp=current_price_gbp,
                submitted_trade_times=self.database.submitted_trade_times(now - timedelta(days=7)),
                now=now,
            )
        except RiskRejected as error:
            raise ExecutionBlocked(f"fresh portfolio risk check failed: {error}") from error

        request = {
            "ticker": ticket.ticker,
            "quantity": str(ticket.quantity),
            "extendedHours": False,
            "approved_ticket_hash": stored.ticket_hash,
        }
        claim = self.database.claim_execution(proposal_id, request, now)
        if not claim.is_new:
            if claim.state in {"SUBMITTED", "FILLED"} and claim.response is not None:
                return claim.response
            raise ManualReconciliationRequired(
                f"proposal already has execution state {claim.state}; it will not be resubmitted"
            )

        try:
            response = self.broker.place_market_order(
                ticker=ticket.ticker,
                quantity=ticket.quantity,
                extended_hours=False,
            )
        except BrokerRejected as error:
            self.database.update_execution(
                proposal_id,
                state="FAILED",
                now=now,
                error=str(error),
                proposal_status=ProposalStatus.FAILED,
            )
            self.database.append_event(
                kind="ORDER_REJECTED",
                aggregate_id=proposal_id,
                payload={"http_status": error.status, "ticket_hash": stored.ticket_hash},
                now=now,
            )
            raise ExecutionBlocked(str(error)) from error
        except (BrokerTransportUncertain, OSError, TimeoutError) as error:
            self._mark_unknown(proposal_id, stored.ticket_hash, now, str(error))
            raise ManualReconciliationRequired(
                "order outcome is unknown; reconcile in Trading 212 before any new proposal"
            ) from error

        order_id = response.get("id")
        response_ticker = response.get("ticker")
        response_side = response.get("side")
        if (
            order_id is None
            or (response_ticker is not None and response_ticker != ticket.ticker)
            or (response_side is not None and str(response_side).upper() != ticket.side.value)
            or _quantity_mismatch(response.get("quantity"), ticket.quantity)
            or response.get("extendedHours") not in (None, False)
            or str(response.get("status", "")).upper() in {"REJECTED", "CANCELLED"}
        ):
            self._mark_unknown(
                proposal_id,
                stored.ticket_hash,
                now,
                "broker response does not match the approved ticket",
                response,
                broker_order_id=str(order_id) if order_id is not None else None,
            )
            raise ManualReconciliationRequired("ambiguous broker response requires reconciliation")

        self.database.update_execution(
            proposal_id,
            state="SUBMITTED",
            now=now,
            response=response,
            broker_order_id=str(order_id),
            proposal_status=ProposalStatus.SUBMITTED,
        )
        self.database.append_event(
            kind="ORDER_SUBMITTED",
            aggregate_id=proposal_id,
            payload={
                "broker_order_id": str(order_id),
                "ticker": ticket.ticker,
                "quantity": ticket.quantity,
                "ticket_hash": stored.ticket_hash,
            },
            now=now,
        )
        return response

    def reconcile(self, proposal_id: str, *, now: datetime) -> dict[str, Any]:
        claim = self.database.get_execution(proposal_id)
        if not claim.broker_order_id:
            raise ManualReconciliationRequired(
                "no broker order id is known; inspect broker history manually"
            )
        stored = self.database.get_proposal(proposal_id)
        try:
            response = self.broker.order(claim.broker_order_id)
        except BrokerRejected as error:
            if error.status != 404:
                raise
            # Terminal orders disappear from the pending endpoint; check history.
            found = None
            for item in self.broker.order_history(
                ticker=stored.ticket.ticker if stored is not None else None
            ):
                if str(item.get("id")) == claim.broker_order_id:
                    found = item
                    break
            if found is None:
                raise ManualReconciliationRequired(
                    "order is absent from both the pending and history endpoints; "
                    "inspect the broker manually"
                ) from error
            response = found
        status = str(response.get("status", "")).upper()
        if status == "FILLED":
            state = "FILLED"
            proposal_status = ProposalStatus.FILLED
            event = "ORDER_FILLED"
        elif status in {"REJECTED", "CANCELLED"}:
            state = "FAILED"
            proposal_status = ProposalStatus.FAILED
            event = "ORDER_TERMINAL_WITHOUT_FILL"
        else:
            state = "SUBMITTED"
            proposal_status = ProposalStatus.SUBMITTED
            event = "ORDER_RECONCILED_OPEN"
        self.database.update_execution(
            proposal_id,
            state=state,
            now=ensure_utc(now),
            response=response,
            broker_order_id=claim.broker_order_id,
            proposal_status=proposal_status,
        )
        self.database.append_event(
            kind=event,
            aggregate_id=proposal_id,
            payload={"broker_order_id": claim.broker_order_id, "broker_status": status},
            now=now,
        )
        return response

    def _mark_unknown(
        self,
        proposal_id: str,
        ticket_hash: str,
        now: datetime,
        error: str,
        response: dict[str, Any] | None = None,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        self.database.update_execution(
            proposal_id,
            state="UNKNOWN",
            now=now,
            response=response,
            broker_order_id=broker_order_id,
            error=error,
            proposal_status=ProposalStatus.RECONCILIATION_REQUIRED,
        )
        self.database.append_event(
            kind="ORDER_RECONCILIATION_REQUIRED",
            aggregate_id=proposal_id,
            payload={"error": error, "ticket_hash": ticket_hash},
            now=now,
        )

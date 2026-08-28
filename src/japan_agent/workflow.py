from __future__ import annotations

from datetime import datetime, timedelta

from .approve.service import ApprovalService
from .config import Settings
from .models import Portfolio, ResearchDecision, TradeTicket
from .risk.engine import ProposalBuilder, RiskRejected
from .storage import Database


class ProposalWorkflow:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        builder: ProposalBuilder | None = None,
    ):
        self.settings = settings
        self.database = database
        self.builder = builder or ProposalBuilder()
        self.approvals = ApprovalService(database)

    def propose(
        self, *, decision: ResearchDecision, portfolio: Portfolio, now: datetime
    ) -> TradeTicket:
        whitelist = self.settings.load_whitelist()
        instrument = whitelist.get(decision.instrument)
        snapshot = self.database.latest_snapshot(decision.instrument)
        # Fetch a harmless superset. The pure risk engine applies the exact ISO-week boundary.
        submitted = self.database.submitted_trade_times(now - timedelta(days=7))
        duplicate = self.database.has_open_duplicate(
            decision.instrument, decision.action.value
        )
        try:
            ticket = self.builder.build(
                decision=decision,
                instrument=instrument,
                snapshot=snapshot,
                portfolio=portfolio,
                submitted_trade_times=submitted,
                duplicate_open=duplicate,
                now=now,
            )
        except RiskRejected as error:
            self.database.append_event(
                kind="PROPOSAL_BLOCKED",
                aggregate_id=None,
                payload={
                    "instrument": decision.instrument,
                    "action": decision.action.value,
                    "violations": [
                        {"code": item.code, "message": item.message}
                        for item in error.violations
                    ],
                },
                now=now,
            )
            raise
        self.approvals.submit(ticket)
        return ticket

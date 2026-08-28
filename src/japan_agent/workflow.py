from __future__ import annotations

import re
from datetime import datetime, timedelta

from .approve.service import ApprovalService
from .config import Settings
from .models import Portfolio, ResearchDecision, TradeTicket
from .risk.engine import ProposalBuilder, RiskRejected, RiskViolation
from .storage import Database

CITATION = re.compile(r"^\[(PRICE|EDINET|TDNET|NEWS|JQUANTS):([^\]]+)\]\s+\S")


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

    def _citation_violations(self, decision: ResearchDecision) -> list[RiskViolation]:
        """Deterministically verify every evidence citation against local storage.

        The model's output is untrusted; a citation of a filing, headline, or
        quote that does not exist in the snapshot database blocks the proposal.
        """
        violations: list[RiskViolation] = []
        for entry in decision.evidence:
            match = CITATION.match(entry)
            if match is None:
                violations.append(
                    RiskViolation(
                        "EVIDENCE_FORMAT",
                        "evidence entry lacks a [SOURCE:external_id] citation prefix",
                    )
                )
                continue
            source, reference = match.groups()
            if source == "PRICE":
                known = self.database.latest_snapshot(reference) is not None
            else:
                known = self.database.research_item_exists(source, reference)
            if not known:
                violations.append(
                    RiskViolation(
                        "EVIDENCE_UNKNOWN_SOURCE",
                        f"evidence cites nonexistent {source} item {reference!r}",
                    )
                )
        return violations

    def propose(
        self, *, decision: ResearchDecision, portfolio: Portfolio, now: datetime
    ) -> TradeTicket:
        citation_violations = self._citation_violations(decision)
        if citation_violations:
            self.database.append_event(
                kind="PROPOSAL_BLOCKED",
                aggregate_id=None,
                payload={
                    "instrument": decision.instrument,
                    "action": decision.action.value,
                    "violations": [
                        {"code": item.code, "message": item.message}
                        for item in citation_violations
                    ],
                },
                now=now,
            )
            raise RiskRejected(citation_violations)
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

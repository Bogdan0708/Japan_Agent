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

    def _citation_violations(
        self, decision: ResearchDecision, now: datetime
    ) -> list[RiskViolation]:
        """Verify every evidence citation against the recorded research run.

        Existence in the database is not enough: an item can exist yet never
        have been in the bundle the model saw. Citations must come from the
        run's snapshot manifest, and the run must be recent.
        """
        if not decision.research_run_id:
            return [
                RiskViolation(
                    "EVIDENCE_NO_RUN",
                    "decision is not linked to a recorded research run",
                )
            ]
        run = self.database.get_research_run(decision.research_run_id)
        if run is None:
            return [
                RiskViolation(
                    "EVIDENCE_NO_RUN",
                    f"research run {decision.research_run_id!r} is not recorded",
                )
            ]
        if now - run["assembled_at"] > timedelta(hours=24):
            return [
                RiskViolation(
                    "EVIDENCE_STALE_RUN",
                    "the cited research run's snapshot is older than 24 hours",
                )
            ]
        permitted = run["permitted_citations"]
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
            if f"{source}:{reference}" not in permitted:
                violations.append(
                    RiskViolation(
                        "EVIDENCE_UNKNOWN_SOURCE",
                        f"evidence cites {source}:{reference}, which is not in the "
                        "research run's snapshot manifest",
                    )
                )
        return violations

    def propose(
        self, *, decision: ResearchDecision, portfolio: Portfolio, now: datetime
    ) -> TradeTicket:
        citation_violations = self._citation_violations(decision, now)
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

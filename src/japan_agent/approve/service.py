from __future__ import annotations

from datetime import datetime

from ..models import TradeTicket
from ..storage import Database, StoredProposal
from ..time import ensure_utc


class ApprovalService:
    def __init__(self, database: Database):
        self.database = database

    def submit(self, ticket: TradeTicket) -> None:
        self.database.create_proposal(ticket)
        self.database.append_event(
            kind="PROPOSAL_CREATED",
            aggregate_id=ticket.proposal_id,
            payload={"ticket": ticket.to_dict(), "ticket_hash": ticket.fingerprint},
            now=ticket.created_at,
        )

    def approve(
        self,
        proposal_id: str,
        *,
        approver: str,
        expected_hash: str,
        now: datetime,
    ) -> StoredProposal:
        stored = self.database.decide_proposal(
            proposal_id,
            approve=True,
            approver=approver,
            expected_hash=expected_hash,
            now=ensure_utc(now),
        )
        self.database.append_event(
            kind="PROPOSAL_APPROVED",
            aggregate_id=proposal_id,
            payload={
                "approver": approver,
                "approved_ticket_hash": expected_hash,
                "approved_at": ensure_utc(now),
            },
            now=now,
        )
        return stored

    def reject(
        self,
        proposal_id: str,
        *,
        approver: str,
        expected_hash: str,
        reason: str,
        now: datetime,
    ) -> StoredProposal:
        if not reason.strip():
            raise ValueError("a rejection reason is required for the decision journal")
        stored = self.database.decide_proposal(
            proposal_id,
            approve=False,
            approver=approver,
            expected_hash=expected_hash,
            reason=reason.strip(),
            now=ensure_utc(now),
        )
        self.database.append_event(
            kind="PROPOSAL_REJECTED",
            aggregate_id=proposal_id,
            payload={"approver": approver, "reason": reason.strip(), "ticket_hash": expected_hash},
            now=now,
        )
        return stored


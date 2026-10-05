"""Settle open paper tickets from official resolutions (spec §4). There is no voiding.

The governing observation for a market is chosen inside the settlement transaction, by
observation time, not insertion order. An observation's evidence (status, outcome,
cross-check) is gathered over [resolution_requested_at, latest of resolution_fetched_at
and gamma_fetched_at]: the cross-check depends on the Gamma fetch, which finishes after
the resolution fetch. The governing observation is the one whose evidence finished last;
among ties, the one that started last. If any observation with different evidence
overlaps the governing one, the order is ambiguous and the ticket waits. Only a governing
observation that is resolved, CONFIRMED against Gamma and maps to YES/NO/HALF settles."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from .cash import append_cash_entry, money_text, parse_money
from .db import append_journal, transaction
from .util import isoformat, parse_datetime

SETTLEABLE_OUTCOMES = ("YES", "NO", "HALF")


class PendingReason(StrEnum):
    AWAITING_RESOLUTION = "AWAITING_RESOLUTION"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    AMBIGUOUS_EVIDENCE = "AMBIGUOUS_EVIDENCE"
    UNSETTLEABLE = "UNSETTLEABLE"


@dataclass(frozen=True)
class PendingSettlement:
    ticket_id: int
    condition_id: str
    reason: PendingReason
    resolved_since: datetime | None  # first observation reporting `resolved`, if any

    def age(self, now: datetime) -> timedelta | None:
        return None if self.resolved_since is None else now - self.resolved_since


@dataclass(frozen=True)
class SettlementSummary:
    settled: int
    pending: tuple[PendingSettlement, ...]

    def count(self, reason: PendingReason) -> int:
        return sum(1 for item in self.pending if item.reason is reason)


def payout_per_share(held: str, outcome: str) -> Decimal:
    if held not in ("YES", "NO"):
        raise ValueError(f"no payout for holding {held!r}")
    if outcome == "HALF":
        return Decimal("0.5")
    if outcome in ("YES", "NO"):
        return Decimal("1") if held == outcome else Decimal("0")
    raise ValueError(f"no payout for holding {held!r} at outcome {outcome!r}")


def _evidence(row: sqlite3.Row) -> tuple[str, str | None, str]:
    return row["status"], row["outcome"], row["cross_check"]


def _interval(row: sqlite3.Row) -> tuple[datetime, datetime]:
    """When this observation's evidence was gathered: from the resolution request to the
    later of the resolution and Gamma fetches."""
    end = parse_datetime(row["resolution_fetched_at"])
    if row["gamma_fetched_at"] is not None:
        end = max(end, parse_datetime(row["gamma_fetched_at"]))
    requested = row["resolution_requested_at"]
    # Rows from before schema v3 carry no request time; their evidence starts at the
    # resolution fetch.
    start = parse_datetime(row["resolution_fetched_at"] if requested is None else requested)
    return start, end


def governing_observation(
    rows: Sequence[sqlite3.Row],
) -> tuple[sqlite3.Row | None, bool]:
    """(governing observation, ambiguous). None when there are no observations.

    Governing: the latest evidence end; among ties, the latest start, then the lowest id,
    so the result never depends on insertion order. An observation that began after a
    contradiction ended supersedes it."""
    if not rows:
        return None, False
    latest_end = max(_interval(row)[1] for row in rows)
    candidates = [row for row in rows if _interval(row)[1] == latest_end]
    if len({_evidence(row) for row in candidates}) > 1:
        return min(candidates, key=lambda row: row["id"]), True
    governing = min(candidates, key=lambda row: (-_interval(row)[0].timestamp(), row["id"]))
    start = _interval(governing)[0]
    for row in rows:
        if _evidence(row) != _evidence(governing) and _interval(row)[1] >= start:
            return governing, True
    return governing, False


def _resolved_since(rows: Sequence[sqlite3.Row]) -> datetime | None:
    times = [_interval(row)[1] for row in rows if row["status"] == "resolved"]
    return min(times) if times else None


def _pending_reason(
    observation: sqlite3.Row | None, ambiguous: bool
) -> PendingReason | None:
    if observation is None:
        return PendingReason.AWAITING_RESOLUTION
    if ambiguous:
        return PendingReason.AMBIGUOUS_EVIDENCE
    if observation["status"] != "resolved":
        return PendingReason.AWAITING_RESOLUTION
    if observation["cross_check"] == "UNCHECKED":
        return PendingReason.AWAITING_CONFIRMATION
    if (
        observation["cross_check"] != "CONFIRMED"
        or observation["outcome"] not in SETTLEABLE_OUTCOMES
    ):
        return PendingReason.UNSETTLEABLE
    return None


def settle_open_tickets(conn: sqlite3.Connection, now: datetime) -> SettlementSummary:
    settled = 0
    pending: list[PendingSettlement] = []
    ticket_ids = [
        row["ticket_id"]
        for row in conn.execute(
            "SELECT ticket_id FROM paper_tickets WHERE status = 'OPEN' ORDER BY ticket_id"
        )
    ]
    for ticket_id in ticket_ids:
        with transaction(conn):
            ticket = conn.execute(
                "SELECT * FROM paper_tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if ticket["status"] != "OPEN":
                continue
            rows = conn.execute(
                "SELECT * FROM resolution_observations WHERE condition_id = ? ORDER BY id",
                (ticket["condition_id"],),
            ).fetchall()
            observation, ambiguous = governing_observation(rows)
            reason = _pending_reason(observation, ambiguous)
            if reason is not None or observation is None:
                pending.append(
                    PendingSettlement(
                        ticket_id,
                        ticket["condition_id"],
                        reason or PendingReason.AWAITING_RESOLUTION,
                        _resolved_since(rows),
                    )
                )
                continue
            per_share = payout_per_share(ticket["outcome"], observation["outcome"])
            payout = parse_money(ticket["shares"]) * per_share
            net_pnl = payout - parse_money(ticket["cost_total"])
            market = conn.execute(
                "SELECT current_rules_hash FROM markets WHERE condition_id = ?",
                (ticket["condition_id"],),
            ).fetchone()
            rules_hash = market["current_rules_hash"] if market else ticket["rules_hash"]
            conn.execute(
                "INSERT INTO settlements (ticket_id, observation_id, outcome, payout_per_share, "
                "payout, net_pnl, rules_hash_at_settlement, settled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    observation["id"],
                    observation["outcome"],
                    money_text(per_share),
                    money_text(payout),
                    money_text(net_pnl),
                    rules_hash,
                    isoformat(now),
                ),
            )
            conn.execute(
                "UPDATE paper_tickets SET status = 'SETTLED' WHERE ticket_id = ?", (ticket_id,)
            )
            append_cash_entry(conn, ticket["portfolio_id"], "CREDIT", payout, ticket_id, now)
            append_journal(
                conn,
                "TICKET_SETTLED",
                {
                    "ticket_id": ticket_id,
                    "observation_id": observation["id"],
                    "outcome": observation["outcome"],
                    "payout": money_text(payout),
                    "net_pnl": money_text(net_pnl),
                },
                now,
            )
            settled += 1
    return SettlementSummary(settled, tuple(pending))

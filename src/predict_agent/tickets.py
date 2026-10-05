"""Paper tickets and entry decisions, per portfolio. Opening a ticket writes the ticket, its
cash debit, its TRADED decision and the journal entry in one transaction (spec §4)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .cash import LedgerError, append_cash_entry, available_cash, money_text, parse_money
from .db import append_journal, transaction
from .util import canonical_json, isoformat, sha256_json

OUTCOMES = ("YES", "NO")
TICKET_HASH_COLUMNS = (
    "portfolio_id",
    "forecast_id",
    "snapshot_id",
    "condition_id",
    "outcome",
    "direction",
    "shares",
    "fills_json",
    "fee",
    "cost_total",
    "policy_hash",
    "rules_hash",
    "created_at",
)


@dataclass(frozen=True)
class Fill:
    price: Decimal
    shares: Decimal


@dataclass(frozen=True)
class TicketDraft:
    portfolio_id: str
    forecast_id: int
    snapshot_id: int
    condition_id: str
    outcome: str
    fills: tuple[Fill, ...]
    fee: Decimal
    policy_hash: str
    rules_hash: str


def ticket_totals(draft: TicketDraft) -> tuple[Decimal, Decimal]:
    if draft.outcome not in OUTCOMES:
        raise LedgerError(f"outcome must be YES or NO, got {draft.outcome!r}")
    if not draft.fills:
        raise LedgerError("a ticket needs at least one fill")
    if not draft.fee.is_finite() or draft.fee < 0:
        raise LedgerError(f"invalid fee {draft.fee}")
    shares = Decimal("0")
    notional = Decimal("0")
    for fill in draft.fills:
        if not fill.price.is_finite() or not Decimal("0") < fill.price < Decimal("1"):
            raise LedgerError(f"fill price {fill.price} outside (0, 1)")
        if not fill.shares.is_finite() or fill.shares <= 0:
            raise LedgerError(f"fill shares {fill.shares} must be positive")
        shares += fill.shares
        notional += fill.price * fill.shares
    return shares, notional + draft.fee


def ticket_hash(values: Mapping[str, Any]) -> str:
    return sha256_json({column: values[column] for column in TICKET_HASH_COLUMNS})


def _portfolio(conn: sqlite3.Connection, portfolio_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT p.*, c.status AS cohort_status FROM portfolios p "
        "JOIN cohorts c ON c.cohort_id = p.cohort_id WHERE p.portfolio_id = ?",
        (portfolio_id,),
    ).fetchone()
    if row is None:
        raise LedgerError(f"unknown portfolio {portfolio_id[:12]}")
    found: sqlite3.Row = row
    return found


def _decided(conn: sqlite3.Connection, portfolio_id: str, forecast_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM decisions WHERE portfolio_id = ? AND forecast_id = ?",
        (portfolio_id, forecast_id),
    )
    return row.fetchone() is not None


def open_ticket_locked(conn: sqlite3.Connection, draft: TicketDraft, now: datetime) -> int:
    """open_ticket for a caller that already holds the transaction."""
    shares, cost_total = ticket_totals(draft)
    portfolio = _portfolio(conn, draft.portfolio_id)
    if portfolio["cohort_status"] != "ACTIVE":
        raise LedgerError(f"portfolio {draft.portfolio_id[:12]}'s cohort is not active")
    if draft.policy_hash != portfolio["policy_hash"]:
        raise LedgerError("ticket policy differs from the portfolio's frozen policy")
    forecast = conn.execute(
        "SELECT * FROM forecasts WHERE forecast_id = ?", (draft.forecast_id,)
    ).fetchone()
    if (
        forecast is None
        or forecast["cohort_id"] != portfolio["cohort_id"]
        or forecast["condition_id"] != draft.condition_id
        or forecast["kind"] != "entry"
        or forecast["abstained"]
    ):
        raise LedgerError("ticket needs a non-abstained entry forecast of this cohort/market")
    if forecast["rules_hash"] != draft.rules_hash:
        raise LedgerError("ticket rules hash differs from the forecast's rules version")
    if _decided(conn, draft.portfolio_id, draft.forecast_id):
        raise LedgerError(f"forecast {draft.forecast_id} is already decided here")
    baseline = conn.execute(
        "SELECT yes_snapshot_id, no_snapshot_id FROM forecast_baselines "
        "WHERE forecast_id = ? AND reason IS NULL",
        (draft.forecast_id,),
    ).fetchone()
    side_snapshot = None
    if baseline is not None:
        column = "yes_snapshot_id" if draft.outcome == "YES" else "no_snapshot_id"
        side_snapshot = baseline[column]
    if side_snapshot != draft.snapshot_id:
        raise LedgerError(
            "ticket snapshot must be the forecast's baseline snapshot for the bought side"
        )
    values: dict[str, Any] = {
        "portfolio_id": draft.portfolio_id,
        "forecast_id": draft.forecast_id,
        "snapshot_id": draft.snapshot_id,
        "condition_id": draft.condition_id,
        "outcome": draft.outcome,
        "direction": "BUY",
        "shares": money_text(shares),
        "fills_json": canonical_json(
            [[money_text(f.price), money_text(f.shares)] for f in draft.fills]
        ),
        "fee": money_text(draft.fee),
        "cost_total": money_text(cost_total),
        "policy_hash": draft.policy_hash,
        "rules_hash": draft.rules_hash,
        "created_at": isoformat(now),
    }
    values["ticket_hash"] = ticket_hash(values)
    values["status"] = "OPEN"
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    try:
        cursor = conn.execute(
            f"INSERT INTO paper_tickets ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
    except sqlite3.IntegrityError as error:
        raise LedgerError(
            f"ticket insert refused (market already traded in this portfolio?): {error}"
        ) from None
    if cursor.lastrowid is None:
        raise LedgerError("ticket insert returned no row id")
    ticket_id = cursor.lastrowid
    append_cash_entry(conn, draft.portfolio_id, "DEBIT", cost_total, ticket_id, now)
    conn.execute(
        "INSERT INTO decisions (portfolio_id, forecast_id, condition_id, kind, reason, "
        "ticket_id, decided_at) VALUES (?, ?, ?, 'TRADED', NULL, ?, ?)",
        (draft.portfolio_id, draft.forecast_id, draft.condition_id, ticket_id, isoformat(now)),
    )
    append_journal(
        conn,
        "TICKET_OPENED",
        {
            "ticket_id": ticket_id,
            "portfolio_id": draft.portfolio_id,
            "condition_id": draft.condition_id,
            "outcome": draft.outcome,
            "cost_total": values["cost_total"],
            "ticket_hash": values["ticket_hash"],
        },
        now,
    )
    return ticket_id


def open_ticket(conn: sqlite3.Connection, draft: TicketDraft, now: datetime) -> int:
    with transaction(conn):
        return open_ticket_locked(conn, draft, now)


def record_refusal_locked(
    conn: sqlite3.Connection,
    portfolio_id: str,
    forecast_id: int,
    reason: str,
    now: datetime,
    detail: str = "",
) -> None:
    """record_refusal_decision for a caller that already holds the transaction. `reason`
    is the code stored on the decision; `detail` goes to the journal."""
    if not reason:
        raise LedgerError("a refusal decision needs a reason")
    portfolio = _portfolio(conn, portfolio_id)
    forecast = conn.execute(
        "SELECT cohort_id, condition_id, kind FROM forecasts WHERE forecast_id = ?",
        (forecast_id,),
    ).fetchone()
    if (
        forecast is None
        or forecast["kind"] != "entry"
        or forecast["cohort_id"] != portfolio["cohort_id"]
    ):
        raise LedgerError(f"forecast {forecast_id} is not an entry forecast of this cohort")
    if _decided(conn, portfolio_id, forecast_id):
        raise LedgerError(f"forecast {forecast_id} is already decided here")
    if not conn.execute(
        "SELECT 1 FROM forecast_baselines WHERE forecast_id = ?", (forecast_id,)
    ).fetchone():
        raise LedgerError(
            f"forecast {forecast_id} has no baseline yet; decisions follow the baseline"
        )
    conn.execute(
        "INSERT INTO decisions (portfolio_id, forecast_id, condition_id, kind, reason, "
        "ticket_id, decided_at) VALUES (?, ?, ?, 'REFUSED', ?, NULL, ?)",
        (portfolio_id, forecast_id, forecast["condition_id"], reason, isoformat(now)),
    )
    append_journal(
        conn,
        "DECISION_REFUSED",
        {
            "portfolio_id": portfolio_id,
            "forecast_id": forecast_id,
            "reason": reason,
            "detail": detail,
        },
        now,
    )


def record_refusal_decision(
    conn: sqlite3.Connection,
    portfolio_id: str,
    forecast_id: int,
    reason: str,
    now: datetime,
    detail: str = "",
) -> None:
    with transaction(conn):
        record_refusal_locked(conn, portfolio_id, forecast_id, reason, now, detail)


def open_cost(conn: sqlite3.Connection, portfolio_id: str) -> Decimal:
    total = Decimal("0")
    for row in conn.execute(
        "SELECT cost_total FROM paper_tickets WHERE portfolio_id = ? AND status = 'OPEN'",
        (portfolio_id,),
    ):
        total += parse_money(row["cost_total"])
    return total


def equity(conn: sqlite3.Connection, portfolio_id: str) -> Decimal:
    return available_cash(conn, portfolio_id) + open_cost(conn, portfolio_id)

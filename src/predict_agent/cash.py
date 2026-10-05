"""Per-portfolio virtual cash. Amounts are Decimal text and are summed in Python: SQLite's
SUM() would convert TEXT to float.

Every entry must be backed by the record it accounts for: the one FUNDING equals the
portfolio's starting bankroll and comes first; a DEBIT equals its OPEN ticket's cost; a
CREDIT equals its ticket's settlement payout. Nothing else can move cash."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .db import GENESIS_HASH, append_journal
from .util import isoformat, sha256_json

ENTRY_SIGNS = {"FUNDING": 1, "DEBIT": -1, "CREDIT": 1}


class LedgerError(RuntimeError):
    pass


class InsufficientCash(LedgerError):
    pass


def money_text(value: Decimal) -> str:
    if not value.is_finite():
        raise LedgerError(f"non-finite amount {value}")
    return format(value, "f")


def parse_money(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise LedgerError(f"stored amount is not a decimal: {text!r}") from None
    if not value.is_finite():
        raise LedgerError(f"stored amount is not finite: {text!r}")
    return value


def available_cash(conn: sqlite3.Connection, portfolio_id: str) -> Decimal:
    total = Decimal("0")
    for row in conn.execute(
        "SELECT entry_type, amount FROM cash_ledger WHERE portfolio_id = ?", (portfolio_id,)
    ):
        total += ENTRY_SIGNS[row["entry_type"]] * parse_money(row["amount"])
    return total


def cash_entry_hash(
    portfolio_id: str,
    entry_type: str,
    amount_text: str,
    ticket_id: int | None,
    at_text: str,
    prev_hash: str,
) -> str:
    return sha256_json(
        {
            "portfolio_id": portfolio_id,
            "entry_type": entry_type,
            "amount": amount_text,
            "ticket_id": ticket_id,
            "at": at_text,
            "prev_hash": prev_hash,
        }
    )


def _check_backing(
    conn: sqlite3.Connection,
    portfolio_id: str,
    entry_type: str,
    amount: Decimal,
    ticket_id: int | None,
) -> None:
    portfolio = conn.execute(
        "SELECT starting_bankroll FROM portfolios WHERE portfolio_id = ?", (portfolio_id,)
    ).fetchone()
    if portfolio is None:
        raise LedgerError(f"unknown portfolio {portfolio_id[:12]}")
    if entry_type == "FUNDING":
        if ticket_id is not None:
            raise LedgerError("FUNDING is not tied to a ticket")
        if conn.execute(
            "SELECT 1 FROM cash_ledger WHERE portfolio_id = ?", (portfolio_id,)
        ).fetchone():
            raise LedgerError(f"portfolio {portfolio_id[:12]} is already funded")
        if amount != parse_money(portfolio["starting_bankroll"]):
            raise LedgerError("FUNDING must equal the portfolio's starting bankroll")
        return
    if ticket_id is None:
        raise LedgerError(f"a {entry_type} needs a ticket")
    ticket = conn.execute(
        "SELECT portfolio_id, status, cost_total FROM paper_tickets WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    if ticket is None:
        raise LedgerError(f"unknown ticket {ticket_id}")
    if ticket["portfolio_id"] != portfolio_id:
        raise LedgerError(f"ticket {ticket_id} belongs to another portfolio")
    if conn.execute(
        "SELECT 1 FROM cash_ledger WHERE ticket_id = ? AND entry_type = ?",
        (ticket_id, entry_type),
    ).fetchone():
        raise LedgerError(f"ticket {ticket_id} already has a {entry_type}")
    if entry_type == "DEBIT":
        if ticket["status"] != "OPEN" or amount != parse_money(ticket["cost_total"]):
            raise LedgerError(f"DEBIT must equal OPEN ticket {ticket_id}'s cost_total")
        return
    settlement = conn.execute(
        "SELECT payout FROM settlements WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    if settlement is None:
        raise LedgerError(f"CREDIT for ticket {ticket_id} has no settlement")
    if amount != parse_money(settlement["payout"]):
        raise LedgerError(f"CREDIT must equal ticket {ticket_id}'s settlement payout")


def append_cash_entry(
    conn: sqlite3.Connection,
    portfolio_id: str,
    entry_type: str,
    amount: Decimal,
    ticket_id: int | None,
    now: datetime,
) -> int:
    """Append one backed cash entry and its journal entry. The caller holds the transaction
    and has already written the backing record (portfolio, ticket or settlement)."""
    if entry_type not in ENTRY_SIGNS:
        raise LedgerError(f"unknown cash entry type {entry_type!r}")
    amount_text = money_text(amount)
    if amount < 0 or (entry_type == "FUNDING" and amount == 0):
        raise LedgerError(f"invalid {entry_type} amount {amount_text}")
    _check_backing(conn, portfolio_id, entry_type, amount, ticket_id)
    if entry_type == "DEBIT":
        available = available_cash(conn, portfolio_id)
        if available < amount:
            raise InsufficientCash(
                f"portfolio {portfolio_id[:12]}: debit {amount_text} exceeds available "
                f"{money_text(available)}"
            )
    last = conn.execute(
        "SELECT entry_hash FROM cash_ledger WHERE portfolio_id = ? "
        "ORDER BY entry_id DESC LIMIT 1",
        (portfolio_id,),
    ).fetchone()
    prev_hash = last["entry_hash"] if last else GENESIS_HASH
    at_text = isoformat(now)
    entry_hash = cash_entry_hash(
        portfolio_id, entry_type, amount_text, ticket_id, at_text, prev_hash
    )
    cursor = conn.execute(
        "INSERT INTO cash_ledger (portfolio_id, entry_type, amount, ticket_id, at, entry_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (portfolio_id, entry_type, amount_text, ticket_id, at_text, entry_hash),
    )
    append_journal(
        conn,
        f"CASH_{entry_type}",
        {
            "portfolio_id": portfolio_id,
            "amount": amount_text,
            "ticket_id": ticket_id,
            "entry_hash": entry_hash,
        },
        now,
    )
    if cursor.lastrowid is None:
        raise LedgerError("cash entry insert returned no row id")
    return cursor.lastrowid

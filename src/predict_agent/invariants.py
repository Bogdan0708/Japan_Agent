"""Ledger invariant checks used by `predict-agent doctor`. Triggers make tampering hard;
these checks make it visible if it happens anyway. They verify every hash (artifacts,
cohort identities, forecasts, tickets, cash chains) and every accounting relationship
between records, independently of the code paths that wrote them."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

from .artifacts import load_artifact
from .cash import available_cash, cash_entry_hash, parse_money
from .cohorts import portfolio_id_for
from .db import GENESIS_HASH, SCHEMA
from .fills import level_fee
from .forecasts import forecast_hash
from .gamma import fee_rate
from .ledger_schema import LEDGER_SCHEMA
from .policy_params import policy_from_artifact
from .settlement import SETTLEABLE_OUTCOMES, payout_per_share
from .tickets import ticket_hash
from .util import parse_datetime, sha256_json, sha256_text

EXPECTED_TRIGGERS = frozenset(
    re.findall(r"CREATE TRIGGER IF NOT EXISTS (\w+)", SCHEMA + LEDGER_SCHEMA)
)


def _trigger_problems(conn: sqlite3.Connection) -> list[str]:
    present = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    return [f"missing immutability trigger {name}" for name in sorted(EXPECTED_TRIGGERS - present)]


def _artifact_problems(conn: sqlite3.Connection) -> list[str]:
    return [
        f"artifact {row['artifact_hash'][:12]}: content does not match its hash"
        for row in conn.execute("SELECT artifact_hash, content FROM artifacts")
        if sha256_text(row["content"]) != row["artifact_hash"]
    ]


def _kind(conn: sqlite3.Connection, digest: str) -> str | None:
    row = conn.execute("SELECT kind FROM artifacts WHERE artifact_hash = ?", (digest,)).fetchone()
    return None if row is None else str(row["kind"])


def _cohort_problems(conn: sqlite3.Connection, cohort: sqlite3.Row) -> list[str]:
    label = f"cohort {cohort['cohort_id'][:12]}"
    identity = json.loads(cohort["identity_json"])
    problems: list[str] = []
    if sha256_json(identity) != cohort["cohort_id"]:
        problems.append(f"{label}: identity does not match cohort id")
    if identity.get("baseline_window_seconds") != cohort["baseline_window_seconds"]:
        problems.append(f"{label}: baseline window differs from its identity")
    if _kind(conn, identity.get("prompt_hash", "")) != "prompt":
        problems.append(f"{label}: prompt artifact missing")
    portfolios = {
        row["variant"]: row
        for row in conn.execute(
            "SELECT * FROM portfolios WHERE cohort_id = ?", (cohort["cohort_id"],)
        )
    }
    declared: dict[str, str] = identity.get("portfolios", {})
    if set(portfolios) != set(declared):
        problems.append(f"{label}: portfolios differ from its identity")
    for variant, row in portfolios.items():
        if row["portfolio_id"] != portfolio_id_for(cohort["cohort_id"], variant):
            problems.append(f"{label}: portfolio {variant} has a wrong id")
        if row["policy_hash"] != declared.get(variant) or _kind(conn, row["policy_hash"]) != (
            "policy"
        ):
            problems.append(f"{label}: portfolio {variant} policy differs from its identity")
        if row["starting_bankroll"] != cohort["starting_bankroll"]:
            problems.append(f"{label}: portfolio {variant} bankroll differs from the cohort's")
    return problems


def _cash_problems(conn: sqlite3.Connection, portfolio: sqlite3.Row) -> list[str]:
    portfolio_id = portfolio["portfolio_id"]
    label = f"portfolio {portfolio_id[:12]}"
    problems: list[str] = []
    rows = conn.execute(
        "SELECT * FROM cash_ledger WHERE portfolio_id = ? ORDER BY entry_id", (portfolio_id,)
    ).fetchall()
    prev_hash = GENESIS_HASH
    for row in rows:
        expected = cash_entry_hash(
            portfolio_id, row["entry_type"], row["amount"], row["ticket_id"], row["at"], prev_hash
        )
        if expected != row["entry_hash"]:
            problems.append(f"{label}: cash chain broken at entry {row['entry_id']}")
            break
        prev_hash = row["entry_hash"]
    fundings = [row for row in rows if row["entry_type"] == "FUNDING"]
    if (
        not rows
        or len(fundings) != 1
        or rows[0]["entry_type"] != "FUNDING"
        or parse_money(fundings[0]["amount"]) != parse_money(portfolio["starting_bankroll"])
    ):
        problems.append(f"{label}: needs exactly one first FUNDING equal to its bankroll")
    balance = Decimal("0")
    for row in rows:
        amount = parse_money(row["amount"])
        balance += -amount if row["entry_type"] == "DEBIT" else amount
        if balance < 0:
            problems.append(f"{label}: cash went negative at entry {row['entry_id']}")
            break
        if row["ticket_id"] is not None:
            owner = conn.execute(
                "SELECT portfolio_id FROM paper_tickets WHERE ticket_id = ?", (row["ticket_id"],)
            ).fetchone()
            if owner is None or owner["portfolio_id"] != portfolio_id:
                problems.append(
                    f"{label}: entry {row['entry_id']} is for a ticket of another portfolio"
                )
    if available_cash(conn, portfolio_id) < 0:
        problems.append(f"{label}: available cash is negative")
    return problems


def _forecast_problems(conn: sqlite3.Connection, forecast: sqlite3.Row) -> list[str]:
    label = f"forecast {forecast['forecast_id']}"
    problems: list[str] = []
    if forecast_hash(dict(forecast)) != forecast["forecast_hash"]:
        problems.append(f"{label}: forecast hash mismatch")
    if _kind(conn, forecast["research_input_hash"]) != "research_input":
        problems.append(f"{label}: research input artifact missing")
    if _kind(conn, forecast["transcript_hash"]) != "tool_transcript":
        problems.append(f"{label}: tool transcript artifact missing")
    attempt = conn.execute(
        "SELECT * FROM research_attempts WHERE attempt_id = ?", (forecast["attempt_id"],)
    ).fetchone()
    if (
        attempt is None
        or attempt["status"] != "SUCCEEDED"
        or attempt["cohort_id"] != forecast["cohort_id"]
        or attempt["condition_id"] != forecast["condition_id"]
        or attempt["cost_usd"] != forecast["cost_usd"]
    ):
        problems.append(f"{label}: research attempt does not match")
    baseline = conn.execute(
        "SELECT * FROM forecast_baselines WHERE forecast_id = ?", (forecast["forecast_id"],)
    ).fetchone()
    if baseline is not None and baseline["reason"] is None:
        window = conn.execute(
            "SELECT baseline_window_seconds FROM cohorts WHERE cohort_id = ?",
            (forecast["cohort_id"],),
        ).fetchone()["baseline_window_seconds"]
        start = parse_datetime(forecast["created_at"])
        deadline = start + timedelta(seconds=window)
        for column, outcome in (("yes_snapshot_id", "YES"), ("no_snapshot_id", "NO")):
            snap = conn.execute(
                "SELECT condition_id, outcome, fetched_at FROM book_snapshots WHERE id = ?",
                (baseline[column],),
            ).fetchone()
            if (
                snap is None
                or snap["condition_id"] != forecast["condition_id"]
                or snap["outcome"] != outcome
                or not start <= parse_datetime(snap["fetched_at"]) <= deadline
            ):
                problems.append(f"{label}: {outcome} baseline is outside the baseline window")
    return problems


def _ticket_problems(conn: sqlite3.Connection, ticket: sqlite3.Row) -> list[str]:
    ticket_id = ticket["ticket_id"]
    label = f"ticket {ticket_id}"
    problems: list[str] = []
    if ticket_hash(dict(ticket)) != ticket["ticket_hash"]:
        problems.append(f"{label}: ticket hash mismatch")
    portfolio = conn.execute(
        "SELECT * FROM portfolios WHERE portfolio_id = ?", (ticket["portfolio_id"],)
    ).fetchone()
    if portfolio is None or ticket["policy_hash"] != portfolio["policy_hash"]:
        problems.append(f"{label}: policy differs from its portfolio's frozen policy")
    forecast = conn.execute(
        "SELECT * FROM forecasts WHERE forecast_id = ?", (ticket["forecast_id"],)
    ).fetchone()
    baseline = conn.execute(
        "SELECT * FROM forecast_baselines WHERE forecast_id = ? AND reason IS NULL",
        (ticket["forecast_id"],),
    ).fetchone()
    side = "yes_snapshot_id" if ticket["outcome"] == "YES" else "no_snapshot_id"
    if (
        forecast is None
        or portfolio is None
        or forecast["cohort_id"] != portfolio["cohort_id"]
        or forecast["condition_id"] != ticket["condition_id"]
        or forecast["kind"] != "entry"
        or forecast["abstained"]
        or forecast["rules_hash"] != ticket["rules_hash"]
        or baseline is None
        or baseline[side] != ticket["snapshot_id"]
    ):
        problems.append(f"{label}: not backed by its forecast and baseline")
    decision = conn.execute(
        "SELECT kind, ticket_id FROM decisions WHERE portfolio_id = ? AND forecast_id = ?",
        (ticket["portfolio_id"], ticket["forecast_id"]),
    ).fetchone()
    if decision is None or decision["kind"] != "TRADED" or decision["ticket_id"] != ticket_id:
        problems.append(f"{label}: no matching TRADED decision")
    debits = conn.execute(
        "SELECT amount FROM cash_ledger WHERE ticket_id = ? AND entry_type = 'DEBIT'",
        (ticket_id,),
    ).fetchall()
    if [parse_money(d["amount"]) for d in debits] != [parse_money(ticket["cost_total"])]:
        problems.append(f"{label}: debit does not match cost_total")
    credits = [
        parse_money(c["amount"])
        for c in conn.execute(
            "SELECT amount FROM cash_ledger WHERE ticket_id = ? AND entry_type = 'CREDIT'",
            (ticket_id,),
        )
    ]
    settlement = conn.execute(
        "SELECT * FROM settlements WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    if ticket["status"] == "SETTLED" and settlement is None:
        problems.append(f"{label}: SETTLED without a settlement row")
    if ticket["status"] == "OPEN" and settlement is not None:
        problems.append(f"{label}: OPEN but has a settlement row")
    if settlement is None:
        if credits:
            problems.append(f"{label}: credit without a settlement")
        return problems
    if credits != [parse_money(settlement["payout"])]:
        problems.append(f"{label}: credit does not match settlement payout")
    problems += _settlement_problems(conn, ticket, settlement)
    return problems


def _settlement_problems(
    conn: sqlite3.Connection, ticket: sqlite3.Row, settlement: sqlite3.Row
) -> list[str]:
    label = f"ticket {ticket['ticket_id']}"
    problems: list[str] = []
    observation = conn.execute(
        "SELECT * FROM resolution_observations WHERE id = ?", (settlement["observation_id"],)
    ).fetchone()
    if (
        observation is None
        or observation["condition_id"] != ticket["condition_id"]
        or observation["status"] != "resolved"
        or observation["cross_check"] != "CONFIRMED"
        or observation["outcome"] != settlement["outcome"]
        or settlement["outcome"] not in SETTLEABLE_OUTCOMES
    ):
        problems.append(f"{label}: settlement is not backed by a confirmed resolution")
        return problems
    per_share = payout_per_share(ticket["outcome"], settlement["outcome"])
    payout = parse_money(ticket["shares"]) * per_share
    if (
        parse_money(settlement["payout_per_share"]) != per_share
        or parse_money(settlement["payout"]) != payout
        or parse_money(settlement["net_pnl"]) != payout - parse_money(ticket["cost_total"])
    ):
        problems.append(f"{label}: settlement payout arithmetic is wrong")
    return problems


def _fill_problems(conn: sqlite3.Connection, ticket: sqlite3.Row) -> list[str]:
    """A ticket's fills must come from its own recorded book: existing levels, no more than
    their recorded size, per-level fees from the snapshot's schedule, within the policy's
    slippage limit, decided while the book was fresh (spec §5: never fabricate fills)."""
    label = f"ticket {ticket['ticket_id']}"
    snapshot = conn.execute(
        "SELECT * FROM book_snapshots WHERE id = ?", (ticket["snapshot_id"],)
    ).fetchone()
    if snapshot is None:
        return [f"{label}: its snapshot is missing"]
    record = json.loads(snapshot["record_json"])
    depth = {Decimal(price): Decimal(size) for price, size in record["asks"]}
    fills = [
        (Decimal(price), Decimal(shares)) for price, shares in json.loads(ticket["fills_json"])
    ]
    problems: list[str] = []
    prices = [price for price, _ in fills]
    if (
        not fills
        or len(set(prices)) != len(prices)
        or any(price not in depth or shares > depth[price] for price, shares in fills)
    ):
        problems.append(f"{label}: fills are not within the recorded book")
    rate = Decimal("0")
    if snapshot["fees_enabled"]:
        schedule = snapshot["fee_schedule_json"]
        found = fee_rate(json.loads(schedule) if schedule else None)
        if found is None:
            return [*problems, f"{label}: snapshot has fees but no fee rate"]
        rate = found
    fee = sum((level_fee(shares, price, rate) for price, shares in fills), Decimal("0"))
    notional = sum((price * shares for price, shares in fills), Decimal("0"))
    if parse_money(ticket["fee"]) != fee:
        problems.append(f"{label}: fee differs from the snapshot's per-level fee")
    if (
        parse_money(ticket["cost_total"]) != notional + parse_money(ticket["fee"])
        or parse_money(ticket["shares"]) != sum((s for _, s in fills), Decimal("0"))
    ):
        problems.append(f"{label}: shares or cost do not match its fills")
    params = policy_from_artifact(load_artifact(conn, ticket["policy_hash"])[1])
    if depth and fills:
        limit = min(depth) * (Decimal("1") + params.max_slippage)
        if max(prices) > limit:
            problems.append(f"{label}: fills beyond the policy's slippage limit")
    age = parse_datetime(ticket["created_at"]) - parse_datetime(snapshot["fetched_at"])
    if not timedelta(0) <= age <= timedelta(seconds=params.max_book_age_seconds):
        problems.append(f"{label}: decided on a book {age} old")
    return problems


def _decision_problems(conn: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    for row in conn.execute(
        "SELECT d.*, p.cohort_id AS portfolio_cohort, f.cohort_id AS forecast_cohort, "
        "f.kind AS forecast_kind FROM decisions d "
        "JOIN portfolios p ON p.portfolio_id = d.portfolio_id "
        "JOIN forecasts f ON f.forecast_id = d.forecast_id"
    ):
        if row["portfolio_cohort"] != row["forecast_cohort"] or row["forecast_kind"] != "entry":
            problems.append(f"decision on forecast {row['forecast_id']}: wrong cohort or kind")
    return problems


def _unreadable(label: str, error: Exception) -> list[str]:
    return [f"{label}: unreadable ({type(error).__name__}: {error})"]


def _guarded(label: str, check: Callable[[], list[str]]) -> list[str]:
    """Run one check; corrupt data becomes a problem line instead of an exception."""
    try:
        return check()
    except Exception as error:
        return _unreadable(label, error)


def _foreign_key_problems(conn: sqlite3.Connection) -> list[str]:
    """Rows whose references point nowhere (e.g. written with foreign keys off). The
    per-record checks start from existing parents, so they cannot see orphans."""
    return [
        f"foreign key violation: {row[0]} rowid {row[1]} references missing {row[2]}"
        for row in conn.execute("PRAGMA foreign_key_check").fetchall()
    ]


def _active_cohort_problems(conn: sqlite3.Connection) -> list[str]:
    count = conn.execute("SELECT COUNT(*) FROM cohorts WHERE status = 'ACTIVE'").fetchone()[0]
    return ["more than one ACTIVE cohort"] if count > 1 else []


def _each(
    conn: sqlite3.Connection,
    query: str,
    prefix: str,
    key: str,
    check: Callable[[sqlite3.Connection, sqlite3.Row], list[str]],
) -> list[str]:
    try:
        rows = conn.execute(query).fetchall()
    except Exception as error:
        return _unreadable(f"{prefix}s", error)
    problems: list[str] = []
    for row in rows:
        label = f"{prefix} {str(row[key])[:12]}"
        problems += _guarded(label, lambda row=row: check(conn, row))  # type: ignore[misc]
    return problems


def _verify(conn: sqlite3.Connection) -> list[str]:
    problems = _guarded("triggers", lambda: _trigger_problems(conn))
    problems += _guarded("artifacts", lambda: _artifact_problems(conn))
    problems += _guarded("foreign keys", lambda: _foreign_key_problems(conn))
    problems += _guarded("cohorts", lambda: _active_cohort_problems(conn))
    problems += _each(
        conn, "SELECT * FROM cohorts ORDER BY started_at", "cohort", "cohort_id", _cohort_problems
    )
    problems += _each(
        conn,
        "SELECT * FROM portfolios ORDER BY portfolio_id",
        "portfolio",
        "portfolio_id",
        _cash_problems,
    )
    problems += _each(
        conn,
        "SELECT * FROM forecasts ORDER BY forecast_id",
        "forecast",
        "forecast_id",
        _forecast_problems,
    )
    problems += _each(
        conn,
        "SELECT * FROM paper_tickets ORDER BY ticket_id",
        "ticket",
        "ticket_id",
        _ticket_problems,
    )
    problems += _each(
        conn,
        "SELECT * FROM paper_tickets ORDER BY ticket_id",
        "ticket",
        "ticket_id",
        _fill_problems,
    )
    problems += _guarded("decisions", lambda: _decision_problems(conn))
    return problems


def verify_ledger(conn: sqlite3.Connection) -> list[str]:
    """All ledger problems, read in one consistent snapshot. Never raises on bad data:
    an unreadable record is reported as a problem and checking continues."""
    owns_transaction = not conn.in_transaction
    try:
        if owns_transaction:
            conn.execute("BEGIN")
        return _verify(conn)
    except Exception as error:
        return _unreadable("ledger", error)
    finally:
        if owns_transaction and conn.in_transaction:
            conn.execute("ROLLBACK")

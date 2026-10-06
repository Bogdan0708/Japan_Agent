"""Research spend and volume limits (spec §6: daily and per-forecast USD caps counting
failed attempts; at most N new entry forecasts per day).

Spend is read from the durable `research_attempts` rows of every cohort for the UTC day.
An attempt still STARTED (in flight, or interrupted by a crash) counts at the full
per-forecast cap, because its real cost is unknown. Amounts are summed in Python."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .cash import parse_money
from .forecasts import fail_attempt
from .util import parse_datetime

INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True)
class DayUsage:
    spent_usd: Decimal
    entry_attempts: int


def _day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def day_usage(conn: sqlite3.Connection, now: datetime, per_forecast_usd: Decimal) -> DayUsage:
    start, end = _day_bounds(now.astimezone(UTC))
    spent = Decimal("0")
    entries = 0
    for row in conn.execute("SELECT kind, status, cost_usd, started_at FROM research_attempts"):
        if not start <= parse_datetime(row["started_at"]) < end:
            continue
        spent += per_forecast_usd if row["status"] == "STARTED" else parse_money(row["cost_usd"])
        entries += row["kind"] == "entry"
    return DayUsage(spent, entries)


def budget_refusal(
    usage: DayUsage, *, daily_usd: Decimal, per_forecast_usd: Decimal, max_entries: int
) -> str | None:
    """BUDGET when one more full-cost attempt could exceed the daily cap; VOLUME when the
    day's entry forecasts are used up."""
    if usage.spent_usd + per_forecast_usd > daily_usd:
        return "BUDGET"
    if usage.entry_attempts >= max_entries:
        return "VOLUME"
    return None


def recover_interrupted_attempts(
    conn: sqlite3.Connection, per_forecast_usd: Decimal, now: datetime
) -> list[int]:
    """Close every STARTED attempt left by an earlier run as FAILED, charged at the full
    per-forecast cap (its real cost is unknown). Call before starting new attempts; the
    caller must hold the only research run (Plan 5 adds the cron lock)."""
    rows = conn.execute(
        "SELECT attempt_id FROM research_attempts WHERE status = 'STARTED' ORDER BY attempt_id"
    ).fetchall()
    recovered = []
    for row in rows:
        fail_attempt(conn, row["attempt_id"], per_forecast_usd, INTERRUPTED, now)
        recovered.append(row["attempt_id"])
    return recovered

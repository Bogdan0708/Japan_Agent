"""Research spend and volume limits (spec §6: daily and per-forecast USD caps counting
failed attempts; at most N new entry forecasts per day).

Spend is read from the durable `research_attempts` rows of every cohort for the UTC day.
An attempt still STARTED (in flight, or interrupted by a crash) counts at the full
per-forecast cap, because its real cost is unknown. That cap is the one frozen in the
attempt's own cohort identity (the cap its session ran under), not the running config's,
which may belong to a newer cohort. When a cohort's frozen cap is missing or unreadable,
the attempt counts at the highest cap known (the running one or any cohort's), so the
budget fails toward over-counting. Amounts are summed in Python."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

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


def _frozen_cap(identity_json: str) -> Decimal | None:
    """The per-forecast cap frozen in a cohort identity, or None when absent or unreadable."""
    try:
        settings = json.loads(identity_json).get("research_settings")
        value = settings.get("per_forecast_usd") if isinstance(settings, dict) else None
        if not isinstance(value, str):
            return None
        cap = Decimal(value)
    except (ValueError, AttributeError, InvalidOperation):
        return None
    return cap if cap.is_finite() and cap > 0 else None


def attempt_caps(
    conn: sqlite3.Connection, per_forecast_usd: Decimal
) -> dict[str, Decimal]:
    """cohort_id -> the cap an unknown-cost attempt of that cohort is charged at: its own
    frozen cap, or (missing/unreadable) the highest of `per_forecast_usd` and every
    readable frozen cap."""
    frozen = {
        row["cohort_id"]: _frozen_cap(row["identity_json"])
        for row in conn.execute("SELECT cohort_id, identity_json FROM cohorts")
    }
    fallback = max([per_forecast_usd, *(cap for cap in frozen.values() if cap is not None)])
    return {cohort_id: fallback if cap is None else cap for cohort_id, cap in frozen.items()}


def day_usage(conn: sqlite3.Connection, now: datetime, per_forecast_usd: Decimal) -> DayUsage:
    """`per_forecast_usd` is the running config's cap: the fallback for attempts whose
    cohort's frozen cap cannot be read (see `attempt_caps`)."""
    start, end = _day_bounds(now.astimezone(UTC))
    caps = attempt_caps(conn, per_forecast_usd)
    fallback = max([per_forecast_usd, *caps.values()])
    spent = Decimal("0")
    entries = 0
    for row in conn.execute(
        "SELECT cohort_id, kind, status, cost_usd, started_at FROM research_attempts"
    ):
        if not start <= parse_datetime(row["started_at"]) < end:
            continue
        if row["status"] == "STARTED":
            spent += caps.get(row["cohort_id"], fallback)
        else:
            spent += parse_money(row["cost_usd"])
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
    per-forecast cap of its own cohort (its real cost is unknown; see `attempt_caps`;
    `per_forecast_usd` is the running config's cap, used only as a fallback floor). Call
    before starting new attempts; the caller must hold the only research run (Plan 5 adds
    the cron lock)."""
    rows = conn.execute(
        "SELECT attempt_id, cohort_id FROM research_attempts WHERE status = 'STARTED' "
        "ORDER BY attempt_id"
    ).fetchall()
    caps = attempt_caps(conn, per_forecast_usd)
    fallback = max([per_forecast_usd, *caps.values()])
    recovered = []
    for row in rows:
        cap = caps.get(row["cohort_id"], fallback)
        fail_attempt(conn, row["attempt_id"], cap, INTERRUPTED, now)
        recovered.append(row["attempt_id"])
    return recovered

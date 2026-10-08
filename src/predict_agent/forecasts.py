"""Research attempts, forecast records, post-forecast baselines and resume state
(spec §3 ordering, §4, §6 budget, §7 resume).

Every paid research call is a durable `research_attempts` row opened before the call and
finished as SUCCEEDED (with its forecast, in one transaction) or FAILED (with its cost and
error), so failed spend is never lost. Resume state is derived from durable rows."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .artifacts import artifact_kind
from .cash import LedgerError, money_text
from .db import append_journal, transaction
from .util import canonical_json, isoformat, parse_datetime, sha256_json

P_MIN = Decimal("0.01")
P_MAX = Decimal("0.99")
CONFIDENCE = ("low", "medium", "high")
FORECAST_KINDS = ("entry", "update")
NO_TIMELY_BASELINE = "NO_TIMELY_BASELINE"
# Body fields the ledger requires (by type). Plan 4 validates their content.
BODY_FIELDS: dict[str, type] = {
    "evidence": list,
    "rules_interpretation": str,
    "exposure_flags": list,
}
FORECAST_HASH_COLUMNS = (
    "attempt_id",
    "cohort_id",
    "condition_id",
    "rules_hash",
    "kind",
    "created_at",
    "abstained",
    "abstain_reason",
    "p_low",
    "p_mid",
    "p_high",
    "confidence",
    "base_rate",
    "body_json",
    "research_input_hash",
    "transcript_hash",
    "cost_usd",
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ForecastError(LedgerError):
    pass


class ResumeStep(StrEnum):
    NEEDS_BASELINE = "NEEDS_BASELINE"
    BASELINE_EXPIRED = "BASELINE_EXPIRED"
    NEEDS_DECISION = "NEEDS_DECISION"
    DONE = "DONE"


@dataclass(frozen=True)
class ForecastRecord:
    attempt_id: int
    cohort_id: str
    condition_id: str
    rules_hash: str
    kind: str
    abstained: bool
    abstain_reason: str | None
    p_low: Decimal | None
    p_mid: Decimal | None
    p_high: Decimal | None
    confidence: str | None
    base_rate: Decimal | None
    body: Mapping[str, Any]
    research_input_hash: str
    transcript_hash: str
    cost_usd: Decimal


def forecast_hash(values: Mapping[str, Any]) -> str:
    return sha256_json({column: values[column] for column in FORECAST_HASH_COLUMNS})


def _optional_text(value: Decimal | None) -> str | None:
    return None if value is None else money_text(value)


def _valid_cost(cost_usd: Decimal) -> None:
    if not cost_usd.is_finite() or cost_usd < 0:
        raise ForecastError(f"cost {cost_usd} must be a non-negative amount")


def validate_forecast(record: ForecastRecord) -> None:
    if record.kind not in FORECAST_KINDS:
        raise ForecastError(f"unknown forecast kind {record.kind!r}")
    for name in ("rules_hash", "research_input_hash", "transcript_hash"):
        if not _HEX64.match(getattr(record, name)):
            raise ForecastError(f"{name} is not a SHA-256 hex digest")
    for field, expected in BODY_FIELDS.items():
        if not isinstance(record.body.get(field), expected):
            raise ForecastError(f"forecast body needs {field} ({expected.__name__})")
    probabilities = (record.p_low, record.p_mid, record.p_high)
    if record.abstained:
        if not record.abstain_reason:
            raise ForecastError("an abstention needs a reason")
        if any(p is not None for p in probabilities) or record.confidence is not None:
            raise ForecastError("an abstention carries no probabilities or confidence")
    else:
        if record.abstain_reason is not None:
            raise ForecastError("abstain_reason is only for abstentions")
        if any(p is None for p in probabilities):
            raise ForecastError("p_low, p_mid and p_high are all required")
        p_low, p_mid, p_high = (p for p in probabilities if p is not None)
        for p in (p_low, p_mid, p_high):
            if not p.is_finite() or not P_MIN <= p <= P_MAX:
                raise ForecastError(f"probability {p} outside [{P_MIN}, {P_MAX}]")
        if not p_low <= p_mid <= p_high:
            raise ForecastError("probabilities must satisfy p_low <= p_mid <= p_high")
        if record.confidence not in CONFIDENCE:
            raise ForecastError(f"confidence must be one of {CONFIDENCE}")
        if record.base_rate is None:
            # The base rate is one of the three scored forecasters (spec §8).
            raise ForecastError("a non-abstained forecast needs a base rate")
    if record.base_rate is not None and (
        not record.base_rate.is_finite() or not Decimal(0) <= record.base_rate <= Decimal(1)
    ):
        raise ForecastError(f"base rate {record.base_rate} outside [0, 1]")
    _valid_cost(record.cost_usd)


def _active_cohort_row(conn: sqlite3.Connection, cohort_id: str) -> sqlite3.Row:
    cohort = conn.execute(
        "SELECT status, baseline_window_seconds FROM cohorts WHERE cohort_id = ?", (cohort_id,)
    ).fetchone()
    if cohort is None or cohort["status"] != "ACTIVE":
        raise ForecastError(f"cohort {cohort_id[:12]} is not active")
    found: sqlite3.Row = cohort
    return found


def start_attempt(
    conn: sqlite3.Connection, cohort_id: str, condition_id: str, kind: str, now: datetime
) -> int:
    """Open a durable research attempt before any paid call is made."""
    if kind not in FORECAST_KINDS:
        raise ForecastError(f"unknown forecast kind {kind!r}")
    with transaction(conn):
        _active_cohort_row(conn, cohort_id)
        cursor = conn.execute(
            "INSERT INTO research_attempts (cohort_id, condition_id, kind, started_at, status) "
            "VALUES (?, ?, ?, ?, 'STARTED')",
            (cohort_id, condition_id, kind, isoformat(now)),
        )
        if cursor.lastrowid is None:
            raise ForecastError("attempt insert returned no row id")
        attempt_id = cursor.lastrowid
        append_journal(
            conn,
            "ATTEMPT_STARTED",
            {"attempt_id": attempt_id, "cohort_id": cohort_id, "condition_id": condition_id},
            now,
        )
    return attempt_id


def fail_attempt(
    conn: sqlite3.Connection, attempt_id: int, cost_usd: Decimal, error: str, now: datetime
) -> None:
    """Close an attempt that produced no forecast, keeping what it cost."""
    with transaction(conn):
        fail_attempt_locked(conn, attempt_id, cost_usd, error, now)


def fail_attempt_locked(
    conn: sqlite3.Connection, attempt_id: int, cost_usd: Decimal, error: str, now: datetime
) -> None:
    """`fail_attempt` inside a write transaction the caller already holds, so the closing
    commits together with whatever else the caller writes (e.g. its refusal)."""
    _valid_cost(cost_usd)
    if not error:
        raise ForecastError("a failed attempt needs an error")
    row = conn.execute(
        "SELECT status FROM research_attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if row is None or row["status"] != "STARTED":
        raise ForecastError(f"attempt {attempt_id} is not STARTED")
    conn.execute(
        "UPDATE research_attempts SET status = 'FAILED', finished_at = ?, cost_usd = ?, "
        "error = ? WHERE attempt_id = ?",
        (isoformat(now), money_text(cost_usd), error, attempt_id),
    )
    append_journal(
        conn,
        "ATTEMPT_FAILED",
        {"attempt_id": attempt_id, "cost_usd": money_text(cost_usd), "error": error},
        now,
    )


def unfinished_attempts(conn: sqlite3.Connection, cohort_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT attempt_id FROM research_attempts WHERE cohort_id = ? AND status = 'STARTED' "
        "ORDER BY attempt_id",
        (cohort_id,),
    ).fetchall()
    return [row["attempt_id"] for row in rows]


def record_forecast(conn: sqlite3.Connection, record: ForecastRecord, now: datetime) -> int:
    """Record a forecast and mark its attempt SUCCEEDED in one transaction."""
    validate_forecast(record)
    created_at = isoformat(now)
    values: dict[str, Any] = {
        "attempt_id": record.attempt_id,
        "cohort_id": record.cohort_id,
        "condition_id": record.condition_id,
        "rules_hash": record.rules_hash,
        "kind": record.kind,
        "created_at": created_at,
        "abstained": int(record.abstained),
        "abstain_reason": record.abstain_reason,
        "p_low": _optional_text(record.p_low),
        "p_mid": _optional_text(record.p_mid),
        "p_high": _optional_text(record.p_high),
        "confidence": record.confidence,
        "base_rate": _optional_text(record.base_rate),
        "body_json": canonical_json(dict(record.body)),
        "research_input_hash": record.research_input_hash,
        "transcript_hash": record.transcript_hash,
        "cost_usd": money_text(record.cost_usd),
    }
    values["forecast_hash"] = forecast_hash(values)
    with transaction(conn):
        _active_cohort_row(conn, record.cohort_id)
        attempt = conn.execute(
            "SELECT * FROM research_attempts WHERE attempt_id = ?", (record.attempt_id,)
        ).fetchone()
        if (
            attempt is None
            or attempt["status"] != "STARTED"
            or attempt["cohort_id"] != record.cohort_id
            or attempt["condition_id"] != record.condition_id
            or attempt["kind"] != record.kind
        ):
            raise ForecastError(
                f"attempt {record.attempt_id} is not a STARTED attempt for this forecast"
            )
        if parse_datetime(attempt["started_at"]) > now:
            raise ForecastError("forecast is older than its attempt")
        rules = conn.execute(
            "SELECT 1 FROM rules_versions WHERE condition_id = ? AND rules_hash = ?",
            (record.condition_id, record.rules_hash),
        ).fetchone()
        if rules is None:
            raise ForecastError("unknown rules version for this market")
        for digest, kind in (
            (record.research_input_hash, "research_input"),
            (record.transcript_hash, "tool_transcript"),
        ):
            if artifact_kind(conn, digest) != kind:
                raise ForecastError(f"{kind} artifact {digest[:12]} is not stored")
        if record.kind == "entry" and conn.execute(
            "SELECT 1 FROM forecasts WHERE cohort_id = ? AND condition_id = ? AND kind = 'entry'",
            (record.cohort_id, record.condition_id),
        ).fetchone():
            raise ForecastError("entry forecast already exists for this market and cohort")
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        cursor = conn.execute(
            f"INSERT INTO forecasts ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        if cursor.lastrowid is None:
            raise ForecastError("forecast insert returned no row id")
        forecast_id = cursor.lastrowid
        conn.execute(
            "UPDATE research_attempts SET status = 'SUCCEEDED', finished_at = ?, cost_usd = ? "
            "WHERE attempt_id = ?",
            (created_at, values["cost_usd"], record.attempt_id),
        )
        append_journal(
            conn,
            "FORECAST_RECORDED",
            {
                "forecast_id": forecast_id,
                "attempt_id": record.attempt_id,
                "cohort_id": record.cohort_id,
                "condition_id": record.condition_id,
                "kind": record.kind,
                "forecast_hash": values["forecast_hash"],
            },
            now,
        )
    return forecast_id


def _forecast_row(conn: sqlite3.Connection, forecast_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT f.*, c.baseline_window_seconds FROM forecasts f "
        "JOIN cohorts c ON c.cohort_id = f.cohort_id WHERE f.forecast_id = ?",
        (forecast_id,),
    ).fetchone()
    if row is None:
        raise ForecastError(f"unknown forecast {forecast_id}")
    found: sqlite3.Row = row
    return found


def baseline_deadline(forecast: sqlite3.Row) -> datetime:
    """The latest fetch time a baseline snapshot may have (cohort's frozen window)."""
    window = timedelta(seconds=forecast["baseline_window_seconds"])
    return parse_datetime(forecast["created_at"]) + window


def attach_baseline(
    conn: sqlite3.Connection,
    forecast_id: int,
    yes_snapshot_id: int,
    no_snapshot_id: int,
    now: datetime,
) -> None:
    """Both snapshots must be fetched inside [forecast time, forecast time + window].
    Attaching after the deadline is allowed when the books themselves were timely."""
    with transaction(conn):
        forecast = _forecast_row(conn, forecast_id)
        if conn.execute(
            "SELECT 1 FROM forecast_baselines WHERE forecast_id = ?", (forecast_id,)
        ).fetchone():
            raise ForecastError(f"forecast {forecast_id} already has a baseline")
        forecast_time = parse_datetime(forecast["created_at"])
        deadline = baseline_deadline(forecast)
        for snapshot_id, outcome in ((yes_snapshot_id, "YES"), (no_snapshot_id, "NO")):
            snap = conn.execute(
                "SELECT condition_id, outcome, fetched_at FROM book_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
            if snap is None:
                raise ForecastError(f"unknown snapshot {snapshot_id}")
            if snap["condition_id"] != forecast["condition_id"]:
                raise ForecastError(f"snapshot {snapshot_id} is for another market")
            if snap["outcome"] != outcome:
                raise ForecastError(f"snapshot {snapshot_id} outcome is not {outcome}")
            fetched_at = parse_datetime(snap["fetched_at"])
            if fetched_at < forecast_time:
                raise ForecastError(
                    f"snapshot {snapshot_id} was fetched before the forecast was committed"
                )
            if fetched_at > deadline:
                raise ForecastError(
                    f"snapshot {snapshot_id} was fetched after the baseline window closed"
                )
        conn.execute(
            "INSERT INTO forecast_baselines (forecast_id, yes_snapshot_id, no_snapshot_id, "
            "reason, attached_at) VALUES (?, ?, ?, NULL, ?)",
            (forecast_id, yes_snapshot_id, no_snapshot_id, isoformat(now)),
        )
        append_journal(
            conn,
            "BASELINE_ATTACHED",
            {"forecast_id": forecast_id, "yes": yes_snapshot_id, "no": no_snapshot_id},
            now,
        )


def mark_no_timely_baseline(conn: sqlite3.Connection, forecast_id: int, now: datetime) -> None:
    """Record that no timely baseline exists. Entry forecasts get a NO_TIMELY_BASELINE
    decision in every portfolio of the cohort."""
    with transaction(conn):
        forecast = _forecast_row(conn, forecast_id)
        if conn.execute(
            "SELECT 1 FROM forecast_baselines WHERE forecast_id = ?", (forecast_id,)
        ).fetchone():
            raise ForecastError(f"forecast {forecast_id} already has a baseline")
        if now <= baseline_deadline(forecast):
            raise ForecastError(f"forecast {forecast_id}'s baseline window is still open")
        now_text = isoformat(now)
        conn.execute(
            "INSERT INTO forecast_baselines (forecast_id, yes_snapshot_id, no_snapshot_id, "
            "reason, attached_at) VALUES (?, NULL, NULL, ?, ?)",
            (forecast_id, NO_TIMELY_BASELINE, now_text),
        )
        if forecast["kind"] == "entry":
            # Only portfolios without a decision: a stray earlier decision must not
            # make the whole close-out roll back on the decisions primary key.
            for portfolio_id in undecided_portfolios(conn, forecast_id):
                conn.execute(
                    "INSERT INTO decisions (portfolio_id, forecast_id, condition_id, kind, "
                    "reason, ticket_id, decided_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
                    (
                        portfolio_id,
                        forecast_id,
                        forecast["condition_id"],
                        NO_TIMELY_BASELINE,
                        "baseline snapshot not taken within the window",
                        now_text,
                    ),
                )
        append_journal(conn, NO_TIMELY_BASELINE, {"forecast_id": forecast_id}, now)


def undecided_portfolios(conn: sqlite3.Connection, forecast_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT p.portfolio_id FROM forecasts f "
        "JOIN portfolios p ON p.cohort_id = f.cohort_id "
        "LEFT JOIN decisions d ON d.portfolio_id = p.portfolio_id "
        "AND d.forecast_id = f.forecast_id "
        "WHERE f.forecast_id = ? AND d.forecast_id IS NULL ORDER BY p.variant",
        (forecast_id,),
    ).fetchall()
    return [row["portfolio_id"] for row in rows]


def resume_step(conn: sqlite3.Connection, forecast_id: int, now: datetime) -> ResumeStep:
    """BASELINE_EXPIRED means: attach books fetched inside the window if they exist,
    otherwise mark the forecast NO_TIMELY_BASELINE (expired forecasts are marked, never
    given a late baseline). Books fetched now would be refused."""
    forecast = _forecast_row(conn, forecast_id)
    baseline = conn.execute(
        "SELECT reason FROM forecast_baselines WHERE forecast_id = ?", (forecast_id,)
    ).fetchone()
    if baseline is not None:
        if baseline["reason"] is not None or forecast["kind"] == "update":
            return ResumeStep.DONE
        if undecided_portfolios(conn, forecast_id):
            return ResumeStep.NEEDS_DECISION
        return ResumeStep.DONE
    if now > baseline_deadline(forecast):
        return ResumeStep.BASELINE_EXPIRED
    return ResumeStep.NEEDS_BASELINE


def unfinished_forecasts(conn: sqlite3.Connection, cohort_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT forecast_id FROM forecasts WHERE cohort_id = ? ORDER BY forecast_id",
        (cohort_id,),
    ).fetchall()
    # Any time works for the DONE test: the clock only splits NEEDS_BASELINE/EXPIRED.
    probe = datetime.fromtimestamp(0, UTC)
    return [
        row["forecast_id"]
        for row in rows
        if resume_step(conn, row["forecast_id"], probe) is not ResumeStep.DONE
    ]

"""Seed helpers for ledger tests. Rows mirror what Plan 1 collection and later plans write."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from predict_agent.artifacts import store_artifact
from predict_agent.cohorts import PRIMARY, CohortIdentity, cohort_portfolios, ensure_cohort
from predict_agent.forecasts import (
    ForecastRecord,
    attach_baseline,
    record_forecast,
    start_attempt,
)
from predict_agent.policy_params import parse_policy, variant_policies
from predict_agent.tickets import Fill, TicketDraft
from predict_agent.util import canonical_json, isoformat, sha256_json
from tests.predict.fixtures import CONDITION_ID, NO_TOKEN, NOW, YES_TOKEN

_POLICIES = variant_policies(
    parse_policy(
        json.loads(
            (Path(__file__).resolve().parents[2] / "config" / "predict-policy.example.json")
            .read_text(encoding="utf-8")
        )["policy"],
        "bounds",
    )
)
PRIMARY_POLICY = _POLICIES["primary"]
SHADOW_POLICY = _POLICIES["shadow_mid"]
# Recorded asks deep enough for every draft the ledger tests open.
BOOK_RECORD_ASKS = [["0.40", "1000"], ["0.41", "1000"]]
WINDOW_SECONDS = 1800


def seed_market(conn: sqlite3.Connection, condition_id: str = CONDITION_ID) -> str:
    payload = {
        "question": f"Will {condition_id[:8]} happen?",
        "rules_text": "Resolves Yes if it happens by the end date.",
        "resolution_source": "",
        "end_date": "2026-11-01T03:59:00Z",
    }
    rules_hash = sha256_json(payload)
    conn.execute(
        "INSERT INTO rules_versions (condition_id, rules_hash, rules_json, first_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (condition_id, rules_hash, canonical_json(payload), isoformat(NOW)),
    )
    conn.execute(
        "INSERT INTO markets (condition_id, event_id, question, category, yes_token_id, "
        "no_token_id, current_rules_hash, fees_enabled, fee_schedule_json, first_seen_at, "
        "last_seen_at) VALUES (?, 'e1', ?, 'politics', ?, ?, ?, 0, NULL, ?, ?)",
        (
            condition_id,
            payload["question"],
            YES_TOKEN,
            NO_TOKEN,
            rules_hash,
            isoformat(NOW),
            isoformat(NOW),
        ),
    )
    return rules_hash


def seed_snapshot(
    conn: sqlite3.Connection,
    outcome: str,
    fetched_at: datetime,
    condition_id: str = CONDITION_ID,
    *,
    asks: list[tuple[str, str]] | None = None,
    min_order_size: str = "0",
    tick_size: str = "0.01",
    fee_schedule: dict[str, Any] | None = None,
) -> int:
    """A stored snapshot whose record carries real ask levels, best-first as Plan 1 stores
    them (default: deep enough for every draft the ledger tests open)."""
    token = YES_TOKEN if outcome == "YES" else NO_TOKEN
    levels = [list(level) for level in asks] if asks is not None else BOOK_RECORD_ASKS
    record = {
        "condition_id": condition_id,
        "token_id": token,
        "observed_at": isoformat(fetched_at),
        "fetched_at": isoformat(fetched_at),
        "book_hash": "h",
        "bids": [["0.01", "100"]],
        "asks": sorted(levels, key=lambda level: Decimal(level[0])),
        "tick_size": tick_size,
        "min_order_size": min_order_size,
    }
    cursor = conn.execute(
        "INSERT INTO book_snapshots (run_id, condition_id, token_id, source_run_id, outcome, "
        "observed_at, fetched_at, record_json, fees_enabled, fee_schedule_json, snapshot_hash) "
        "VALUES ('r', ?, ?, 'r', ?, ?, ?, ?, ?, ?, ?)",
        (
            condition_id,
            token,
            outcome,
            isoformat(fetched_at),
            isoformat(fetched_at),
            canonical_json(record),
            int(fee_schedule is not None),
            canonical_json(fee_schedule) if fee_schedule is not None else None,
            uuid.uuid4().hex,
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def seed_observation(
    conn: sqlite3.Connection,
    outcome: str | None,
    *,
    cross_check: str = "CONFIRMED",
    status: str = "resolved",
    condition_id: str = CONDITION_ID,
    fetched_at: datetime = NOW,
    requested_at: datetime | None = None,
    gamma_fetched_at: datetime | None = None,
) -> int:
    """An observation whose resolution request ran over [requested_at, fetched_at]
    (default: one second before fetched_at), cross-checked against Gamma fetched at
    gamma_fetched_at (default: fetched_at)."""
    requested = requested_at or fetched_at - timedelta(seconds=1)
    cursor = conn.execute(
        "INSERT INTO resolution_observations (run_id, condition_id, resolution_requested_at, "
        "resolution_fetched_at, gamma_fetched_at, gamma_json, status, outcome, cross_check, "
        "was_disputed, new_version_q, raw_json) VALUES ('r', ?, ?, ?, ?, '{}', ?, ?, ?, 0, 0, "
        "'{}')",
        (
            condition_id,
            isoformat(requested),
            isoformat(fetched_at),
            isoformat(gamma_fetched_at or fetched_at),
            status,
            outcome,
            cross_check,
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def seed_cohort(
    conn: sqlite3.Connection,
    *,
    model_id: str = "m1",
    bankroll: str = "1000",
    shadow: bool = False,
) -> str:
    portfolios = {PRIMARY: store_artifact(conn, "policy", PRIMARY_POLICY, NOW)}
    if shadow:
        portfolios["shadow_mid"] = store_artifact(conn, "policy", SHADOW_POLICY, NOW)
    identity = CohortIdentity(
        portfolios=portfolios,
        prompt_hash=store_artifact(conn, "prompt", "Forecast without market prices.", NOW),
        model_id=model_id,
        research_settings={"tools": ["WebSearch", "WebFetch"]},
        scoring_version="1",
        baseline_window_seconds=WINDOW_SECONDS,
    )
    return ensure_cohort(
        conn, identity, starting_bankroll=Decimal(bankroll), code_version="test", now=NOW
    )


def portfolio(conn: sqlite3.Connection, cohort_id: str, variant: str = PRIMARY) -> str:
    return cohort_portfolios(conn, cohort_id)[variant]


def forecast_record(
    conn: sqlite3.Connection,
    cohort_id: str,
    rules_hash: str,
    *,
    at: datetime = NOW,
    **overrides: Any,
) -> ForecastRecord:
    """A valid entry forecast record; opens its research attempt at `at` unless the
    overrides supply an attempt_id."""
    kind = overrides.get("kind", "entry")
    condition_id = overrides.get("condition_id", CONDITION_ID)
    if "attempt_id" not in overrides:
        overrides["attempt_id"] = start_attempt(conn, cohort_id, condition_id, kind, at)
    record = ForecastRecord(
        attempt_id=0,
        cohort_id=cohort_id,
        condition_id=CONDITION_ID,
        rules_hash=rules_hash,
        kind="entry",
        abstained=False,
        abstain_reason=None,
        p_low=Decimal("0.55"),
        p_mid=Decimal("0.60"),
        p_high=Decimal("0.65"),
        confidence="medium",
        base_rate=Decimal("0.30"),
        body={"evidence": [], "rules_interpretation": "plain reading", "exposure_flags": []},
        research_input_hash=store_artifact(conn, "research_input", "rendered prompt", NOW),
        transcript_hash=store_artifact(conn, "tool_transcript", "[]", NOW),
        cost_usd=Decimal("0.12"),
    )
    return replace(record, **overrides)


def seed_entry_forecast(
    conn: sqlite3.Connection,
    cohort_id: str,
    rules_hash: str,
    *,
    at: datetime = NOW,
    **overrides: Any,
) -> int:
    record = forecast_record(conn, cohort_id, rules_hash, at=at, **overrides)
    return record_forecast(conn, record, at)


def seed_baselined_forecast(
    conn: sqlite3.Connection,
    cohort_id: str,
    rules_hash: str,
    *,
    condition_id: str = CONDITION_ID,
    at: datetime = NOW,
) -> tuple[int, int, int]:
    """(forecast_id, yes_snapshot_id, no_snapshot_id), books fetched one second later."""
    forecast = seed_entry_forecast(conn, cohort_id, rules_hash, at=at, condition_id=condition_id)
    later = at + timedelta(seconds=1)
    yes = seed_snapshot(conn, "YES", later, condition_id)
    no = seed_snapshot(conn, "NO", later, condition_id)
    attach_baseline(conn, forecast, yes, no, later)
    return forecast, yes, no


def ticket_draft(
    conn: sqlite3.Connection,
    portfolio_id: str,
    forecast_id: int,
    snapshot_id: int,
    *,
    outcome: str = "YES",
    fills: tuple[Fill, ...] = (Fill(Decimal("0.40"), Decimal("10")),),
    fee: Decimal = Decimal("0"),
) -> TicketDraft:
    """A draft that satisfies every ledger check, using the portfolio's own policy."""
    forecast = conn.execute(
        "SELECT condition_id, rules_hash FROM forecasts WHERE forecast_id = ?", (forecast_id,)
    ).fetchone()
    policy = conn.execute(
        "SELECT policy_hash FROM portfolios WHERE portfolio_id = ?", (portfolio_id,)
    ).fetchone()
    return TicketDraft(
        portfolio_id=portfolio_id,
        forecast_id=forecast_id,
        snapshot_id=snapshot_id,
        condition_id=forecast["condition_id"],
        outcome=outcome,
        fills=fills,
        fee=fee,
        policy_hash=policy["policy_hash"],
        rules_hash=forecast["rules_hash"],
    )

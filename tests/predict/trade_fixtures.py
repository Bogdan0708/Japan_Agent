"""Seed helpers for paper-trading tests: cohorts with real policy artifacts, tradeable
markets, discovery runs and baselined forecasts. Builds on ledger_fixtures."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from predict_agent.artifacts import store_artifact
from predict_agent.cohorts import CohortIdentity, ensure_cohort
from predict_agent.forecasts import attach_baseline
from predict_agent.policy_params import parse_policy, variant_policies
from predict_agent.util import canonical_json, isoformat, sha256_json
from tests.predict.fixtures import CONDITION_ID, NO_TOKEN, NOW, YES_TOKEN
from tests.predict.ledger_fixtures import WINDOW_SECONDS, seed_entry_forecast, seed_snapshot

REPO = Path(__file__).resolve().parents[2]
POLICY_SECTION: dict[str, Any] = json.loads(
    (REPO / "config" / "predict-policy.example.json").read_text(encoding="utf-8")
)["policy"]
POLITICS_FEES = {"exponent": 1, "rate": 0.04, "takerOnly": True, "rebateRate": 0.25}


def seed_policy_cohort(conn: sqlite3.Connection, *, model_id: str = "m1") -> str:
    """A cohort whose primary and shadow_mid portfolios carry the example config's policy."""
    params = parse_policy(POLICY_SECTION, "bounds")
    portfolios = {
        variant: store_artifact(conn, "policy", content, NOW)
        for variant, content in variant_policies(params).items()
    }
    identity = CohortIdentity(
        portfolios=portfolios,
        prompt_hash=store_artifact(conn, "prompt", "Forecast without market prices.", NOW),
        model_id=model_id,
        research_settings={"tools": ["WebSearch", "WebFetch"]},
        scoring_version="1",
        baseline_window_seconds=WINDOW_SECONDS,
    )
    return ensure_cohort(
        conn,
        identity,
        starting_bankroll=params.starting_bankroll,
        code_version="test",
        now=NOW,
    )


def seed_tradeable_market(
    conn: sqlite3.Connection,
    condition_id: str = CONDITION_ID,
    *,
    event_id: str = "e1",
    category: str = "politics",
    end_date: datetime = NOW + timedelta(days=20),
) -> str:
    """A market with its rules version and markets row; returns the rules hash."""
    payload = {
        "question": f"Will {condition_id[:8]} happen?",
        "rules_text": "Resolves Yes if it happens by the end date.",
        "resolution_source": "",
        "end_date": isoformat(end_date),
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
        "last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)",
        (
            condition_id,
            event_id,
            payload["question"],
            category,
            YES_TOKEN,
            NO_TOKEN,
            rules_hash,
            isoformat(NOW),
            isoformat(NOW),
        ),
    )
    return rules_hash


def seed_discovery(
    conn: sqlite3.Connection, condition_ids: list[str], *, at: datetime = NOW
) -> str:
    """A COMPLETED discovery run that found these markets eligible."""
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO runs (run_id, command, started_at, policy_hash, status, finished_at) "
        "VALUES (?, 'discover', ?, 'p', 'COMPLETED', ?)",
        (run_id, isoformat(at), isoformat(at)),
    )
    for condition_id in condition_ids:
        market = conn.execute(
            "SELECT * FROM markets WHERE condition_id = ?", (condition_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO discoveries (run_id, condition_id, event_id, question, category, "
            "yes_token_id, no_token_id, rules_hash, fees_enabled, fee_schedule_json, "
            "observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)",
            (
                run_id,
                condition_id,
                market["event_id"],
                market["question"],
                market["category"],
                market["yes_token_id"],
                market["no_token_id"],
                market["current_rules_hash"],
                isoformat(at),
            ),
        )
    return run_id


def seed_ready_forecast(
    conn: sqlite3.Connection,
    cohort_id: str,
    rules_hash: str,
    *,
    condition_id: str = CONDITION_ID,
    at: datetime = NOW,
    yes_asks: list[tuple[str, str]] | None = None,
    no_asks: list[tuple[str, str]] | None = None,
    p: tuple[str, str, str] = ("0.80", "0.85", "0.90"),
    fee_schedule: dict[str, Any] | None = None,
) -> int:
    """An entry forecast with both baseline books attached one second after it."""
    forecast_id = seed_entry_forecast(
        conn,
        cohort_id,
        rules_hash,
        at=at,
        condition_id=condition_id,
        p_low=Decimal(p[0]),
        p_mid=Decimal(p[1]),
        p_high=Decimal(p[2]),
    )
    later = at + timedelta(seconds=1)
    yes = seed_snapshot(conn, "YES", later, condition_id, asks=yes_asks or [("0.50", "50")],
                        min_order_size="5", fee_schedule=fee_schedule)
    no = seed_snapshot(conn, "NO", later, condition_id, asks=no_asks or [("0.45", "50")],
                       min_order_size="5", fee_schedule=fee_schedule)
    attach_baseline(conn, forecast_id, yes, no, later)
    return forecast_id

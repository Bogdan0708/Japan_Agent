"""Cohorts and their portfolios (spec §4).

A cohort is one research identity: prompt, model, research settings, scoring version,
baseline window and generation, plus the pre-registered portfolio variants with their
policies. Forecasts and baselines belong to the cohort and are shared by its portfolios;
each portfolio (`primary`, and shadows such as `shadow_mid`) has its own frozen policy,
FUNDING, cash, tickets and decisions. Only one cohort is ACTIVE; opening a new one closes
the others, which keep settling but take no new forecasts or tickets."""

from __future__ import annotations

import re
import sqlite3
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .artifacts import artifact_kind
from .cash import LedgerError, append_cash_entry, money_text
from .db import append_journal, transaction
from .util import canonical_json, isoformat, sha256_json

PRIMARY = "primary"
_VARIANT = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True)
class CohortIdentity:
    portfolios: Mapping[str, str]  # variant -> policy artifact hash; must include "primary"
    prompt_hash: str
    model_id: str
    research_settings: Mapping[str, Any]
    scoring_version: str
    baseline_window_seconds: int
    generation: int = 1

    def record(self) -> dict[str, Any]:
        return {
            "portfolios": dict(self.portfolios),
            "prompt_hash": self.prompt_hash,
            "model_id": self.model_id,
            "research_settings": dict(self.research_settings),
            "scoring_version": self.scoring_version,
            "baseline_window_seconds": self.baseline_window_seconds,
            "generation": self.generation,
        }


def cohort_id_for(identity: CohortIdentity) -> str:
    return sha256_json(identity.record())


def portfolio_id_for(cohort_id: str, variant: str) -> str:
    return sha256_json({"cohort_id": cohort_id, "variant": variant})


def code_version(root: Path) -> str:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=True, timeout=10
        )
        return result.stdout.strip()

    try:
        sha = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if len(sha) != 40:
        return "unknown"
    return f"{sha}-dirty" if dirty else sha


def active_cohort(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT cohort_id FROM cohorts WHERE status = 'ACTIVE'").fetchone()
    return row["cohort_id"] if row else None


def cohort_portfolios(conn: sqlite3.Connection, cohort_id: str) -> dict[str, str]:
    """variant -> portfolio_id."""
    rows = conn.execute(
        "SELECT variant, portfolio_id FROM portfolios WHERE cohort_id = ? ORDER BY variant",
        (cohort_id,),
    ).fetchall()
    return {row["variant"]: row["portfolio_id"] for row in rows}


def validate_identity(identity: CohortIdentity) -> None:
    if PRIMARY not in identity.portfolios:
        raise LedgerError("a cohort needs a 'primary' portfolio")
    for variant in identity.portfolios:
        if not _VARIANT.match(variant):
            raise LedgerError(f"invalid portfolio variant name {variant!r}")
    if not identity.model_id or not identity.scoring_version:
        raise LedgerError("cohort identity needs a model id and a scoring version")
    if identity.baseline_window_seconds <= 0:
        raise LedgerError("baseline window must be positive")
    if identity.generation < 1:
        raise LedgerError("generation starts at 1")


def ensure_cohort(
    conn: sqlite3.Connection,
    identity: CohortIdentity,
    *,
    starting_bankroll: Decimal,
    code_version: str,
    now: datetime,
) -> str:
    if not starting_bankroll.is_finite() or starting_bankroll <= 0:
        raise LedgerError(f"starting bankroll must be positive, got {starting_bankroll}")
    validate_identity(identity)
    cohort_id = cohort_id_for(identity)
    with transaction(conn):
        expected = [(identity.prompt_hash, "prompt")]
        expected += [(digest, "policy") for digest in identity.portfolios.values()]
        for digest, kind in expected:
            if artifact_kind(conn, digest) != kind:
                raise LedgerError(f"{kind} artifact {digest[:12]} is not stored")
        existing = conn.execute(
            "SELECT status FROM cohorts WHERE cohort_id = ?", (cohort_id,)
        ).fetchone()
        if existing is not None:
            if existing["status"] == "ACTIVE":
                return cohort_id
            raise LedgerError(
                f"cohort {cohort_id[:12]} is closed; refusing to reopen it "
                "(increase the generation to restart the same settings as a new cohort)"
            )
        now_text = isoformat(now)
        active = conn.execute("SELECT cohort_id FROM cohorts WHERE status = 'ACTIVE'").fetchall()
        for row in active:
            conn.execute(
                "UPDATE cohorts SET status = 'CLOSED', closed_at = ? WHERE cohort_id = ?",
                (now_text, row["cohort_id"]),
            )
            append_journal(
                conn,
                "COHORT_CLOSED",
                {"cohort_id": row["cohort_id"], "superseded_by": cohort_id},
                now,
            )
        bankroll_text = money_text(starting_bankroll)
        conn.execute(
            "INSERT INTO cohorts (cohort_id, identity_json, code_version, starting_bankroll, "
            "baseline_window_seconds, status, started_at) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?)",
            (
                cohort_id,
                canonical_json(identity.record()),
                code_version,
                bankroll_text,
                identity.baseline_window_seconds,
                now_text,
            ),
        )
        for variant, policy_hash in sorted(identity.portfolios.items()):
            portfolio_id = portfolio_id_for(cohort_id, variant)
            conn.execute(
                "INSERT INTO portfolios (portfolio_id, cohort_id, variant, policy_hash, "
                "starting_bankroll) VALUES (?, ?, ?, ?, ?)",
                (portfolio_id, cohort_id, variant, policy_hash, bankroll_text),
            )
            append_cash_entry(conn, portfolio_id, "FUNDING", starting_bankroll, None, now)
        append_journal(
            conn,
            "COHORT_OPENED",
            {
                "cohort_id": cohort_id,
                "identity": identity.record(),
                "code_version": code_version,
                "starting_bankroll": bankroll_text,
            },
            now,
        )
    return cohort_id

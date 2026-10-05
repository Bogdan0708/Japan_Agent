"""Content-addressed, immutable artifacts: prompts, policies, rendered research inputs and
tool transcripts. Forecasts, tickets and cohorts reference them by SHA-256."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .util import isoformat, sha256_text

ARTIFACT_KINDS = frozenset({"prompt", "policy", "research_input", "tool_transcript"})


def store_artifact(conn: sqlite3.Connection, kind: str, content: str, now: datetime) -> str:
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"unknown artifact kind {kind!r}")
    digest = sha256_text(content)
    row = conn.execute("SELECT kind FROM artifacts WHERE artifact_hash = ?", (digest,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO artifacts (artifact_hash, kind, content, created_at) VALUES (?, ?, ?, ?)",
            (digest, kind, content, isoformat(now)),
        )
    elif row["kind"] != kind:
        raise ValueError(f"artifact {digest[:12]} already stored as {row['kind']}")
    return digest


def load_artifact(conn: sqlite3.Connection, digest: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT kind, content FROM artifacts WHERE artifact_hash = ?", (digest,)
    ).fetchone()
    if row is None:
        raise KeyError(digest)
    if sha256_text(row["content"]) != digest:
        raise RuntimeError(f"artifact {digest[:12]} no longer matches its hash")
    return row["kind"], row["content"]


def artifact_kind(conn: sqlite3.Connection, digest: str) -> str | None:
    row = conn.execute("SELECT kind FROM artifacts WHERE artifact_hash = ?", (digest,)).fetchone()
    return None if row is None else str(row["kind"])

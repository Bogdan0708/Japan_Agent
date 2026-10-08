from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .ledger_schema import LEDGER_SCHEMA
from .util import canonical_json, isoformat, sha256_json

SCHEMA_VERSION = 4
# Statements that bring an older version's existing tables up to date. CREATE ... IF NOT
# EXISTS in SCHEMA and LEDGER_SCHEMA adds the new tables and indexes (v4: per-market
# lookup indexes only, so v3 needs no statements of its own).
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: ("ALTER TABLE resolution_observations ADD COLUMN resolution_requested_at TEXT",),
    3: (),
}
GENESIS_HASH = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    started_at TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    geoblock_json TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    finished_at TEXT,
    source_run_id TEXT
);
CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    question TEXT NOT NULL,
    category TEXT NOT NULL,
    yes_token_id TEXT NOT NULL,
    no_token_id TEXT NOT NULL,
    current_rules_hash TEXT NOT NULL,
    fees_enabled INTEGER NOT NULL,
    fee_schedule_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules_versions (
    condition_id TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (condition_id, rules_hash)
);
CREATE TABLE IF NOT EXISTS discoveries (
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    question TEXT NOT NULL,
    category TEXT NOT NULL,
    yes_token_id TEXT NOT NULL,
    no_token_id TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    fees_enabled INTEGER NOT NULL,
    fee_schedule_json TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, condition_id)
);
CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('YES', 'NO')),
    observed_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    record_json TEXT NOT NULL,
    fees_enabled INTEGER NOT NULL,
    fee_schedule_json TEXT,
    snapshot_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS resolution_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    resolution_fetched_at TEXT NOT NULL,
    gamma_fetched_at TEXT,
    gamma_json TEXT,
    status TEXT NOT NULL,
    outcome TEXT,
    cross_check TEXT NOT NULL
        CHECK (cross_check IN ('CONFIRMED', 'UNCHECKED', 'MISMATCH', 'NOT_APPLICABLE')),
    was_disputed INTEGER NOT NULL,
    new_version_q INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    resolution_requested_at TEXT
);
CREATE TABLE IF NOT EXISTS refusals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    condition_id TEXT,
    stage TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    detail TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS rules_no_update BEFORE UPDATE ON rules_versions
BEGIN SELECT RAISE(ABORT, 'rules versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS rules_no_delete BEFORE DELETE ON rules_versions
BEGIN SELECT RAISE(ABORT, 'rules versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS discoveries_no_update BEFORE UPDATE ON discoveries
BEGIN SELECT RAISE(ABORT, 'discoveries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS discoveries_no_delete BEFORE DELETE ON discoveries
BEGIN SELECT RAISE(ABORT, 'discoveries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS observations_no_update BEFORE UPDATE ON resolution_observations
BEGIN SELECT RAISE(ABORT, 'resolution observations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON book_snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON book_snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE INDEX IF NOT EXISTS discoveries_by_market ON discoveries (condition_id);
CREATE INDEX IF NOT EXISTS snapshots_by_market ON book_snapshots (condition_id);
CREATE INDEX IF NOT EXISTS observations_by_market ON resolution_observations (condition_id);
"""


def _statements(script: str) -> list[str]:
    """Split a DDL script into statements; trigger bodies contain inner semicolons."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise RuntimeError("schema script ends with an incomplete statement")
    return statements


def _stored_version(conn: sqlite3.Connection) -> int | None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if table is None:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone():
            raise RuntimeError("predict database has tables but no schema version")
        return None
    versions = [row["version"] for row in conn.execute("SELECT version FROM schema_version")]
    if not versions:
        raise RuntimeError("predict database has an empty schema_version table")
    if len(versions) != 1:
        raise RuntimeError(f"unsupported predict schema version {versions}")
    version: int = versions[0]
    return version


def connect(path: Path) -> sqlite3.Connection:
    """Open the database. Creation and migration run in one transaction after the stored
    version is checked, so a refused or failed migration leaves the file unchanged."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Read the version without a write lock; only creation/migration needs one.
        if _stored_version(conn) != SCHEMA_VERSION:
            with transaction(conn):
                version = _stored_version(conn)  # another process may have migrated
                if version != SCHEMA_VERSION:
                    if version is not None and version not in MIGRATIONS:
                        raise RuntimeError(f"unsupported predict schema version {version}")
                    for statement in _statements(SCHEMA + LEDGER_SCHEMA):
                        conn.execute(statement)
                    if version is None:
                        conn.execute(
                            "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                        )
                    else:
                        for statement in MIGRATIONS[version]:
                            conn.execute(statement)
                        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _entry_hash(at: str, kind: str, payload_json: str, prev_hash: str) -> str:
    return sha256_json(
        {"at": at, "kind": kind, "payload": json.loads(payload_json), "prev_hash": prev_hash}
    )


def append_journal(
    conn: sqlite3.Connection, kind: str, payload: dict[str, Any], at: datetime
) -> str:
    row = conn.execute("SELECT entry_hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = row["entry_hash"] if row else GENESIS_HASH
    at_text = isoformat(at)
    payload_json = canonical_json(payload)
    entry_hash = _entry_hash(at_text, kind, payload_json, prev_hash)
    conn.execute(
        "INSERT INTO journal (at, kind, payload_json, prev_hash, entry_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        (at_text, kind, payload_json, prev_hash, entry_hash),
    )
    return entry_hash


def verify_journal(conn: sqlite3.Connection) -> bool:
    """True when the chain is intact. An unreadable entry (e.g. payload that is not JSON)
    is a broken chain, never an exception."""
    prev_hash = GENESIS_HASH
    for row in conn.execute("SELECT * FROM journal ORDER BY seq"):
        if row["prev_hash"] != prev_hash:
            return False
        try:
            expected = _entry_hash(row["at"], row["kind"], row["payload_json"], row["prev_hash"])
        except (ValueError, TypeError):
            return False
        if expected != row["entry_hash"]:
            return False
        prev_hash = row["entry_hash"]
    return True


def record_refusal(
    conn: sqlite3.Connection,
    run_id: str,
    condition_id: str | None,
    stage: str,
    reason_code: str,
    detail: str,
    at: datetime,
) -> None:
    conn.execute(
        "INSERT INTO refusals (run_id, condition_id, stage, reason_code, detail, at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, condition_id, stage, reason_code, detail, isoformat(at)),
    )

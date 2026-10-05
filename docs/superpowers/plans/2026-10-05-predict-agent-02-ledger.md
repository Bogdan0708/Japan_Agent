# predict-agent Plan 2 — Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the paper-trading ledger of `predict_agent`: immutable artifacts, cohorts with independent funding, forecast records with post-forecast baselines, atomic ticket/cash/decision writes, settlement from confirmed resolutions, crash-resume state, and ledger invariant checks — with no network, no Claude calls and no trading policy.

**Architecture:** New tables live in `ledger_schema.py` and are created by `db.connect` (schema v3, migrating v2 by adding tables only). Each concern gets one module — `artifacts.py`, `cash.py`, `cohorts.py`, `forecasts.py` (records, baselines, resume), `tickets.py` (tickets, decisions, equity), `settlement.py`, `invariants.py` — and every state change commits together with its journal entry in one SQLite transaction. Plan 3 (policy + paper fills) and Plan 4 (research) call these functions; they never write the tables directly.

**Tech Stack:** Python ≥ 3.11 stdlib (`sqlite3`, `decimal`, `subprocess` for `git`), `unittest`, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-10-05-polymarket-paper-forecaster-design.md` (§4 data model and accounting, §5 equity definition, §7 resume, §10 build order item 2).

**Builds on:** Plan 1 (`docs/superpowers/plans/2026-10-05-predict-agent-01-data-milestone.md`), merged state of branch `design/polymarket-paper-forecaster` at `eeb5fd9`.

## Global Constraints

- Core code is stdlib-only; `predict_agent` never imports `japan_agent`; no wallet, key, signing or non-GET HTTP code.
- Money is `Decimal`, stored as exact text via `format(value, "f")`; **never `SUM()` money in SQL** (SQLite converts TEXT to float) — sum in Python.
- Timestamps are timezone-aware UTC; compare stored timestamps by parsing them (`util.parse_datetime`), never as strings (`"…00Z"` vs `"…00.123Z"` sorts wrong).
- Every state change and its `journal` entry commit in the same `db.transaction`.
- Available cash can never go negative (spec §4), enforced inside the transaction that debits.
- No voiding: a ticket only moves `OPEN → SETTLED`; P&L is never removed (spec §4).
- One entry per market per cohort, ever (spec §4); one entry forecast per market per cohort.
- A new cohort is opened by any change to policy content, prompt content, model id, research settings or scoring version; each cohort is an independent portfolio with its own `FUNDING`; closed cohorts keep settling but take no new entries (spec §4).
- Settle only from a resolution observation with `status = 'resolved'`, `cross_check = 'CONFIRMED'` and outcome in `YES/NO/HALF`; anything else waits or is surfaced, never guessed.
- Canonical test run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -v`; ruff line length 100; mypy strict on `src/predict_agent`.

## Spec deviations ruled in this plan

1. **`stage_runs` is replaced by state derived from durable rows** (`forecast_baselines`, `decisions`, `paper_tickets`). A separate bookkeeping table can disagree with the rows it describes after a crash; deriving resume state from the rows themselves cannot. Cost if wrong: a later plan adds a view.
2. **`CANCELLED` is not implemented.** Polymarket documents no cancellation status (Plan 1 contract facts); an undocumented outcome stays unsettled and surfaced. Cost if wrong: one mapping entry once a real example exists.
3. **Settlement uses only `CONFIRMED` observations.** 120/120 recent live resolutions were `CONFIRMED` (Plan 1 evidence); `UNCHECKED` waits for Gamma to catch up. Cost if wrong: settlement lags by a poll cycle.
4. **Reusing a closed cohort's exact identity is refused** rather than reopening it, so two time periods never share one portfolio. Cost if wrong: the human edits a label (e.g. scoring version) to start fresh.

## Review Focus

1. **Money summed by SQLite** — three `0.1` debits must leave exactly `0.7` of `1.0`, not `0.7000000000000001` (Task 2 test `test_cash_sums_are_exact_decimal`).
2. **Crash in the middle of opening a ticket** — a failure after the ticket insert must leave no ticket, no debit, no decision and no journal entry (Task 5 test `test_open_ticket_failure_leaves_no_partial_state`).
3. **Running `settle` twice** — the second run must credit nothing and settle nothing (Task 6 test `test_settle_is_idempotent`).
4. **A baseline snapshot fetched before the forecast** — must be refused, enforcing forecast-before-price ordering even with mixed timestamp precision (Task 4 test `test_baseline_snapshot_before_forecast_is_refused`).
5. **A closed cohort with open tickets** — must still settle them and still refuse new entries (Task 6 test `test_closed_cohort_still_settles_but_cannot_open`).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/predict_agent/ledger_schema.py` | DDL for all ledger tables and their immutability triggers |
| `src/predict_agent/db.py` (modify) | apply ledger DDL, foreign keys on, schema v3 with v2→v3 migration |
| `src/predict_agent/artifacts.py` | content-addressed immutable artifacts |
| `src/predict_agent/cash.py` | money helpers, hash-chained cash entries, available cash |
| `src/predict_agent/tickets.py` | ticket opening, decisions, open cost, equity |
| `src/predict_agent/invariants.py` | ledger invariant checks for `doctor` |
| `src/predict_agent/cohorts.py` | cohort identity, opening/closing, `code_version` |
| `src/predict_agent/forecasts.py` | forecast validation/recording, baselines, resume state |
| `src/predict_agent/settlement.py` | settle open tickets from confirmed resolutions |
| `src/predict_agent/cli.py` (modify) | `settle` command; `doctor` checks ledger invariants |
| `tests/predict/ledger_fixtures.py` | seed helpers for markets, snapshots, observations, cohorts, forecasts |
| `tests/predict/test_ledger_*.py` | one test module per source module |

---

### Task 1: Ledger schema and v2 → v3 migration

**Files:**
- Create: `src/predict_agent/ledger_schema.py`, `tests/predict/test_ledger_schema.py`
- Modify: `src/predict_agent/db.py`

**Interfaces:**
- Consumes: `db.connect`, `db.SCHEMA` (Plan 1).
- Produces: `ledger_schema.LEDGER_SCHEMA: str`; `db.SCHEMA_VERSION = 3`; `db.MIGRATABLE_FROM = frozenset({2})`; `db.connect` creates ledger tables, sets `PRAGMA foreign_keys = ON`, migrates a v2 database to v3. Tables: `artifacts`, `cohorts`, `forecasts`, `forecast_baselines`, `paper_tickets`, `decisions`, `cash_ledger`, `settlements`.

- [ ] **Step 1: Write the failing tests**

`tests/predict/test_ledger_schema.py`:

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from predict_agent.db import SCHEMA, SCHEMA_VERSION, connect

LEDGER_TABLES = {
    "artifacts",
    "cohorts",
    "forecasts",
    "forecast_baselines",
    "paper_tickets",
    "decisions",
    "cash_ledger",
    "settlements",
}


class LedgerSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "data" / "predict.sqlite3"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def tables(self, conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {row[0] for row in rows}

    def test_fresh_database_has_ledger_tables_and_version_3(self) -> None:
        conn = connect(self.path)
        try:
            self.assertEqual(SCHEMA_VERSION, 3)
            self.assertLessEqual(LEDGER_TABLES, self.tables(conn))
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            conn.close()

    def test_v2_database_migrates_by_adding_tables(self) -> None:
        self.path.parent.mkdir(parents=True)
        raw = sqlite3.connect(self.path)
        try:
            raw.executescript(SCHEMA)
            raw.execute("INSERT INTO schema_version (version) VALUES (2)")
            raw.execute(
                "INSERT INTO runs (run_id, command, started_at, policy_hash) "
                "VALUES ('r1', 'discover', '2026-10-05T12:00:00Z', 'p')"
            )
            raw.commit()
        finally:
            raw.close()
        conn = connect(self.path)
        try:
            version = conn.execute("SELECT version FROM schema_version").fetchall()
            self.assertEqual([row[0] for row in version], [3])
            self.assertLessEqual(LEDGER_TABLES, self.tables(conn))
            self.assertEqual(conn.execute("SELECT run_id FROM runs").fetchone()[0], "r1")
        finally:
            conn.close()

    def test_unknown_version_is_refused(self) -> None:
        self.path.parent.mkdir(parents=True)
        raw = sqlite3.connect(self.path)
        try:
            raw.executescript(SCHEMA)
            raw.execute("INSERT INTO schema_version (version) VALUES (1)")
            raw.commit()
        finally:
            raw.close()
        with self.assertRaisesRegex(RuntimeError, "unsupported predict schema version 1"):
            connect(self.path)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_schema -v`
Expected: FAIL — `AssertionError: 2 != 3` and missing ledger tables.

- [ ] **Step 3: Implement**

`src/predict_agent/ledger_schema.py`:

```python
"""Ledger tables (Plan 2). Every table is append-only except the two documented
transitions: paper_tickets OPEN -> SETTLED and cohorts ACTIVE -> CLOSED."""

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL
        CHECK (kind IN ('prompt', 'policy', 'research_input', 'tool_transcript')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cohorts (
    cohort_id TEXT PRIMARY KEY,
    identity_json TEXT NOT NULL,
    code_version TEXT NOT NULL,
    starting_bankroll TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED')),
    started_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cohort_id TEXT NOT NULL REFERENCES cohorts (cohort_id),
    condition_id TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('entry', 'update')),
    created_at TEXT NOT NULL,
    abstained INTEGER NOT NULL,
    abstain_reason TEXT,
    p_low TEXT,
    p_mid TEXT,
    p_high TEXT,
    confidence TEXT,
    base_rate TEXT,
    body_json TEXT NOT NULL,
    research_input_hash TEXT NOT NULL REFERENCES artifacts (artifact_hash),
    transcript_hash TEXT NOT NULL REFERENCES artifacts (artifact_hash),
    cost_usd TEXT NOT NULL,
    forecast_hash TEXT NOT NULL UNIQUE,
    FOREIGN KEY (condition_id, rules_hash) REFERENCES rules_versions (condition_id, rules_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_entry_forecast_per_market
    ON forecasts (cohort_id, condition_id) WHERE kind = 'entry';
CREATE TABLE IF NOT EXISTS forecast_baselines (
    forecast_id INTEGER PRIMARY KEY REFERENCES forecasts (forecast_id),
    yes_snapshot_id INTEGER REFERENCES book_snapshots (id),
    no_snapshot_id INTEGER REFERENCES book_snapshots (id),
    reason TEXT,
    attached_at TEXT NOT NULL,
    CHECK (
        (yes_snapshot_id IS NOT NULL AND no_snapshot_id IS NOT NULL AND reason IS NULL)
        OR (yes_snapshot_id IS NULL AND no_snapshot_id IS NULL AND reason IS NOT NULL)
    )
);
CREATE TABLE IF NOT EXISTS paper_tickets (
    ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cohort_id TEXT NOT NULL REFERENCES cohorts (cohort_id),
    forecast_id INTEGER NOT NULL UNIQUE REFERENCES forecasts (forecast_id),
    snapshot_id INTEGER NOT NULL REFERENCES book_snapshots (id),
    condition_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('YES', 'NO')),
    direction TEXT NOT NULL CHECK (direction = 'BUY'),
    shares TEXT NOT NULL,
    fills_json TEXT NOT NULL,
    fee TEXT NOT NULL,
    cost_total TEXT NOT NULL,
    policy_hash TEXT NOT NULL REFERENCES artifacts (artifact_hash),
    rules_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'SETTLED')),
    ticket_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (cohort_id, condition_id)
);
CREATE TABLE IF NOT EXISTS decisions (
    forecast_id INTEGER PRIMARY KEY REFERENCES forecasts (forecast_id),
    cohort_id TEXT NOT NULL REFERENCES cohorts (cohort_id),
    condition_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('TRADED', 'REFUSED', 'NO_TIMELY_BASELINE')),
    reason TEXT,
    ticket_id INTEGER REFERENCES paper_tickets (ticket_id),
    decided_at TEXT NOT NULL,
    CHECK ((kind = 'TRADED') = (ticket_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS cash_ledger (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cohort_id TEXT NOT NULL REFERENCES cohorts (cohort_id),
    entry_type TEXT NOT NULL CHECK (entry_type IN ('FUNDING', 'DEBIT', 'CREDIT')),
    amount TEXT NOT NULL,
    ticket_id INTEGER REFERENCES paper_tickets (ticket_id),
    at TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS settlements (
    ticket_id INTEGER PRIMARY KEY REFERENCES paper_tickets (ticket_id),
    observation_id INTEGER NOT NULL REFERENCES resolution_observations (id),
    outcome TEXT NOT NULL CHECK (outcome IN ('YES', 'NO', 'HALF')),
    payout_per_share TEXT NOT NULL,
    payout TEXT NOT NULL,
    net_pnl TEXT NOT NULL,
    rules_hash_at_settlement TEXT NOT NULL,
    settled_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS artifacts_no_update BEFORE UPDATE ON artifacts
BEGIN SELECT RAISE(ABORT, 'artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS artifacts_no_delete BEFORE DELETE ON artifacts
BEGIN SELECT RAISE(ABORT, 'artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecasts_no_update BEFORE UPDATE ON forecasts
BEGIN SELECT RAISE(ABORT, 'forecasts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecasts_no_delete BEFORE DELETE ON forecasts
BEGIN SELECT RAISE(ABORT, 'forecasts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS baselines_no_update BEFORE UPDATE ON forecast_baselines
BEGIN SELECT RAISE(ABORT, 'baselines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS baselines_no_delete BEFORE DELETE ON forecast_baselines
BEGIN SELECT RAISE(ABORT, 'baselines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS cash_no_update BEFORE UPDATE ON cash_ledger
BEGIN SELECT RAISE(ABORT, 'cash ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS cash_no_delete BEFORE DELETE ON cash_ledger
BEGIN SELECT RAISE(ABORT, 'cash ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS settlements_no_update BEFORE UPDATE ON settlements
BEGIN SELECT RAISE(ABORT, 'settlements are immutable'); END;
CREATE TRIGGER IF NOT EXISTS settlements_no_delete BEFORE DELETE ON settlements
BEGIN SELECT RAISE(ABORT, 'settlements are immutable'); END;
CREATE TRIGGER IF NOT EXISTS tickets_no_delete BEFORE DELETE ON paper_tickets
BEGIN SELECT RAISE(ABORT, 'tickets are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS tickets_open_to_settled_only BEFORE UPDATE ON paper_tickets
WHEN NOT (
    OLD.status = 'OPEN' AND NEW.status = 'SETTLED'
    AND NEW.ticket_id IS OLD.ticket_id AND NEW.cohort_id IS OLD.cohort_id
    AND NEW.forecast_id IS OLD.forecast_id AND NEW.snapshot_id IS OLD.snapshot_id
    AND NEW.condition_id IS OLD.condition_id AND NEW.outcome IS OLD.outcome
    AND NEW.direction IS OLD.direction AND NEW.shares IS OLD.shares
    AND NEW.fills_json IS OLD.fills_json AND NEW.fee IS OLD.fee
    AND NEW.cost_total IS OLD.cost_total AND NEW.policy_hash IS OLD.policy_hash
    AND NEW.rules_hash IS OLD.rules_hash AND NEW.ticket_hash IS OLD.ticket_hash
    AND NEW.created_at IS OLD.created_at
)
BEGIN SELECT RAISE(ABORT, 'tickets only move OPEN -> SETTLED'); END;
CREATE TRIGGER IF NOT EXISTS cohorts_no_delete BEFORE DELETE ON cohorts
BEGIN SELECT RAISE(ABORT, 'cohorts are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS cohorts_active_to_closed_only BEFORE UPDATE ON cohorts
WHEN NOT (
    OLD.status = 'ACTIVE' AND NEW.status = 'CLOSED' AND NEW.closed_at IS NOT NULL
    AND NEW.cohort_id IS OLD.cohort_id AND NEW.identity_json IS OLD.identity_json
    AND NEW.code_version IS OLD.code_version
    AND NEW.starting_bankroll IS OLD.starting_bankroll
    AND NEW.started_at IS OLD.started_at
)
BEGIN SELECT RAISE(ABORT, 'cohorts only move ACTIVE -> CLOSED'); END;
"""
```

Modify `src/predict_agent/db.py`:

1. Replace `SCHEMA_VERSION = 2` with:

```python
SCHEMA_VERSION = 3
# v2 -> v3 only adds the ledger tables, which CREATE ... IF NOT EXISTS creates in place.
MIGRATABLE_FROM = frozenset({2})
```

2. Add the import after `from .util import ...`:

```python
from .ledger_schema import LEDGER_SCHEMA
```

3. In `connect`, replace the block from `conn.row_factory = sqlite3.Row` through the version check with:

```python
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA + LEDGER_SCHEMA)
        versions = [row["version"] for row in conn.execute("SELECT version FROM schema_version")]
        if not versions:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif versions == [SCHEMA_VERSION]:
            pass
        elif len(versions) == 1 and versions[0] in MIGRATABLE_FROM:
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        else:
            raise RuntimeError(f"unsupported predict schema version {versions[0]}")
```

- [ ] **Step 4: Run to verify pass, then the full suite**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_schema -v` → 3 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK (Plan 1's `test_reconnect_is_idempotent` reads `SCHEMA_VERSION`, so it follows the bump).

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/ledger_schema.py src/predict_agent/db.py tests/predict/test_ledger_schema.py
git commit -m "feat(predict): ledger schema v3 with v2 migration"
```

---
### Task 2: Artifacts and cash entries

**Files:**
- Create: `src/predict_agent/artifacts.py`, `src/predict_agent/cash.py`, `tests/predict/test_ledger_cash.py`

**Interfaces:**
- Consumes: `db.connect`, `db.transaction`, `db.append_journal`, `db.GENESIS_HASH`; `util.sha256_text`, `util.sha256_json`, `util.isoformat`.
- Produces:
  - `artifacts.ARTIFACT_KINDS = frozenset({"prompt", "policy", "research_input", "tool_transcript"})`
  - `artifacts.store_artifact(conn, kind: str, content: str, now: datetime) -> str` (returns SHA-256 hex; idempotent; refuses a hash already stored under another kind)
  - `artifacts.load_artifact(conn, digest: str) -> tuple[str, str]` (kind, content; `KeyError` if absent; `RuntimeError` if content no longer matches its hash)
  - `cash.LedgerError(RuntimeError)`, `cash.InsufficientCash(LedgerError)`
  - `cash.money_text(value: Decimal) -> str`, `cash.parse_money(text: str) -> Decimal`
  - `cash.append_cash_entry(conn, cohort_id: str, entry_type: str, amount: Decimal, ticket_id: int | None, now: datetime) -> int` — caller holds the transaction; per-cohort hash chain; journals `CASH_<TYPE>`; a `DEBIT` larger than available cash raises `InsufficientCash`
  - `cash.available_cash(conn, cohort_id: str) -> Decimal`
  - `cash.cash_entry_hash(cohort_id: str, entry_type: str, amount_text: str, ticket_id: int | None, at_text: str, prev_hash: str) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/predict/test_ledger_cash.py`:

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from predict_agent.artifacts import load_artifact, store_artifact
from predict_agent.cash import (
    InsufficientCash,
    LedgerError,
    append_cash_entry,
    available_cash,
)
from predict_agent.db import connect, transaction
from tests.predict.fixtures import NOW


class CashTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.conn.execute(
            "INSERT INTO cohorts (cohort_id, identity_json, code_version, starting_bankroll, "
            "status, started_at) VALUES ('c1', '{}', 'test', '1', 'ACTIVE', '2026-10-05T12:00:00Z')"
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def entry(self, entry_type: str, amount: str) -> int:
        with transaction(self.conn):
            return append_cash_entry(self.conn, "c1", entry_type, Decimal(amount), None, NOW)


class ArtifactTests(CashTestCase):
    def test_store_is_content_addressed_and_idempotent(self) -> None:
        first = store_artifact(self.conn, "prompt", "Forecast {question}", NOW)
        second = store_artifact(self.conn, "prompt", "Forecast {question}", NOW)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertEqual(load_artifact(self.conn, first), ("prompt", "Forecast {question}"))

    def test_same_content_under_another_kind_is_refused(self) -> None:
        store_artifact(self.conn, "prompt", "x", NOW)
        with self.assertRaisesRegex(ValueError, "already stored as prompt"):
            store_artifact(self.conn, "policy", "x", NOW)

    def test_unknown_kind_and_missing_hash(self) -> None:
        with self.assertRaises(ValueError):
            store_artifact(self.conn, "notes", "x", NOW)
        with self.assertRaises(KeyError):
            load_artifact(self.conn, "0" * 64)

    def test_artifacts_are_immutable(self) -> None:
        digest = store_artifact(self.conn, "policy", "{}", NOW)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "UPDATE artifacts SET content = 'y' WHERE artifact_hash = ?", (digest,)
            )


class CashTests(CashTestCase):
    def test_cash_sums_are_exact_decimal(self) -> None:
        self.entry("FUNDING", "1.0")
        for _ in range(3):
            self.entry("DEBIT", "0.1")
        balance = available_cash(self.conn, "c1")
        self.assertEqual(balance, Decimal("0.7"))
        self.assertEqual(str(balance), "0.7")

    def test_debit_beyond_cash_raises_and_writes_nothing(self) -> None:
        self.entry("FUNDING", "10")
        with self.assertRaises(InsufficientCash):
            self.entry("DEBIT", "10.01")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cash_ledger").fetchone()[0], 1)
        self.assertEqual(available_cash(self.conn, "c1"), Decimal("10"))

    def test_invalid_amounts_rejected(self) -> None:
        for entry_type, amount in (
            ("FUNDING", "0"),
            ("CREDIT", "-1"),
            ("DEBIT", "NaN"),
            ("REFUND", "1"),
        ):
            with (
                self.subTest(entry_type=entry_type, amount=amount),
                self.assertRaises(LedgerError),
            ):
                self.entry(entry_type, amount)

    def test_zero_credit_is_allowed(self) -> None:
        self.entry("FUNDING", "5")
        self.entry("CREDIT", "0")
        self.assertEqual(available_cash(self.conn, "c1"), Decimal("5"))

    def test_entries_chain_and_journal(self) -> None:
        self.entry("FUNDING", "5")
        self.entry("DEBIT", "2")
        rows = self.conn.execute("SELECT entry_hash FROM cash_ledger ORDER BY entry_id").fetchall()
        self.assertEqual(len({row[0] for row in rows}), 2)
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM journal ORDER BY seq")]
        self.assertEqual(kinds, ["CASH_FUNDING", "CASH_DEBIT"])

    def test_cash_ledger_is_append_only(self) -> None:
        self.entry("FUNDING", "5")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE cash_ledger SET amount = '500'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM cash_ledger")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_cash -v`
Expected: ERROR `No module named 'predict_agent.artifacts'`.

- [ ] **Step 3: Implement**

`src/predict_agent/artifacts.py`:

```python
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
```

`src/predict_agent/cash.py`:

```python
"""Per-cohort virtual cash. Amounts are Decimal text and are summed in Python: SQLite's
SUM() would convert TEXT to float."""

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


def available_cash(conn: sqlite3.Connection, cohort_id: str) -> Decimal:
    total = Decimal("0")
    for row in conn.execute(
        "SELECT entry_type, amount FROM cash_ledger WHERE cohort_id = ?", (cohort_id,)
    ):
        total += ENTRY_SIGNS[row["entry_type"]] * parse_money(row["amount"])
    return total


def cash_entry_hash(
    cohort_id: str,
    entry_type: str,
    amount_text: str,
    ticket_id: int | None,
    at_text: str,
    prev_hash: str,
) -> str:
    return sha256_json(
        {
            "cohort_id": cohort_id,
            "entry_type": entry_type,
            "amount": amount_text,
            "ticket_id": ticket_id,
            "at": at_text,
            "prev_hash": prev_hash,
        }
    )


def append_cash_entry(
    conn: sqlite3.Connection,
    cohort_id: str,
    entry_type: str,
    amount: Decimal,
    ticket_id: int | None,
    now: datetime,
) -> int:
    """Append one cash entry and its journal entry. The caller holds the transaction."""
    if entry_type not in ENTRY_SIGNS:
        raise LedgerError(f"unknown cash entry type {entry_type!r}")
    amount_text = money_text(amount)
    if amount < 0 or (entry_type == "FUNDING" and amount == 0):
        raise LedgerError(f"invalid {entry_type} amount {amount_text}")
    if entry_type == "DEBIT":
        available = available_cash(conn, cohort_id)
        if available < amount:
            raise InsufficientCash(
                f"cohort {cohort_id[:12]}: debit {amount_text} exceeds available "
                f"{money_text(available)}"
            )
    last = conn.execute(
        "SELECT entry_hash FROM cash_ledger WHERE cohort_id = ? ORDER BY entry_id DESC LIMIT 1",
        (cohort_id,),
    ).fetchone()
    prev_hash = last["entry_hash"] if last else GENESIS_HASH
    at_text = isoformat(now)
    entry_hash = cash_entry_hash(cohort_id, entry_type, amount_text, ticket_id, at_text, prev_hash)
    cursor = conn.execute(
        "INSERT INTO cash_ledger (cohort_id, entry_type, amount, ticket_id, at, entry_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (cohort_id, entry_type, amount_text, ticket_id, at_text, entry_hash),
    )
    append_journal(
        conn,
        f"CASH_{entry_type}",
        {
            "cohort_id": cohort_id,
            "amount": amount_text,
            "ticket_id": ticket_id,
            "entry_hash": entry_hash,
        },
        now,
    )
    if cursor.lastrowid is None:
        raise LedgerError("cash entry insert returned no row id")
    return cursor.lastrowid
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_cash -v` → 10 tests OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/artifacts.py src/predict_agent/cash.py tests/predict/test_ledger_cash.py
git commit -m "feat(predict): content-addressed artifacts and hash-chained cash entries"
```

---

### Task 3: Cohorts

**Files:**
- Create: `src/predict_agent/cohorts.py`, `tests/predict/test_ledger_cohorts.py`

**Interfaces:**
- Consumes: `db.transaction`, `db.append_journal`; `cash.append_cash_entry`, `cash.money_text`, `cash.LedgerError`; `util.sha256_json`, `util.isoformat`.
- Produces:
  - `cohorts.CohortIdentity` frozen dataclass: `policy_hash: str`, `prompt_hash: str`, `model_id: str`, `research_settings: Mapping[str, Any]`, `scoring_version: str`; method `record() -> dict[str, Any]`
  - `cohorts.cohort_id_for(identity: CohortIdentity) -> str` (SHA-256 of the identity record; any field change gives a new id)
  - `cohorts.ensure_cohort(conn, identity, *, starting_bankroll: Decimal, code_version: str, now: datetime) -> str` — returns the active cohort for this identity, or opens it (closing every other active cohort, funding it once); refuses a closed identity, unknown artifacts, a non-positive bankroll
  - `cohorts.active_cohort(conn) -> str | None`
  - `cohorts.code_version(root: Path) -> str` (`"<40-hex sha>"`, `"<sha>-dirty"`, or `"unknown"`)

- [ ] **Step 1: Write the failing tests**

`tests/predict/test_ledger_cohorts.py`:

```python
from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from predict_agent.artifacts import store_artifact
from predict_agent.cash import LedgerError, available_cash
from predict_agent.cohorts import (
    CohortIdentity,
    active_cohort,
    code_version,
    cohort_id_for,
    ensure_cohort,
)
from predict_agent.db import connect
from tests.predict.fixtures import NOW

REPO = Path(__file__).resolve().parents[2]


class CohortTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        policy = store_artifact(self.conn, "policy", '{"edge": "0.05"}', NOW)
        prompt = store_artifact(self.conn, "prompt", "Forecast without prices.", NOW)
        self.identity = CohortIdentity(
            policy_hash=policy,
            prompt_hash=prompt,
            model_id="claude-model-x",
            research_settings={"tools": ["WebSearch", "WebFetch"], "daily_usd": "5"},
            scoring_version="1",
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def open(self, identity: CohortIdentity, bankroll: str = "1000") -> str:
        return ensure_cohort(
            self.conn, identity, starting_bankroll=Decimal(bankroll), code_version="abc", now=NOW
        )

    def test_same_identity_returns_same_cohort_funded_once(self) -> None:
        first = self.open(self.identity)
        self.assertEqual(self.open(self.identity), first)
        self.assertEqual(available_cash(self.conn, first), Decimal("1000"))
        self.assertEqual(active_cohort(self.conn), first)

    def test_every_identity_field_changes_the_cohort(self) -> None:
        other_policy = store_artifact(self.conn, "policy", '{"edge": "0.06"}', NOW)
        other_prompt = store_artifact(self.conn, "prompt", "Forecast v2.", NOW)
        variants = {
            "policy_hash": replace(self.identity, policy_hash=other_policy),
            "prompt_hash": replace(self.identity, prompt_hash=other_prompt),
            "model_id": replace(self.identity, model_id="claude-model-y"),
            "research_settings": replace(self.identity, research_settings={"tools": []}),
            "scoring_version": replace(self.identity, scoring_version="2"),
        }
        base = cohort_id_for(self.identity)
        for field, variant in variants.items():
            with self.subTest(field=field):
                self.assertNotEqual(cohort_id_for(variant), base)

    def test_new_identity_closes_old_and_funds_independently(self) -> None:
        old = self.open(self.identity)
        new = self.open(replace(self.identity, model_id="claude-model-y"), bankroll="250")
        statuses = dict(self.conn.execute("SELECT cohort_id, status FROM cohorts").fetchall())
        self.assertEqual(statuses, {old: "CLOSED", new: "ACTIVE"})
        self.assertEqual(available_cash(self.conn, old), Decimal("1000"))
        self.assertEqual(available_cash(self.conn, new), Decimal("250"))
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM journal ORDER BY seq")]
        self.assertIn("COHORT_CLOSED", kinds)

    def test_reopening_a_closed_identity_is_refused(self) -> None:
        self.open(self.identity)
        self.open(replace(self.identity, model_id="claude-model-y"))
        with self.assertRaisesRegex(LedgerError, "closed"):
            self.open(self.identity)

    def test_unknown_artifacts_and_bad_bankroll_refused(self) -> None:
        with self.assertRaisesRegex(LedgerError, "prompt artifact"):
            self.open(replace(self.identity, prompt_hash="0" * 64))
        with self.assertRaisesRegex(LedgerError, "policy artifact"):
            self.open(replace(self.identity, policy_hash=self.identity.prompt_hash))
        for bankroll in ("0", "-5", "NaN"):
            with self.subTest(bankroll=bankroll), self.assertRaises(LedgerError):
                self.open(self.identity, bankroll=bankroll)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0], 0)

    def test_cohort_rows_only_close(self) -> None:
        cohort = self.open(self.identity)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "UPDATE cohorts SET starting_bankroll = '9' WHERE cohort_id = ?", (cohort,)
            )

    def test_code_version(self) -> None:
        self.assertRegex(code_version(REPO), re.compile(r"^[0-9a-f]{40}(-dirty)?$"))
        self.assertEqual(code_version(Path(self._tmp.name)), "unknown")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_cohorts -v`
Expected: ERROR `No module named 'predict_agent.cohorts'`.

- [ ] **Step 3: Implement**

`src/predict_agent/cohorts.py`:

```python
"""Cohorts: one independent virtual portfolio per research/policy identity (spec §4)."""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .cash import LedgerError, append_cash_entry, money_text
from .db import append_journal, transaction
from .util import canonical_json, isoformat, sha256_json


@dataclass(frozen=True)
class CohortIdentity:
    policy_hash: str
    prompt_hash: str
    model_id: str
    research_settings: Mapping[str, Any]
    scoring_version: str

    def record(self) -> dict[str, Any]:
        return {
            "policy_hash": self.policy_hash,
            "prompt_hash": self.prompt_hash,
            "model_id": self.model_id,
            "research_settings": dict(self.research_settings),
            "scoring_version": self.scoring_version,
        }


def cohort_id_for(identity: CohortIdentity) -> str:
    return sha256_json(identity.record())


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
    if not identity.model_id or not identity.scoring_version:
        raise LedgerError("cohort identity needs a model id and a scoring version")
    cohort_id = cohort_id_for(identity)
    with transaction(conn):
        for digest, kind in ((identity.policy_hash, "policy"), (identity.prompt_hash, "prompt")):
            row = conn.execute(
                "SELECT kind FROM artifacts WHERE artifact_hash = ?", (digest,)
            ).fetchone()
            if row is None or row["kind"] != kind:
                raise LedgerError(f"{kind} artifact {digest[:12]} is not stored")
        existing = conn.execute(
            "SELECT status FROM cohorts WHERE cohort_id = ?", (cohort_id,)
        ).fetchone()
        if existing is not None:
            if existing["status"] == "ACTIVE":
                return cohort_id
            raise LedgerError(
                f"cohort {cohort_id[:12]} is closed; refusing to reopen it "
                "(change the scoring version to start a fresh portfolio)"
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
        conn.execute(
            "INSERT INTO cohorts (cohort_id, identity_json, code_version, starting_bankroll, "
            "status, started_at) VALUES (?, ?, ?, ?, 'ACTIVE', ?)",
            (
                cohort_id,
                canonical_json(identity.record()),
                code_version,
                money_text(starting_bankroll),
                now_text,
            ),
        )
        append_cash_entry(conn, cohort_id, "FUNDING", starting_bankroll, None, now)
        append_journal(
            conn,
            "COHORT_OPENED",
            {
                "cohort_id": cohort_id,
                "identity": identity.record(),
                "code_version": code_version,
                "starting_bankroll": money_text(starting_bankroll),
            },
            now,
        )
    return cohort_id
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_cohorts -v` → 7 tests OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/cohorts.py tests/predict/test_ledger_cohorts.py
git commit -m "feat(predict): cohorts with independent funding and identity hashing"
```

---

### Task 4: Forecast records, baselines and resume state

**Files:**
- Create: `src/predict_agent/forecasts.py`, `tests/predict/ledger_fixtures.py`, `tests/predict/test_ledger_forecasts.py`

**Interfaces:**
- Consumes: `db.transaction`, `db.append_journal`; `cash.LedgerError`, `cash.money_text`; `artifacts.store_artifact`; `cohorts.CohortIdentity`, `cohorts.ensure_cohort`; `util.*`.
- Produces:
  - `forecasts.ForecastError(LedgerError)`
  - `forecasts.ForecastRecord` frozen dataclass: `cohort_id: str`, `condition_id: str`, `rules_hash: str`, `kind: str` (`"entry"|"update"`), `abstained: bool`, `abstain_reason: str | None`, `p_low/p_mid/p_high: Decimal | None`, `confidence: str | None` (`low|medium|high`), `base_rate: Decimal | None`, `body: Mapping[str, Any]`, `research_input_hash: str`, `transcript_hash: str`, `cost_usd: Decimal`
  - `forecasts.validate_forecast(record: ForecastRecord) -> None`
  - `forecasts.record_forecast(conn, record, now) -> int` (forecast id; active cohort only; one entry per market per cohort)
  - `forecasts.attach_baseline(conn, forecast_id: int, yes_snapshot_id: int, no_snapshot_id: int, now) -> None` (both snapshots fetched at or after the forecast; set once)
  - `forecasts.mark_no_timely_baseline(conn, forecast_id: int, now) -> None` (baseline reason `NO_TIMELY_BASELINE`; entry forecasts also get decision `NO_TIMELY_BASELINE`)
  - `forecasts.ResumeStep` (`StrEnum`: `NEEDS_BASELINE`, `BASELINE_EXPIRED`, `NEEDS_DECISION`, `DONE`)
  - `forecasts.resume_step(conn, forecast_id: int, now, baseline_window: timedelta) -> ResumeStep`
  - `forecasts.unfinished_forecasts(conn, cohort_id: str) -> list[int]`
  - Test helpers in `tests/predict/ledger_fixtures.py`: `seed_market(conn, condition_id=CONDITION_ID) -> str` (rules hash), `seed_snapshot(conn, outcome, fetched_at, condition_id=CONDITION_ID) -> int`, `seed_observation(conn, outcome, *, cross_check="CONFIRMED", status="resolved", condition_id=CONDITION_ID) -> int`, `seed_cohort(conn, *, model_id="m1", bankroll="1000") -> str`, `forecast_record(conn, cohort_id, rules_hash, **overrides) -> ForecastRecord`, `seed_entry_forecast(conn, cohort_id, rules_hash, *, at=NOW, **overrides) -> int`

- [ ] **Step 1: Write the fixtures and failing tests**

`tests/predict/ledger_fixtures.py`:

```python
"""Seed helpers for ledger tests. Rows mirror what Plan 1 collection and later plans write."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from predict_agent.artifacts import store_artifact
from predict_agent.cohorts import CohortIdentity, ensure_cohort
from predict_agent.forecasts import ForecastRecord, record_forecast
from predict_agent.util import canonical_json, isoformat, sha256_json
from tests.predict.fixtures import CONDITION_ID, NO_TOKEN, NOW, YES_TOKEN


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
) -> int:
    token = YES_TOKEN if outcome == "YES" else NO_TOKEN
    cursor = conn.execute(
        "INSERT INTO book_snapshots (run_id, condition_id, token_id, source_run_id, outcome, "
        "observed_at, fetched_at, record_json, fees_enabled, fee_schedule_json, snapshot_hash) "
        "VALUES ('r', ?, ?, 'r', ?, ?, ?, '{}', 0, NULL, ?)",
        (
            condition_id,
            token,
            outcome,
            isoformat(fetched_at),
            isoformat(fetched_at),
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
) -> int:
    cursor = conn.execute(
        "INSERT INTO resolution_observations (run_id, condition_id, resolution_fetched_at, "
        "gamma_fetched_at, gamma_json, status, outcome, cross_check, was_disputed, "
        "new_version_q, raw_json) VALUES ('r', ?, ?, ?, '{}', ?, ?, ?, 0, 0, '{}')",
        (condition_id, isoformat(NOW), isoformat(NOW), status, outcome, cross_check),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def seed_cohort(
    conn: sqlite3.Connection, *, model_id: str = "m1", bankroll: str = "1000"
) -> str:
    identity = CohortIdentity(
        policy_hash=store_artifact(conn, "policy", '{"min_edge": "0.05"}', NOW),
        prompt_hash=store_artifact(conn, "prompt", "Forecast without market prices.", NOW),
        model_id=model_id,
        research_settings={"tools": ["WebSearch", "WebFetch"]},
        scoring_version="1",
    )
    return ensure_cohort(
        conn, identity, starting_bankroll=Decimal(bankroll), code_version="test", now=NOW
    )


def forecast_record(
    conn: sqlite3.Connection, cohort_id: str, rules_hash: str, **overrides: Any
) -> ForecastRecord:
    record = ForecastRecord(
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
    return record_forecast(conn, forecast_record(conn, cohort_id, rules_hash, **overrides), at)
```

`tests/predict/test_ledger_forecasts.py`:

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from predict_agent.db import connect
from predict_agent.forecasts import (
    ForecastError,
    ResumeStep,
    attach_baseline,
    mark_no_timely_baseline,
    record_forecast,
    resume_step,
    unfinished_forecasts,
    validate_forecast,
)
from tests.predict.fixtures import NOW
from tests.predict.ledger_fixtures import (
    forecast_record,
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_snapshot,
)

WINDOW = timedelta(minutes=30)


class ForecastTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.rules_hash = seed_market(self.conn)
        self.cohort = seed_cohort(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()


class RecordTests(ForecastTestCase):
    def test_records_entry_forecast_and_journals(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        row = self.conn.execute(
            "SELECT p_mid, cost_usd, kind FROM forecasts WHERE forecast_id = ?", (forecast_id,)
        ).fetchone()
        self.assertEqual(tuple(row), ("0.60", "0.12", "entry"))
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM journal ORDER BY seq")]
        self.assertEqual(kinds[-1], "FORECAST_RECORDED")

    def test_validation_rejects_bad_records(self) -> None:
        base = forecast_record(self.conn, self.cohort, self.rules_hash)
        bad = {
            "p_high above 0.99": replace(base, p_high=Decimal("0.995")),
            "unordered range": replace(base, p_low=Decimal("0.70")),
            "abstained with p": replace(base, abstained=True, abstain_reason="ambiguous"),
            "abstained without reason": replace(
                base, abstained=True, p_low=None, p_mid=None, p_high=None, confidence=None
            ),
            "missing confidence": replace(base, confidence=None),
            "unknown confidence": replace(base, confidence="certain"),
            "nan cost": replace(base, cost_usd=Decimal("NaN")),
            "base rate above 1": replace(base, base_rate=Decimal("1.5")),
            "unknown kind": replace(base, kind="draft"),
            "bad hash": replace(base, transcript_hash="xyz"),
        }
        for label, record in bad.items():
            with self.subTest(label), self.assertRaises(ForecastError):
                validate_forecast(record)

    def test_abstention_is_valid(self) -> None:
        abstained = forecast_record(
            self.conn,
            self.cohort,
            self.rules_hash,
            abstained=True,
            abstain_reason="rules ambiguous",
            p_low=None,
            p_mid=None,
            p_high=None,
            confidence=None,
        )
        validate_forecast(abstained)
        self.assertGreater(record_forecast(self.conn, abstained, NOW), 0)

    def test_one_entry_forecast_per_market_but_many_updates(self) -> None:
        seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        with self.assertRaisesRegex(ForecastError, "entry forecast already exists"):
            seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        for minutes in (1, 2):
            seed_entry_forecast(
                self.conn,
                self.cohort,
                self.rules_hash,
                kind="update",
                at=NOW + timedelta(minutes=minutes),
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0], 3)
        with self.assertRaisesRegex(ForecastError, "identical forecast"):
            seed_entry_forecast(
                self.conn,
                self.cohort,
                self.rules_hash,
                kind="update",
                at=NOW + timedelta(minutes=2),
            )

    def test_closed_cohort_and_unknown_rules_refused(self) -> None:
        seed_cohort(self.conn, model_id="m2")
        with self.assertRaisesRegex(ForecastError, "not active"):
            seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        active = seed_cohort(self.conn, model_id="m3")
        with self.assertRaisesRegex(ForecastError, "rules version"):
            seed_entry_forecast(self.conn, active, "0" * 64)

    def test_forecasts_are_immutable(self) -> None:
        seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE forecasts SET p_mid = '0.99'")


class BaselineTests(ForecastTestCase):
    def test_baseline_attaches_once(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        yes = seed_snapshot(self.conn, "YES", NOW + timedelta(seconds=5))
        no = seed_snapshot(self.conn, "NO", NOW + timedelta(seconds=6))
        attach_baseline(self.conn, forecast_id, yes, no, NOW + timedelta(seconds=7))
        with self.assertRaisesRegex(ForecastError, "already"):
            attach_baseline(self.conn, forecast_id, yes, no, NOW + timedelta(seconds=8))

    def test_baseline_snapshot_before_forecast_is_refused(self) -> None:
        # The forecast carries microseconds; the snapshot does not. As strings,
        # "12:00:00Z" sorts after "12:00:00.500000Z", so only parsed times compare correctly.
        forecast_id = seed_entry_forecast(
            self.conn, self.cohort, self.rules_hash, at=NOW + timedelta(milliseconds=500)
        )
        yes = seed_snapshot(self.conn, "YES", NOW)
        no = seed_snapshot(self.conn, "NO", NOW + timedelta(seconds=1))
        with self.assertRaisesRegex(ForecastError, "before the forecast"):
            attach_baseline(self.conn, forecast_id, yes, no, NOW + timedelta(seconds=2))

    def test_baseline_sides_and_market_must_match(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        yes = seed_snapshot(self.conn, "YES", NOW + timedelta(seconds=1))
        no = seed_snapshot(self.conn, "NO", NOW + timedelta(seconds=1))
        with self.assertRaisesRegex(ForecastError, "outcome"):
            attach_baseline(self.conn, forecast_id, no, yes, NOW + timedelta(seconds=2))
        other = "0x" + "9" * 64
        seed_market(self.conn, other)
        foreign = seed_snapshot(self.conn, "YES", NOW + timedelta(seconds=1), other)
        with self.assertRaisesRegex(ForecastError, "market"):
            attach_baseline(self.conn, forecast_id, foreign, no, NOW + timedelta(seconds=2))


class ResumeTests(ForecastTestCase):
    def test_resume_walks_baseline_then_decision(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        later = NOW + timedelta(minutes=1)
        self.assertEqual(
            resume_step(self.conn, forecast_id, later, WINDOW), ResumeStep.NEEDS_BASELINE
        )
        yes = seed_snapshot(self.conn, "YES", later)
        no = seed_snapshot(self.conn, "NO", later)
        attach_baseline(self.conn, forecast_id, yes, no, later)
        self.assertEqual(
            resume_step(self.conn, forecast_id, later, WINDOW), ResumeStep.NEEDS_DECISION
        )
        self.assertEqual(unfinished_forecasts(self.conn, self.cohort), [forecast_id])

    def test_late_resume_marks_no_timely_baseline(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        late = NOW + timedelta(minutes=31)
        self.assertEqual(
            resume_step(self.conn, forecast_id, late, WINDOW), ResumeStep.BASELINE_EXPIRED
        )
        mark_no_timely_baseline(self.conn, forecast_id, late)
        self.assertEqual(resume_step(self.conn, forecast_id, late, WINDOW), ResumeStep.DONE)
        decision = self.conn.execute("SELECT kind FROM decisions").fetchone()[0]
        self.assertEqual(decision, "NO_TIMELY_BASELINE")
        self.assertEqual(unfinished_forecasts(self.conn, self.cohort), [])

    def test_update_forecast_is_done_after_baseline(self) -> None:
        seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        update_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash, kind="update")
        later = NOW + timedelta(minutes=1)
        attach_baseline(
            self.conn,
            update_id,
            seed_snapshot(self.conn, "YES", later),
            seed_snapshot(self.conn, "NO", later),
            later,
        )
        self.assertEqual(resume_step(self.conn, update_id, later, WINDOW), ResumeStep.DONE)
        self.assertNotIn(update_id, unfinished_forecasts(self.conn, self.cohort))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_forecasts -v`
Expected: ERROR `No module named 'predict_agent.forecasts'`.

- [ ] **Step 3: Implement**

`src/predict_agent/forecasts.py`:

```python
"""Forecast records, post-forecast baselines and resume state (spec §3 ordering, §4, §7).
Resume state is derived from durable rows (baselines, decisions), not a separate table."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .cash import LedgerError, money_text
from .db import append_journal, transaction
from .util import canonical_json, isoformat, parse_datetime, sha256_json

P_MIN = Decimal("0.01")
P_MAX = Decimal("0.99")
CONFIDENCE = ("low", "medium", "high")
NO_TIMELY_BASELINE = "NO_TIMELY_BASELINE"
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


def _optional_text(value: Decimal | None) -> str | None:
    return None if value is None else money_text(value)


def validate_forecast(record: ForecastRecord) -> None:
    if record.kind not in ("entry", "update"):
        raise ForecastError(f"unknown forecast kind {record.kind!r}")
    for name in ("rules_hash", "research_input_hash", "transcript_hash"):
        if not _HEX64.match(getattr(record, name)):
            raise ForecastError(f"{name} is not a SHA-256 hex digest")
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
    if record.base_rate is not None and (
        not record.base_rate.is_finite() or not Decimal(0) <= record.base_rate <= Decimal(1)
    ):
        raise ForecastError(f"base rate {record.base_rate} outside [0, 1]")
    if not record.cost_usd.is_finite() or record.cost_usd < 0:
        raise ForecastError(f"cost {record.cost_usd} must be a non-negative amount")


def record_forecast(conn: sqlite3.Connection, record: ForecastRecord, now: datetime) -> int:
    validate_forecast(record)
    created_at = isoformat(now)
    values: dict[str, Any] = {
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
    values["forecast_hash"] = sha256_json(values)
    with transaction(conn):
        cohort = conn.execute(
            "SELECT status FROM cohorts WHERE cohort_id = ?", (record.cohort_id,)
        ).fetchone()
        if cohort is None or cohort["status"] != "ACTIVE":
            raise ForecastError(f"cohort {record.cohort_id[:12]} is not active")
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
            row = conn.execute(
                "SELECT kind FROM artifacts WHERE artifact_hash = ?", (digest,)
            ).fetchone()
            if row is None or row["kind"] != kind:
                raise ForecastError(f"{kind} artifact {digest[:12]} is not stored")
        if record.kind == "entry" and conn.execute(
            "SELECT 1 FROM forecasts WHERE cohort_id = ? AND condition_id = ? AND kind = 'entry'",
            (record.cohort_id, record.condition_id),
        ).fetchone():
            raise ForecastError("entry forecast already exists for this market and cohort")
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        try:
            cursor = conn.execute(
                f"INSERT INTO forecasts ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        except sqlite3.IntegrityError:
            # Same content at the same instant: a double run, not a new forecast.
            raise ForecastError("an identical forecast is already recorded") from None
        if cursor.lastrowid is None:
            raise ForecastError("forecast insert returned no row id")
        forecast_id = cursor.lastrowid
        append_journal(
            conn,
            "FORECAST_RECORDED",
            {
                "forecast_id": forecast_id,
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
        "SELECT * FROM forecasts WHERE forecast_id = ?", (forecast_id,)
    ).fetchone()
    if row is None:
        raise ForecastError(f"unknown forecast {forecast_id}")
    found: sqlite3.Row = row
    return found


def attach_baseline(
    conn: sqlite3.Connection,
    forecast_id: int,
    yes_snapshot_id: int,
    no_snapshot_id: int,
    now: datetime,
) -> None:
    with transaction(conn):
        forecast = _forecast_row(conn, forecast_id)
        if conn.execute(
            "SELECT 1 FROM forecast_baselines WHERE forecast_id = ?", (forecast_id,)
        ).fetchone():
            raise ForecastError(f"forecast {forecast_id} already has a baseline")
        forecast_time = parse_datetime(forecast["created_at"])
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
            if parse_datetime(snap["fetched_at"]) < forecast_time:
                raise ForecastError(
                    f"snapshot {snapshot_id} was fetched before the forecast was committed"
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
    with transaction(conn):
        forecast = _forecast_row(conn, forecast_id)
        if conn.execute(
            "SELECT 1 FROM forecast_baselines WHERE forecast_id = ?", (forecast_id,)
        ).fetchone():
            raise ForecastError(f"forecast {forecast_id} already has a baseline")
        now_text = isoformat(now)
        conn.execute(
            "INSERT INTO forecast_baselines (forecast_id, yes_snapshot_id, no_snapshot_id, "
            "reason, attached_at) VALUES (?, NULL, NULL, ?, ?)",
            (forecast_id, NO_TIMELY_BASELINE, now_text),
        )
        if forecast["kind"] == "entry":
            conn.execute(
                "INSERT INTO decisions (forecast_id, cohort_id, condition_id, kind, reason, "
                "ticket_id, decided_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (
                    forecast_id,
                    forecast["cohort_id"],
                    forecast["condition_id"],
                    NO_TIMELY_BASELINE,
                    "baseline snapshot not taken within the window",
                    now_text,
                ),
            )
        append_journal(conn, NO_TIMELY_BASELINE, {"forecast_id": forecast_id}, now)


def resume_step(
    conn: sqlite3.Connection, forecast_id: int, now: datetime, baseline_window: timedelta
) -> ResumeStep:
    forecast = _forecast_row(conn, forecast_id)
    if conn.execute("SELECT 1 FROM decisions WHERE forecast_id = ?", (forecast_id,)).fetchone():
        return ResumeStep.DONE
    baseline = conn.execute(
        "SELECT reason FROM forecast_baselines WHERE forecast_id = ?", (forecast_id,)
    ).fetchone()
    if baseline is not None:
        if baseline["reason"] is not None or forecast["kind"] == "update":
            return ResumeStep.DONE
        return ResumeStep.NEEDS_DECISION
    if now - parse_datetime(forecast["created_at"]) > baseline_window:
        return ResumeStep.BASELINE_EXPIRED
    return ResumeStep.NEEDS_BASELINE


def unfinished_forecasts(conn: sqlite3.Connection, cohort_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT f.forecast_id FROM forecasts f "
        "LEFT JOIN decisions d ON d.forecast_id = f.forecast_id "
        "LEFT JOIN forecast_baselines b ON b.forecast_id = f.forecast_id "
        "WHERE f.cohort_id = ? AND ("
        "(f.kind = 'entry' AND d.forecast_id IS NULL) "
        "OR (f.kind = 'update' AND b.forecast_id IS NULL)) "
        "ORDER BY f.forecast_id",
        (cohort_id,),
    ).fetchall()
    return [row["forecast_id"] for row in rows]
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_forecasts -v` → all OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/forecasts.py tests/predict/ledger_fixtures.py tests/predict/test_ledger_forecasts.py
git commit -m "feat(predict): forecast records, post-forecast baselines, resume state"
```

---

### Task 5: Tickets, decisions, equity

**Files:**
- Create: `src/predict_agent/tickets.py`, `tests/predict/test_ledger_tickets.py`

**Interfaces:**
- Consumes: `cash.append_cash_entry`, `cash.available_cash`, `cash.money_text`, `cash.parse_money`, `cash.LedgerError`; `db.transaction`, `db.append_journal`; `forecasts.attach_baseline` (tests); fixtures from Task 4.
- Produces:
  - `tickets.Fill` frozen dataclass: `price: Decimal`, `shares: Decimal`
  - `tickets.TicketDraft` frozen dataclass: `cohort_id: str`, `forecast_id: int`, `snapshot_id: int`, `condition_id: str`, `outcome: str` (`"YES"|"NO"`), `fills: tuple[Fill, ...]`, `fee: Decimal`, `policy_hash: str`, `rules_hash: str`
  - `tickets.ticket_totals(draft: TicketDraft) -> tuple[Decimal, Decimal]` (shares, cost_total = Σ price×shares + fee; validates)
  - `tickets.ticket_hash(values: Mapping[str, Any]) -> str` (over every ticket column except `ticket_id`, `status`, `ticket_hash`)
  - `tickets.open_ticket(conn, draft, now) -> int` — one transaction: ticket `OPEN`, `DEBIT`, decision `TRADED`, journal `TICKET_OPENED`; the snapshot must be the forecast's own baseline snapshot for the bought side
  - `tickets.record_refusal_decision(conn, forecast_id: int, reason: str, now) -> None`
  - `tickets.open_cost(conn, cohort_id: str) -> Decimal`, `tickets.equity(conn, cohort_id: str) -> Decimal` (available cash + cost basis of open tickets, spec §5)

- [ ] **Step 1: Write the failing tests**

`tests/predict/test_ledger_tickets.py`:

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from predict_agent.cash import InsufficientCash, LedgerError, available_cash
from predict_agent.db import connect
from predict_agent.forecasts import attach_baseline
from predict_agent.tickets import (
    Fill,
    TicketDraft,
    equity,
    open_cost,
    open_ticket,
    record_refusal_decision,
)
from tests.predict.fixtures import NOW
from tests.predict.ledger_fixtures import (
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_snapshot,
)

LATER = NOW + timedelta(seconds=1)


class TicketTestCase(unittest.TestCase):
    bankroll = "1000"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.rules_hash = seed_market(self.conn)
        self.cohort = seed_cohort(self.conn, bankroll=self.bankroll)
        self.forecast = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        self.yes = seed_snapshot(self.conn, "YES", LATER)
        self.no = seed_snapshot(self.conn, "NO", LATER)
        attach_baseline(self.conn, self.forecast, self.yes, self.no, LATER)
        policy = self.conn.execute(
            "SELECT artifact_hash FROM artifacts WHERE kind = 'policy'"
        ).fetchone()[0]
        self.draft = TicketDraft(
            cohort_id=self.cohort,
            forecast_id=self.forecast,
            snapshot_id=self.yes,
            condition_id=self.conn.execute("SELECT condition_id FROM markets").fetchone()[0],
            outcome="YES",
            fills=(Fill(Decimal("0.40"), Decimal("10")), Fill(Decimal("0.41"), Decimal("5"))),
            fee=Decimal("0.1"),
            policy_hash=policy,
            rules_hash=self.rules_hash,
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class OpenTicketTests(TicketTestCase):
    def test_open_ticket_debits_decides_and_keeps_equity(self) -> None:
        ticket_id = open_ticket(self.conn, self.draft, LATER)
        row = self.conn.execute(
            "SELECT shares, cost_total, status FROM paper_tickets WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("15", "6.15", "OPEN"))
        self.assertEqual(available_cash(self.conn, self.cohort), Decimal("993.85"))
        self.assertEqual(open_cost(self.conn, self.cohort), Decimal("6.15"))
        self.assertEqual(equity(self.conn, self.cohort), Decimal("1000"))
        decision = self.conn.execute("SELECT kind, ticket_id FROM decisions").fetchone()
        self.assertEqual(tuple(decision), ("TRADED", ticket_id))

    def test_open_ticket_failure_leaves_no_partial_state(self) -> None:
        journal_before = self.count("journal")
        with (
            mock.patch("predict_agent.tickets.append_journal", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            open_ticket(self.conn, self.draft, LATER)
        self.assertEqual(self.count("paper_tickets"), 0)
        self.assertEqual(self.count("decisions"), 0)
        self.assertEqual(self.count("journal"), journal_before)
        self.assertEqual(available_cash(self.conn, self.cohort), Decimal("1000"))

    def test_snapshot_must_be_the_forecast_baseline_for_that_side(self) -> None:
        with self.assertRaisesRegex(LedgerError, "baseline"):
            open_ticket(self.conn, replace(self.draft, snapshot_id=self.no), LATER)
        stray = seed_snapshot(self.conn, "YES", LATER)
        with self.assertRaisesRegex(LedgerError, "baseline"):
            open_ticket(self.conn, replace(self.draft, snapshot_id=stray), LATER)

    def test_forecast_without_baseline_is_refused(self) -> None:
        other = "0x" + "7" * 64
        rules = seed_market(self.conn, other)
        bare = seed_entry_forecast(self.conn, self.cohort, rules, condition_id=other)
        draft = replace(
            self.draft, forecast_id=bare, condition_id=other, rules_hash=rules, snapshot_id=self.yes
        )
        with self.assertRaisesRegex(LedgerError, "baseline"):
            open_ticket(self.conn, draft, LATER)

    def test_second_ticket_on_same_forecast_is_refused(self) -> None:
        open_ticket(self.conn, self.draft, LATER)
        with self.assertRaisesRegex(LedgerError, "already decided"):
            open_ticket(self.conn, self.draft, LATER)
        self.assertEqual(self.count("paper_tickets"), 1)

    def test_rules_mismatch_and_closed_cohort_refused(self) -> None:
        with self.assertRaisesRegex(LedgerError, "rules"):
            open_ticket(self.conn, replace(self.draft, rules_hash="1" * 64), LATER)
        seed_cohort(self.conn, model_id="m2")
        with self.assertRaisesRegex(LedgerError, "not active"):
            open_ticket(self.conn, self.draft, LATER)

    def test_invalid_fills_rejected(self) -> None:
        bad = {
            "no fills": replace(self.draft, fills=()),
            "price 1": replace(self.draft, fills=(Fill(Decimal("1"), Decimal("1")),)),
            "zero shares": replace(self.draft, fills=(Fill(Decimal("0.5"), Decimal("0")),)),
            "negative fee": replace(self.draft, fee=Decimal("-0.01")),
            "nan price": replace(self.draft, fills=(Fill(Decimal("NaN"), Decimal("1")),)),
            "bad outcome": replace(self.draft, outcome="MAYBE"),
        }
        for label, draft in bad.items():
            with self.subTest(label), self.assertRaises(LedgerError):
                open_ticket(self.conn, draft, LATER)
        self.assertEqual(self.count("paper_tickets"), 0)

    def test_tickets_are_frozen_except_settlement(self) -> None:
        open_ticket(self.conn, self.draft, LATER)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE paper_tickets SET cost_total = '0.01'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM paper_tickets")


class SmallBankrollTests(TicketTestCase):
    bankroll = "5"

    def test_insufficient_cash_rolls_back(self) -> None:
        with self.assertRaises(InsufficientCash):
            open_ticket(self.conn, self.draft, LATER)
        self.assertEqual(self.count("paper_tickets"), 0)
        self.assertEqual(self.count("decisions"), 0)
        self.assertEqual(available_cash(self.conn, self.cohort), Decimal("5"))


class RefusalDecisionTests(TicketTestCase):
    def test_refusal_decision_is_recorded_once(self) -> None:
        record_refusal_decision(self.conn, self.forecast, "EDGE_BELOW_MIN", LATER)
        row = self.conn.execute("SELECT kind, reason, ticket_id FROM decisions").fetchone()
        self.assertEqual(tuple(row), ("REFUSED", "EDGE_BELOW_MIN", None))
        with self.assertRaisesRegex(LedgerError, "already decided"):
            record_refusal_decision(self.conn, self.forecast, "AGAIN", LATER)
        with self.assertRaisesRegex(LedgerError, "already decided"):
            open_ticket(self.conn, self.draft, LATER)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_tickets -v`
Expected: ERROR `No module named 'predict_agent.tickets'`.

- [ ] **Step 3: Implement**

`src/predict_agent/tickets.py`:

```python
"""Paper tickets and entry decisions. Opening a ticket writes the ticket, its cash debit,
its TRADED decision and the journal entry in one transaction (spec §4)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .cash import LedgerError, append_cash_entry, available_cash, money_text, parse_money
from .db import append_journal, transaction
from .util import canonical_json, isoformat, sha256_json

OUTCOMES = ("YES", "NO")


@dataclass(frozen=True)
class Fill:
    price: Decimal
    shares: Decimal


@dataclass(frozen=True)
class TicketDraft:
    cohort_id: str
    forecast_id: int
    snapshot_id: int
    condition_id: str
    outcome: str
    fills: tuple[Fill, ...]
    fee: Decimal
    policy_hash: str
    rules_hash: str


def ticket_totals(draft: TicketDraft) -> tuple[Decimal, Decimal]:
    if draft.outcome not in OUTCOMES:
        raise LedgerError(f"outcome must be YES or NO, got {draft.outcome!r}")
    if not draft.fills:
        raise LedgerError("a ticket needs at least one fill")
    if not draft.fee.is_finite() or draft.fee < 0:
        raise LedgerError(f"invalid fee {draft.fee}")
    shares = Decimal("0")
    notional = Decimal("0")
    for fill in draft.fills:
        if not fill.price.is_finite() or not Decimal("0") < fill.price < Decimal("1"):
            raise LedgerError(f"fill price {fill.price} outside (0, 1)")
        if not fill.shares.is_finite() or fill.shares <= 0:
            raise LedgerError(f"fill shares {fill.shares} must be positive")
        shares += fill.shares
        notional += fill.price * fill.shares
    return shares, notional + draft.fee


def ticket_hash(values: Mapping[str, Any]) -> str:
    excluded = {"ticket_id", "status", "ticket_hash"}
    return sha256_json({key: value for key, value in values.items() if key not in excluded})


def _decided(conn: sqlite3.Connection, forecast_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM decisions WHERE forecast_id = ?", (forecast_id,))
    return row.fetchone() is not None


def open_ticket(conn: sqlite3.Connection, draft: TicketDraft, now: datetime) -> int:
    shares, cost_total = ticket_totals(draft)
    with transaction(conn):
        cohort = conn.execute(
            "SELECT status FROM cohorts WHERE cohort_id = ?", (draft.cohort_id,)
        ).fetchone()
        if cohort is None or cohort["status"] != "ACTIVE":
            raise LedgerError(f"cohort {draft.cohort_id[:12]} is not active")
        forecast = conn.execute(
            "SELECT * FROM forecasts WHERE forecast_id = ?", (draft.forecast_id,)
        ).fetchone()
        if (
            forecast is None
            or forecast["cohort_id"] != draft.cohort_id
            or forecast["condition_id"] != draft.condition_id
            or forecast["kind"] != "entry"
            or forecast["abstained"]
        ):
            raise LedgerError("ticket needs a non-abstained entry forecast of this cohort/market")
        if forecast["rules_hash"] != draft.rules_hash:
            raise LedgerError("ticket rules hash differs from the forecast's rules version")
        if _decided(conn, draft.forecast_id):
            raise LedgerError(f"forecast {draft.forecast_id} is already decided")
        baseline = conn.execute(
            "SELECT yes_snapshot_id, no_snapshot_id FROM forecast_baselines "
            "WHERE forecast_id = ? AND reason IS NULL",
            (draft.forecast_id,),
        ).fetchone()
        side_snapshot = None
        if baseline is not None:
            column = "yes_snapshot_id" if draft.outcome == "YES" else "no_snapshot_id"
            side_snapshot = baseline[column]
        if side_snapshot != draft.snapshot_id:
            raise LedgerError(
                "ticket snapshot must be the forecast's baseline snapshot for the bought side"
            )
        policy = conn.execute(
            "SELECT kind FROM artifacts WHERE artifact_hash = ?", (draft.policy_hash,)
        ).fetchone()
        if policy is None or policy["kind"] != "policy":
            raise LedgerError(f"policy artifact {draft.policy_hash[:12]} is not stored")
        values: dict[str, Any] = {
            "cohort_id": draft.cohort_id,
            "forecast_id": draft.forecast_id,
            "snapshot_id": draft.snapshot_id,
            "condition_id": draft.condition_id,
            "outcome": draft.outcome,
            "direction": "BUY",
            "shares": money_text(shares),
            "fills_json": canonical_json(
                [[money_text(f.price), money_text(f.shares)] for f in draft.fills]
            ),
            "fee": money_text(draft.fee),
            "cost_total": money_text(cost_total),
            "policy_hash": draft.policy_hash,
            "rules_hash": draft.rules_hash,
            "created_at": isoformat(now),
        }
        values["ticket_hash"] = ticket_hash(values)
        values["status"] = "OPEN"
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        try:
            cursor = conn.execute(
                f"INSERT INTO paper_tickets ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        except sqlite3.IntegrityError as error:
            raise LedgerError(f"market already traded in this cohort: {error}") from None
        if cursor.lastrowid is None:
            raise LedgerError("ticket insert returned no row id")
        ticket_id = cursor.lastrowid
        append_cash_entry(conn, draft.cohort_id, "DEBIT", cost_total, ticket_id, now)
        conn.execute(
            "INSERT INTO decisions (forecast_id, cohort_id, condition_id, kind, reason, "
            "ticket_id, decided_at) VALUES (?, ?, ?, 'TRADED', NULL, ?, ?)",
            (draft.forecast_id, draft.cohort_id, draft.condition_id, ticket_id, isoformat(now)),
        )
        append_journal(
            conn,
            "TICKET_OPENED",
            {
                "ticket_id": ticket_id,
                "cohort_id": draft.cohort_id,
                "condition_id": draft.condition_id,
                "outcome": draft.outcome,
                "cost_total": values["cost_total"],
                "ticket_hash": values["ticket_hash"],
            },
            now,
        )
    return ticket_id


def record_refusal_decision(
    conn: sqlite3.Connection, forecast_id: int, reason: str, now: datetime
) -> None:
    if not reason:
        raise LedgerError("a refusal decision needs a reason")
    with transaction(conn):
        forecast = conn.execute(
            "SELECT cohort_id, condition_id, kind FROM forecasts WHERE forecast_id = ?",
            (forecast_id,),
        ).fetchone()
        if forecast is None or forecast["kind"] != "entry":
            raise LedgerError(f"forecast {forecast_id} is not an entry forecast")
        if _decided(conn, forecast_id):
            raise LedgerError(f"forecast {forecast_id} is already decided")
        conn.execute(
            "INSERT INTO decisions (forecast_id, cohort_id, condition_id, kind, reason, "
            "ticket_id, decided_at) VALUES (?, ?, ?, 'REFUSED', ?, NULL, ?)",
            (forecast_id, forecast["cohort_id"], forecast["condition_id"], reason, isoformat(now)),
        )
        append_journal(
            conn, "DECISION_REFUSED", {"forecast_id": forecast_id, "reason": reason}, now
        )


def open_cost(conn: sqlite3.Connection, cohort_id: str) -> Decimal:
    total = Decimal("0")
    for row in conn.execute(
        "SELECT cost_total FROM paper_tickets WHERE cohort_id = ? AND status = 'OPEN'",
        (cohort_id,),
    ):
        total += parse_money(row["cost_total"])
    return total


def equity(conn: sqlite3.Connection, cohort_id: str) -> Decimal:
    return available_cash(conn, cohort_id) + open_cost(conn, cohort_id)
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_tickets -v` → all OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/tickets.py tests/predict/test_ledger_tickets.py
git commit -m "feat(predict): atomic paper tickets, decisions and equity"
```

---

### Task 6: Settlement

**Files:**
- Create: `src/predict_agent/settlement.py`, `tests/predict/test_ledger_settlement.py`

**Interfaces:**
- Consumes: `cash.append_cash_entry`, `cash.money_text`, `cash.parse_money`; `db.transaction`, `db.append_journal`; `tickets.*`, `forecasts.*` and fixtures (tests).
- Produces:
  - `settlement.SettlementSummary` frozen dataclass: `settled: int`, `awaiting_resolution: int`, `awaiting_confirmation: int`, `unsettleable: int`
  - `settlement.payout_per_share(held: str, outcome: str) -> Decimal` (YES/NO → 1 or 0; HALF → 0.5)
  - `settlement.settle_open_tickets(conn, now) -> SettlementSummary` — for every `OPEN` ticket in any cohort, reads the market's **latest** resolution observation; settles only `resolved` + `CONFIRMED` + `YES|NO|HALF`, writing settlement row, `SETTLED` status, `CREDIT` (possibly 0) and journal `TICKET_SETTLED` in one transaction per ticket

- [ ] **Step 1: Write the failing tests**

`tests/predict/test_ledger_settlement.py`:

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from predict_agent.cash import available_cash
from predict_agent.db import connect
from predict_agent.forecasts import ForecastError, attach_baseline
from predict_agent.settlement import payout_per_share, settle_open_tickets
from predict_agent.tickets import Fill, TicketDraft, open_ticket
from tests.predict.fixtures import CONDITION_ID, NOW
from tests.predict.ledger_fixtures import (
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_observation,
    seed_snapshot,
)

LATER = NOW + timedelta(seconds=1)
SETTLE_AT = NOW + timedelta(days=30)


class SettlementTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.rules_hash = seed_market(self.conn)
        self.cohort = seed_cohort(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def open_test_ticket(self, outcome: str) -> int:
        """Buys 10 shares of `outcome` at 0.40 with no fee: cost 4."""
        forecast = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        yes = seed_snapshot(self.conn, "YES", LATER)
        no = seed_snapshot(self.conn, "NO", LATER)
        attach_baseline(self.conn, forecast, yes, no, LATER)
        policy = self.conn.execute(
            "SELECT artifact_hash FROM artifacts WHERE kind = 'policy'"
        ).fetchone()[0]
        draft = TicketDraft(
            cohort_id=self.cohort,
            forecast_id=forecast,
            snapshot_id=yes if outcome == "YES" else no,
            condition_id=CONDITION_ID,
            outcome=outcome,
            fills=(Fill(Decimal("0.40"), Decimal("10")),),
            fee=Decimal("0"),
            policy_hash=policy,
            rules_hash=self.rules_hash,
        )
        return open_ticket(self.conn, draft, LATER)

    def settlement(self, ticket_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM settlements WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        assert row is not None
        return row


class PayoutTests(unittest.TestCase):
    def test_payout_table(self) -> None:
        cases = {
            ("YES", "YES"): "1",
            ("YES", "NO"): "0",
            ("NO", "NO"): "1",
            ("NO", "YES"): "0",
            ("YES", "HALF"): "0.5",
            ("NO", "HALF"): "0.5",
        }
        for (held, outcome), expected in cases.items():
            with self.subTest(held=held, outcome=outcome):
                self.assertEqual(payout_per_share(held, outcome), Decimal(expected))
        with self.assertRaises(ValueError):
            payout_per_share("YES", "UNKNOWN")


class SettleTests(SettlementTestCase):
    def test_winning_yes_ticket_settles_at_one(self) -> None:
        ticket = self.open_test_ticket("YES")
        seed_observation(self.conn, "YES")
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual(summary.settled, 1)
        row = self.settlement(ticket)
        self.assertEqual(Decimal(row["payout"]), Decimal("10"))
        self.assertEqual(Decimal(row["net_pnl"]), Decimal("6"))
        self.assertEqual(row["outcome"], "YES")
        self.assertEqual(available_cash(self.conn, self.cohort), Decimal("1006"))
        status = self.conn.execute("SELECT status FROM paper_tickets").fetchone()[0]
        self.assertEqual(status, "SETTLED")

    def test_losing_ticket_settles_with_zero_credit(self) -> None:
        ticket = self.open_test_ticket("NO")
        seed_observation(self.conn, "YES")
        settle_open_tickets(self.conn, SETTLE_AT)
        row = self.settlement(ticket)
        self.assertEqual(Decimal(row["payout"]), Decimal("0"))
        self.assertEqual(Decimal(row["net_pnl"]), Decimal("-4"))
        self.assertEqual(available_cash(self.conn, self.cohort), Decimal("996"))

    def test_half_pays_half_on_either_side(self) -> None:
        ticket = self.open_test_ticket("NO")
        seed_observation(self.conn, "HALF")
        settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual(Decimal(self.settlement(ticket)["net_pnl"]), Decimal("1"))
        self.assertEqual(available_cash(self.conn, self.cohort), Decimal("1001"))

    def test_unresolved_unchecked_and_mismatch_wait(self) -> None:
        self.open_test_ticket("YES")
        cases = (
            (
                dict(outcome=None, status="posed", cross_check="NOT_APPLICABLE"),
                "awaiting_resolution",
            ),
            (dict(outcome="YES", cross_check="UNCHECKED"), "awaiting_confirmation"),
            (dict(outcome="UNKNOWN", cross_check="MISMATCH"), "unsettleable"),
        )
        for kwargs, field in cases:
            with self.subTest(field=field):
                seed_observation(self.conn, **kwargs)  # type: ignore[arg-type]
                summary = settle_open_tickets(self.conn, SETTLE_AT)
                self.assertEqual(getattr(summary, field), 1)
                self.assertEqual(summary.settled, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 0)

    def test_no_observation_awaits_resolution(self) -> None:
        self.open_test_ticket("YES")
        self.assertEqual(settle_open_tickets(self.conn, SETTLE_AT).awaiting_resolution, 1)

    def test_latest_observation_wins(self) -> None:
        self.open_test_ticket("YES")
        seed_observation(self.conn, "YES")
        seed_observation(self.conn, "YES", cross_check="UNCHECKED")
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual((summary.settled, summary.awaiting_confirmation), (0, 1))

    def test_settle_is_idempotent(self) -> None:
        self.open_test_ticket("YES")
        seed_observation(self.conn, "YES")
        settle_open_tickets(self.conn, SETTLE_AT)
        second = settle_open_tickets(self.conn, SETTLE_AT + timedelta(hours=1))
        self.assertEqual(second.settled, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 1)
        self.assertEqual(available_cash(self.conn, self.cohort), Decimal("1006"))

    def test_closed_cohort_still_settles_but_cannot_open(self) -> None:
        self.open_test_ticket("YES")
        seed_cohort(self.conn, model_id="m2")
        seed_observation(self.conn, "YES")
        self.assertEqual(settle_open_tickets(self.conn, SETTLE_AT).settled, 1)
        self.assertEqual(available_cash(self.conn, self.cohort), Decimal("1006"))
        other = "0x" + "8" * 64
        rules = seed_market(self.conn, other)
        with self.assertRaisesRegex(ForecastError, "not active"):
            seed_entry_forecast(self.conn, self.cohort, rules, condition_id=other)

    def test_settlement_rows_are_immutable(self) -> None:
        self.open_test_ticket("YES")
        seed_observation(self.conn, "YES")
        settle_open_tickets(self.conn, SETTLE_AT)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE settlements SET payout = '999'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE paper_tickets SET status = 'OPEN'")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_settlement -v`
Expected: ERROR `No module named 'predict_agent.settlement'`.

- [ ] **Step 3: Implement**

`src/predict_agent/settlement.py`:

```python
"""Settle open paper tickets from official resolutions (spec §4). Only a latest observation
that is resolved, CONFIRMED against Gamma and maps to YES/NO/HALF settles; everything else
waits or is surfaced. There is no voiding."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .cash import append_cash_entry, money_text, parse_money
from .db import append_journal, transaction
from .util import isoformat


@dataclass(frozen=True)
class SettlementSummary:
    settled: int
    awaiting_resolution: int
    awaiting_confirmation: int
    unsettleable: int


def payout_per_share(held: str, outcome: str) -> Decimal:
    if outcome == "HALF":
        return Decimal("0.5")
    if held in ("YES", "NO") and outcome in ("YES", "NO"):
        return Decimal("1") if held == outcome else Decimal("0")
    raise ValueError(f"no payout for holding {held!r} at outcome {outcome!r}")


def settle_open_tickets(conn: sqlite3.Connection, now: datetime) -> SettlementSummary:
    counts = {"settled": 0, "awaiting_resolution": 0, "awaiting_confirmation": 0,
              "unsettleable": 0}
    tickets = conn.execute(
        "SELECT * FROM paper_tickets WHERE status = 'OPEN' ORDER BY ticket_id"
    ).fetchall()
    for ticket in tickets:
        observation = conn.execute(
            "SELECT * FROM resolution_observations WHERE condition_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (ticket["condition_id"],),
        ).fetchone()
        if observation is None or observation["status"] != "resolved":
            counts["awaiting_resolution"] += 1
            continue
        if observation["cross_check"] == "UNCHECKED":
            counts["awaiting_confirmation"] += 1
            continue
        if observation["cross_check"] != "CONFIRMED" or observation["outcome"] not in (
            "YES",
            "NO",
            "HALF",
        ):
            counts["unsettleable"] += 1
            continue
        per_share = payout_per_share(ticket["outcome"], observation["outcome"])
        payout = parse_money(ticket["shares"]) * per_share
        net_pnl = payout - parse_money(ticket["cost_total"])
        with transaction(conn):
            current = conn.execute(
                "SELECT status FROM paper_tickets WHERE ticket_id = ?", (ticket["ticket_id"],)
            ).fetchone()
            if current["status"] != "OPEN":
                continue
            market = conn.execute(
                "SELECT current_rules_hash FROM markets WHERE condition_id = ?",
                (ticket["condition_id"],),
            ).fetchone()
            rules_hash = market["current_rules_hash"] if market else ticket["rules_hash"]
            conn.execute(
                "INSERT INTO settlements (ticket_id, observation_id, outcome, payout_per_share, "
                "payout, net_pnl, rules_hash_at_settlement, settled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticket["ticket_id"],
                    observation["id"],
                    observation["outcome"],
                    money_text(per_share),
                    money_text(payout),
                    money_text(net_pnl),
                    rules_hash,
                    isoformat(now),
                ),
            )
            conn.execute(
                "UPDATE paper_tickets SET status = 'SETTLED' WHERE ticket_id = ?",
                (ticket["ticket_id"],),
            )
            append_cash_entry(
                conn, ticket["cohort_id"], "CREDIT", payout, ticket["ticket_id"], now
            )
            append_journal(
                conn,
                "TICKET_SETTLED",
                {
                    "ticket_id": ticket["ticket_id"],
                    "observation_id": observation["id"],
                    "outcome": observation["outcome"],
                    "payout": money_text(payout),
                    "net_pnl": money_text(net_pnl),
                },
                now,
            )
            counts["settled"] += 1
    return SettlementSummary(**counts)
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_settlement -v` → all OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/settlement.py tests/predict/test_ledger_settlement.py
git commit -m "feat(predict): settle open tickets from confirmed resolutions"
```

---

### Task 7: Ledger invariants, `settle` command, `doctor` checks

**Files:**
- Create: `src/predict_agent/invariants.py`, `tests/predict/test_ledger_invariants.py`
- Modify: `src/predict_agent/cli.py`, `CLAUDE.md`

**Interfaces:**
- Consumes: `cash.available_cash`, `cash.cash_entry_hash`, `cash.parse_money`; `tickets.ticket_hash`; `settlement.settle_open_tickets`; `db.GENESIS_HASH`, `db.verify_journal`.
- Produces:
  - `invariants.verify_ledger(conn) -> list[str]` — empty when sound; one line per problem: negative available cash, broken per-cohort cash chain, ticket hash mismatch, `SETTLED` without settlement (or the reverse), ticket debit ≠ cost, settlement credit ≠ payout
  - CLI: `predict-agent settle` (offline; prints the `SettlementSummary`; exit 0); `doctor` exits 3 when the journal is broken **or** `verify_ledger` reports problems

- [ ] **Step 1: Write the failing tests**

`tests/predict/test_ledger_invariants.py`:

```python
from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from predict_agent.cli import main
from predict_agent.db import connect
from predict_agent.forecasts import attach_baseline
from predict_agent.invariants import verify_ledger
from predict_agent.settlement import settle_open_tickets
from predict_agent.tickets import Fill, TicketDraft, open_ticket
from tests.predict.fixtures import CONDITION_ID, NOW
from tests.predict.ledger_fixtures import (
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_observation,
    seed_snapshot,
)

REPO = Path(__file__).resolve().parents[2]
LATER = NOW + timedelta(seconds=1)


class InvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        shutil.copy(
            REPO / "config" / "predict-policy.example.json",
            self.root / "config" / "predict-policy.json",
        )
        self.conn = connect(self.root / "data" / "predict.sqlite3")
        rules_hash = seed_market(self.conn)
        self.cohort = seed_cohort(self.conn)
        forecast = seed_entry_forecast(self.conn, self.cohort, rules_hash)
        yes = seed_snapshot(self.conn, "YES", LATER)
        no = seed_snapshot(self.conn, "NO", LATER)
        attach_baseline(self.conn, forecast, yes, no, LATER)
        policy = self.conn.execute(
            "SELECT artifact_hash FROM artifacts WHERE kind = 'policy'"
        ).fetchone()[0]
        open_ticket(
            self.conn,
            TicketDraft(
                cohort_id=self.cohort,
                forecast_id=forecast,
                snapshot_id=yes,
                condition_id=CONDITION_ID,
                outcome="YES",
                fills=(Fill(Decimal("0.40"), Decimal("10")),),
                fee=Decimal("0.02"),
                policy_hash=policy,
                rules_hash=rules_hash,
            ),
            LATER,
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(argv, root=self.root, now_fn=lambda: NOW + timedelta(days=30))
        return code, out.getvalue()

    def test_sound_ledger_has_no_problems_before_and_after_settlement(self) -> None:
        self.assertEqual(verify_ledger(self.conn), [])
        seed_observation(self.conn, "YES")
        settle_open_tickets(self.conn, NOW + timedelta(days=30))
        self.assertEqual(verify_ledger(self.conn), [])

    def test_tampered_cash_entry_breaks_chain(self) -> None:
        self.conn.execute("DROP TRIGGER cash_no_update")
        self.conn.execute("UPDATE cash_ledger SET amount = '5000' WHERE entry_type = 'FUNDING'")
        problems = verify_ledger(self.conn)
        self.assertTrue(any("cash chain" in p for p in problems), problems)

    def test_tampered_ticket_is_detected(self) -> None:
        self.conn.execute("DROP TRIGGER tickets_open_to_settled_only")
        self.conn.execute("UPDATE paper_tickets SET cost_total = '0.01'")
        problems = verify_ledger(self.conn)
        self.assertTrue(any("ticket hash" in p for p in problems), problems)
        self.assertTrue(any("debit" in p for p in problems), problems)

    def test_settled_without_settlement_is_detected(self) -> None:
        self.conn.execute("DROP TRIGGER tickets_open_to_settled_only")
        self.conn.execute("UPDATE paper_tickets SET status = 'SETTLED'")
        problems = verify_ledger(self.conn)
        self.assertTrue(any("without a settlement" in p for p in problems), problems)

    def test_settle_command_runs_offline(self) -> None:
        seed_observation(self.conn, "YES")
        code, output = self.run_cli(["settle"])
        self.assertEqual(code, 0, output)
        self.assertIn("settled 1", output)

    def test_doctor_fails_on_ledger_problem(self) -> None:
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)
        self.conn.execute("DROP TRIGGER cash_no_update")
        self.conn.execute("UPDATE cash_ledger SET amount = '1' WHERE entry_type = 'DEBIT'")
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 3)
        self.assertIn("cash chain", output)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_invariants -v`
Expected: ERROR `No module named 'predict_agent.invariants'`.

- [ ] **Step 3: Implement**

`src/predict_agent/invariants.py`:

```python
"""Ledger invariant checks used by `predict-agent doctor`. Triggers make tampering hard;
these checks make it visible if it happens anyway (e.g. a trigger was dropped)."""

from __future__ import annotations

import sqlite3

from .cash import available_cash, cash_entry_hash, parse_money
from .db import GENESIS_HASH
from .tickets import ticket_hash


def _cash_problems(conn: sqlite3.Connection, cohort_id: str) -> list[str]:
    problems: list[str] = []
    prev_hash = GENESIS_HASH
    for row in conn.execute(
        "SELECT * FROM cash_ledger WHERE cohort_id = ? ORDER BY entry_id", (cohort_id,)
    ):
        expected = cash_entry_hash(
            cohort_id, row["entry_type"], row["amount"], row["ticket_id"], row["at"], prev_hash
        )
        if expected != row["entry_hash"]:
            entry = row["entry_id"]
            problems.append(f"cohort {cohort_id[:12]}: cash chain broken at entry {entry}")
            break
        prev_hash = row["entry_hash"]
    if available_cash(conn, cohort_id) < 0:
        problems.append(f"cohort {cohort_id[:12]}: available cash is negative")
    return problems


def _ticket_problems(conn: sqlite3.Connection, ticket: sqlite3.Row) -> list[str]:
    problems: list[str] = []
    label = f"ticket {ticket['ticket_id']}"
    if ticket_hash(dict(ticket)) != ticket["ticket_hash"]:
        problems.append(f"{label}: ticket hash mismatch")
    debits = conn.execute(
        "SELECT amount FROM cash_ledger WHERE ticket_id = ? AND entry_type = 'DEBIT'",
        (ticket["ticket_id"],),
    ).fetchall()
    if [parse_money(d["amount"]) for d in debits] != [parse_money(ticket["cost_total"])]:
        problems.append(f"{label}: debit does not match cost_total")
    settlement = conn.execute(
        "SELECT payout FROM settlements WHERE ticket_id = ?", (ticket["ticket_id"],)
    ).fetchone()
    if ticket["status"] == "SETTLED" and settlement is None:
        problems.append(f"{label}: SETTLED without a settlement row")
    if ticket["status"] == "OPEN" and settlement is not None:
        problems.append(f"{label}: OPEN but has a settlement row")
    if settlement is not None:
        credits = conn.execute(
            "SELECT amount FROM cash_ledger WHERE ticket_id = ? AND entry_type = 'CREDIT'",
            (ticket["ticket_id"],),
        ).fetchall()
        if [parse_money(c["amount"]) for c in credits] != [parse_money(settlement["payout"])]:
            problems.append(f"{label}: credit does not match settlement payout")
    return problems


def verify_ledger(conn: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    for row in conn.execute("SELECT cohort_id FROM cohorts ORDER BY started_at").fetchall():
        problems += _cash_problems(conn, row["cohort_id"])
    for ticket in conn.execute("SELECT * FROM paper_tickets ORDER BY ticket_id").fetchall():
        problems += _ticket_problems(conn, ticket)
    return problems
```

Modify `src/predict_agent/cli.py`:

1. Imports — add:

```python
from .invariants import verify_ledger
from .settlement import settle_open_tickets
```

2. In `_parser`, after the `resolve` sub-parser line, add:

```python
    sub.add_parser("settle", help="settle open paper tickets from stored resolutions (offline)")
```

3. Replace the `doctor` branch in `main` with:

```python
    if args.command == "doctor":
        conn = connect(settings.database_path)
        try:
            journal_ok = verify_journal(conn)
            problems = verify_ledger(conn)
        finally:
            conn.close()
        print(f"policy {policy_hash[:12]} ok; journal chain {'ok' if journal_ok else 'BROKEN'}")
        for problem in problems:
            print(f"ledger: {problem}")
        print(f"ledger: {'ok' if not problems else f'{len(problems)} problem(s)'}")
        return 0 if journal_ok and not problems else 3
    if args.command == "settle":
        conn = connect(settings.database_path)
        try:
            summary = settle_open_tickets(conn, now_fn())
        finally:
            conn.close()
        print(
            f"settled {summary.settled}; awaiting resolution {summary.awaiting_resolution}; "
            f"awaiting confirmation {summary.awaiting_confirmation}; "
            f"unsettleable {summary.unsettleable}"
        )
        return 0
```

Modify `CLAUDE.md` — append to the `predict_agent` section:

```markdown
- Ledger (Plan 2): money is Decimal text summed in Python (never SQL `SUM`); compare stored timestamps parsed, not as strings; tickets only move OPEN → SETTLED, cohorts only ACTIVE → CLOSED, everything else is append-only (triggers). Write ledger state only through `cohorts`/`forecasts`/`tickets`/`settlement` functions — each commits with its journal entry. `predict-agent settle` is offline; `doctor` also runs `verify_ledger`.
```

- [ ] **Step 4: Run to verify pass, then the full suite**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_invariants -v` → all OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK.

- [ ] **Step 5: Lint, type-check, CI checks, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`

```bash
git add src/predict_agent/invariants.py src/predict_agent/cli.py tests/predict/test_ledger_invariants.py CLAUDE.md
git commit -m "feat(predict): ledger invariants, settle command, doctor ledger checks"
```

---

## Self-Review Record

1. **Spec coverage (build-order item 2):** cash ledger with never-negative cash (Task 2), immutable artifacts (Task 2), cohorts with independent funding/closing (Task 3), forecasts with one entry per market, baselines bound to post-forecast snapshots, `NO_TIMELY_BASELINE`, resume (Task 4), tickets with per-level fills, fee, cost, decisions, equity on cost basis (Task 5), settlement incl. HALF and no voiding (Task 6), invariant checks and ops commands (Task 7). Deferred by design: policy sizing/caps and book walking (Plan 3), research/forecast generation (Plan 4), scoring report incl. `rules_changed_since_first_forecast` (Plan 5 — `settlements.rules_hash_at_settlement` and `forecasts.rules_hash` provide the inputs).
2. **Placeholder scan:** none.
3. **Type consistency:** `LedgerError` family shared via `cash`; `TicketDraft`/`Fill`, `ForecastRecord`, `CohortIdentity`, `SettlementSummary` names match across tasks and fixtures.
4. **Review Focus:** five items listed at the top, each pinned to a named test.

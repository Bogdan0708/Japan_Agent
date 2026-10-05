# predict-agent Plan 2 — Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the paper-trading ledger of `predict_agent`: immutable artifacts, cohorts with pre-registered portfolio variants (primary + shadow) that each have independent funding, durable research attempts, forecast records with post-forecast baselines inside a frozen window, atomic ticket/cash/decision writes, settlement from confirmed resolutions chosen by observation time, crash-resume state, and ledger invariant checks that verify both hashes and accounting relationships — with no network, no Claude calls and no trading policy.

**Architecture:** New tables live in `ledger_schema.py` and are created by `db.connect` (schema v3). The version is checked before anything is written, and creation/migration run in one transaction, so a refused or failed migration leaves the file unchanged. A **cohort** is one research identity and owns the shared forecasts and baselines; each cohort has **portfolios** (`primary` plus shadows such as `shadow_mid`) with their own frozen policy, cash, tickets and decisions. Each concern gets one module — `artifacts.py`, `cash.py`, `cohorts.py`, `forecasts.py` (attempts, records, baselines, resume), `tickets.py`, `settlement.py`, `invariants.py` — and every state change commits together with its journal entry in one SQLite transaction. Plan 3 (policy + paper fills) and Plan 4 (research) call these functions; they never write the tables directly.

**Tech Stack:** Python ≥ 3.11 stdlib (`sqlite3`, `decimal`, `subprocess` for `git`), `unittest`, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-10-05-polymarket-paper-forecaster-design.md` (§4 data model and accounting, §5 equity and the shadow p_mid policy, §6 budget incl. failed attempts, §7 resume, §8 scoring population, §10 build order item 2).

**Builds on:** Plan 1 (`docs/superpowers/plans/2026-10-05-predict-agent-01-data-milestone.md`), branch `design/polymarket-paper-forecaster` at `f9c3cc6`.

**Revision 2 (2026-10-05):** rewritten after an independent three-agent review of revision 1 (`f9c3cc6`). Every code block below was extracted from a scratch worktree where the full suite (291 tests) passed on Python 3.11 and 3.14 with ruff, strict mypy, `bash -n` and `git diff --check` clean; each review regression test was mutation-checked (removing its fix makes it fail); and copies of the saved Plan 1 smoke databases (smoke4, smoke5) migrated v2 → v3 with every row kept, `integrity_check` ok, `foreign_key_check` empty, journal and ledger checks clean. See **Review response** for what changed.

## Global Constraints

- Core code is stdlib-only; `predict_agent` never imports `japan_agent`; no wallet, key, signing or non-GET HTTP code.
- Money is `Decimal`, stored as exact text via `format(value, "f")`; **never `SUM()` money in SQL** (SQLite converts TEXT to float) — sum in Python.
- Timestamps are timezone-aware UTC; compare stored timestamps by parsing them (`util.parse_datetime`), never as strings (`"…00Z"` vs `"…00.123Z"` sorts wrong).
- Every state change and its `journal` entry commit in the same `db.transaction` (`BEGIN IMMEDIATE`). Anything a write depends on is read **inside** that transaction.
- Every cash entry is backed by the record it accounts for: exactly one `FUNDING` per portfolio, first, equal to its starting bankroll; a `DEBIT` equals its own `OPEN` ticket's cost; a `CREDIT` equals its own ticket's settlement payout. Available cash can never go negative (spec §4).
- No voiding: a ticket only moves `OPEN → SETTLED`; P&L is never removed (spec §4).
- One entry forecast per market per cohort; one ticket per market per portfolio, ever (spec §4).
- A ticket's policy is its portfolio's frozen policy; a different policy requires a new cohort.
- A new cohort is opened by any change to the portfolio policies, prompt content, model id, research settings, scoring version, baseline window or generation; each portfolio has its own `FUNDING`; closed cohorts keep settling but take no new attempts, forecasts or tickets (spec §4).
- Baseline books must be **fetched** inside `[forecast time, forecast time + cohort baseline window]`; attaching timely books late is allowed, attaching late-fetched books is not (spec §7).
- Settle only from the governing resolution observation (latest by observation time, unambiguous) with `status = 'resolved'`, `cross_check = 'CONFIRMED'` and outcome in `YES/NO/HALF`; anything else waits and is reported with reason and age, never guessed.
- Every paid research call is a durable `research_attempts` row opened before the call; it ends `SUCCEEDED` (with its forecast) or `FAILED` (with its cost). Spend is never lost.
- Canonical test run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -v`; ruff line length 100; mypy strict on `src/predict_agent`.

## Spec deviations ruled in this plan

1. **`stage_runs` is replaced by state derived from durable rows** (`research_attempts`, `forecast_baselines`, `decisions`, `paper_tickets`). A bookkeeping table can disagree with the rows it describes after a crash; deriving resume state from the rows cannot. Failed paid attempts are **not** derivable after the fact, so they get their own durable table now (`research_attempts`, with cost and error); Plan 4 owns the recovery policy for attempts left `STARTED` by a crash (`forecasts.unfinished_attempts` lists them).
2. **Portfolios under cohorts.** Spec §4 says "each cohort is an independent virtual portfolio", while §5 requires a shadow p_mid policy "on the same post-forecast snapshots with its own virtual cash ledger". Both hold only if the research identity (cohort: shared forecasts and baselines) is separate from the policy variant (portfolio: own policy, cash, tickets, decisions). Plan 3 implements the shadow policy; its storage is settled here. A cohort with only `primary` behaves exactly like revision 1's cohort.
3. **The baseline window is part of the cohort identity** (`baseline_window_seconds`, spec default 30 min), so it cannot change mid-cohort.
4. **`CANCELLED` is not implemented.** The resolution API documents status/outcome selection but has no cancellation example; absence of an example does not prove every exceptional outcome is covered. An unmapped outcome never settles: the ticket stays `OPEN` and `settle` lists it as `UNSETTLEABLE` with its age. Cost if wrong: one mapping entry once a real example exists.
5. **Settlement uses only `CONFIRMED` observations.** 120/120 recent live resolutions were `CONFIRMED`, including 24 with million-scaled payout arrays; `HALF` is unverified by live data. An `UNCHECKED` resolution can wait **indefinitely**, not one poll cycle, so `settle` reports every waiting ticket with its reason and the time since the market was first observed resolved; Plan 5's report must surface the same.
6. **Reusing a closed cohort's exact identity is refused.** Restarting unchanged settings is done by increasing `generation`, which keeps every other setting truthful (no relabelling of `scoring_version`).
7. **Base rate is required for every non-abstained forecast**, because it is one of the three scored forecasters (spec §8). Abstentions may omit it. The ledger requires the body fields `evidence` (list), `rules_interpretation` (str) and `exposure_flags` (list) by type; Plan 4 validates their content (citations, URLs, exposure detection).
8. **Plan 1 change:** resolution observations gain `resolution_requested_at` (the v2 → v3 migration adds the column; `poll_resolutions` stamps it before the request). Settlement needs each observation's interval to order evidence; pre-v3 rows have no request time and are treated as instantaneous.

## Review response (revision 1 → revision 2)

| # | Finding | Change | Regression tests |
|---|---|---|---|
| 1 | Integrity checker certified unsupported cash and altered evidence | `append_cash_entry` checks each entry's backing record (Task 2); one-FUNDING partial unique index, `UNIQUE(ticket_id, entry_type)`, FUNDING⇔no-ticket `CHECK` (Task 1); `verify_ledger` checks triggers present, artifact hashes, cohort identity hashes and portfolios, forecast hashes and artifacts, attempt linkage, baseline windows, ticket backing, policy, decisions, single-FUNDING, running balance, entry ownership, credit⇔settlement, settlement evidence and payout arithmetic (Task 7) | `test_ledger_tickets.CashBackingTests`; `test_ledger_cash.FundingTests`; `test_ledger_invariants` "every hash valid, relationship broken" group, plus dropped trigger, altered prompt, altered probability |
| 2 | Ticket could use a different policy without a new cohort | `open_ticket` compares the draft's policy with the portfolio's frozen policy inside the transaction; `verify_ledger` checks it independently | `test_policy_other_than_the_portfolios_is_refused_without_state`, `test_ticket_with_another_policy_and_valid_hash_is_detected` |
| 3 | Baseline deadline was advisory | Window frozen in the cohort; `attach_baseline` refuses books fetched after the deadline but accepts timely books attached late; `mark_no_timely_baseline` refuses while the window is open; `verify_ledger` re-checks windows | `test_books_fetched_after_the_window_are_refused`, `test_timely_books_may_be_attached_late`, `test_late_baseline_inserted_directly_is_detected` |
| 4 | Settlement could credit a superseded confirmation | Observations read and the governing one chosen inside the settlement transaction; governing = latest by `resolution_fetched_at`, ambiguous (waits) when contradictory evidence overlaps its request interval or ties it | `test_superseding_observation_committed_before_settlement_wins` (two connections), `test_delayed_poll_inserted_last_does_not_displace_newer_evidence`, `test_overlapping_polls_with_different_evidence_wait` |
| 5 | Refusing an unsupported database still modified it | `connect` checks the stored version first, then creates/migrates and bumps the version in one transaction; DDL is split with `sqlite3.complete_statement` because `executescript` commits | `test_unsupported_version_is_refused_without_changing_the_file`, `test_failed_migration_rolls_back_completely`, `test_tables_without_a_version_are_refused` |
| 6 | Schema could not support primary/shadow comparison | Spec deviation 2: `portfolios` table; tickets, decisions and cash keyed by portfolio; `NO_TIMELY_BASELINE` decided per portfolio | `test_shadow_portfolio_trades_the_same_forecast_with_its_own_cash`, `test_each_portfolio_is_funded_once_and_independently`, `test_late_resume_marks_no_timely_baseline_in_every_portfolio` |
| — | Base rate optional; empty bodies accepted | Spec deviation 7 | `test_validation_rejects_bad_records` ("missing base rate", "empty body") |
| — | Failed paid attempts not recorded | Spec deviation 1: `research_attempts` | `AttemptTests` |
| — | UNCHECKED may wait indefinitely | Spec deviation 5: pending reason + age in `settle` | `test_waiting_tickets_report_reason_and_age`, `test_settle_command_runs_offline_and_reports_pending_age` |
| — | Restart advice mislabelled the experiment | Spec deviation 6: `generation` | `test_reopening_a_closed_identity_is_refused_but_a_new_generation_opens` |
| — | CANCELLED coverage overstated | Spec deviation 4 reworded | `test_waiting_tickets_report_reason_and_age` (UNSETTLEABLE) |

## Review Focus

1. **Every hash valid, accounting relationship broken** — a second FUNDING, a CREDIT against an open ticket, an entry on another portfolio's ticket, a ticket re-hashed under another policy, wrong payout arithmetic, a settlement from an unconfirmed observation and a late baseline must each be reported (Task 7, "relationship broken" tests).
2. **Settlement race** — a newer contradictory observation committed by another connection just before settlement takes its lock must prevent the credit (Task 6 `test_superseding_observation_committed_before_settlement_wins`).
3. **Baseline deadline** — books fetched at +31 minutes are refused even though collection started in time; books fetched at +29 and attached at +45 are accepted (Task 4).
4. **Refused or failed migration** — the database's `sqlite_master` entries and stored version are unchanged (Task 1).
5. **Money summed by SQLite** — three `0.1` debits leave exactly `0.7` of `1.0` (Task 5 `test_cash_sums_are_exact_decimal`).
6. **Crash in the middle of opening a ticket** — a failure after the ticket insert leaves no ticket, debit, decision or journal entry (Task 5 `test_open_ticket_failure_leaves_no_partial_state`).
7. **Running `settle` twice** credits nothing the second time; a **closed cohort** still settles and still refuses new forecasts (Task 6).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/predict_agent/ledger_schema.py` | DDL for all ledger tables, constraints and immutability triggers |
| `src/predict_agent/db.py` (modify) | foreign keys on; version check before mutation; atomic create/migrate to v3 |
| `src/predict_agent/collect.py` (modify) | stamp `resolution_requested_at` on resolution observations |
| `src/predict_agent/artifacts.py` | content-addressed immutable artifacts |
| `src/predict_agent/cash.py` | money helpers, backed hash-chained cash entries, available cash |
| `src/predict_agent/cohorts.py` | cohort identity, portfolios, opening/closing, `code_version` |
| `src/predict_agent/forecasts.py` | research attempts, forecast validation/recording, baselines, resume state |
| `src/predict_agent/tickets.py` | ticket opening, decisions, open cost, equity (per portfolio) |
| `src/predict_agent/settlement.py` | governing observation, settle open tickets, pending reasons |
| `src/predict_agent/invariants.py` | hash and relationship checks for `doctor` |
| `src/predict_agent/cli.py` (modify) | `settle` command; `doctor` runs ledger checks |
| `tests/predict/ledger_fixtures.py` | seed helpers for markets, snapshots, observations, cohorts, forecasts, drafts |
| `tests/predict/test_ledger_*.py` | one test module per source module |

---

### Task 1: Ledger schema and atomic v2 → v3 migration

**Files:**
- Create: `src/predict_agent/ledger_schema.py`, `tests/predict/test_ledger_schema.py`
- Modify: `src/predict_agent/db.py`, `src/predict_agent/collect.py`, `tests/predict/test_collect.py`

**Interfaces:**
- Consumes: `db.SCHEMA`, `db.transaction` (Plan 1).
- Produces: `ledger_schema.LEDGER_SCHEMA: str`; `db.SCHEMA_VERSION = 3`; `db.MIGRATIONS: dict[int, tuple[str, ...]]` (`{2: (ALTER … ADD COLUMN resolution_requested_at,)}`); `db.connect` sets `PRAGMA foreign_keys = ON`, checks the stored version **before** writing, and creates or migrates in one transaction; it refuses unknown versions, tables without a version, and an empty `schema_version`. Tables: `artifacts`, `cohorts`, `portfolios`, `research_attempts`, `forecasts`, `forecast_baselines`, `paper_tickets`, `decisions`, `cash_ledger`, `settlements`. `resolution_observations` gains `resolution_requested_at` (nullable; stamped by `poll_resolutions`) and a no-delete trigger.


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_ledger_schema.py` (create):

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from predict_agent.db import SCHEMA, SCHEMA_VERSION, connect
from predict_agent.ledger_schema import LEDGER_SCHEMA

LEDGER_TABLES = {
    "artifacts",
    "cohorts",
    "portfolios",
    "research_attempts",
    "forecasts",
    "forecast_baselines",
    "paper_tickets",
    "decisions",
    "cash_ledger",
    "settlements",
}

# Plan 1's v2 resolution_observations table, before resolution_requested_at existed.
V2_OBSERVATIONS = """
CREATE TABLE resolution_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    resolution_fetched_at TEXT NOT NULL,
    gamma_fetched_at TEXT,
    gamma_json TEXT,
    status TEXT NOT NULL,
    outcome TEXT,
    cross_check TEXT NOT NULL,
    was_disputed INTEGER NOT NULL,
    new_version_q INTEGER NOT NULL,
    raw_json TEXT NOT NULL
);
"""


class LedgerSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "data" / "predict.sqlite3"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_old_database(self, version: int) -> None:
        """A Plan 1 database at `version` with one run and one observation."""
        self.path.parent.mkdir(parents=True)
        raw = sqlite3.connect(self.path)
        try:
            raw.executescript(V2_OBSERVATIONS)
            raw.executescript(SCHEMA)
            raw.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            raw.execute(
                "INSERT INTO runs (run_id, command, started_at, policy_hash) "
                "VALUES ('r1', 'discover', '2026-10-05T12:00:00Z', 'p')"
            )
            raw.execute(
                "INSERT INTO resolution_observations (run_id, condition_id, "
                "resolution_fetched_at, status, cross_check, was_disputed, new_version_q, "
                "raw_json) VALUES ('r1', 'c', '2026-10-05T12:00:00Z', 'posed', "
                "'NOT_APPLICABLE', 0, 0, '{}')"
            )
            raw.commit()
        finally:
            raw.close()

    def schema_dump(self) -> list[tuple[str, str]]:
        raw = sqlite3.connect(self.path)
        try:
            rows = raw.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
            version = raw.execute("SELECT version FROM schema_version").fetchall()
        finally:
            raw.close()
        return [*rows, ("version", repr(version))]

    def tables(self, conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {row[0] for row in rows}

    def test_fresh_database_has_ledger_tables_and_version_3(self) -> None:
        conn = connect(self.path)
        try:
            self.assertEqual(SCHEMA_VERSION, 3)
            self.assertLessEqual(LEDGER_TABLES, self.tables(conn))
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(resolution_observations)")}
            self.assertIn("resolution_requested_at", columns)
        finally:
            conn.close()

    def test_v2_database_migrates_and_keeps_data(self) -> None:
        self.write_old_database(2)
        conn = connect(self.path)
        try:
            version = conn.execute("SELECT version FROM schema_version").fetchall()
            self.assertEqual([row[0] for row in version], [3])
            self.assertLessEqual(LEDGER_TABLES, self.tables(conn))
            self.assertEqual(conn.execute("SELECT run_id FROM runs").fetchone()[0], "r1")
            observation = conn.execute("SELECT * FROM resolution_observations").fetchone()
            self.assertEqual(observation["status"], "posed")
            self.assertIsNone(observation["resolution_requested_at"])
        finally:
            conn.close()
        connect(self.path).close()  # reconnecting at v3 changes nothing

    def test_unsupported_version_is_refused_without_changing_the_file(self) -> None:
        self.write_old_database(1)
        before = self.schema_dump()
        with self.assertRaisesRegex(RuntimeError, "unsupported predict schema version 1"):
            connect(self.path)
        self.assertEqual(self.schema_dump(), before)

    def test_failed_migration_rolls_back_completely(self) -> None:
        self.write_old_database(2)
        before = self.schema_dump()
        broken = LEDGER_SCHEMA + "INSERT INTO no_such_table VALUES (1);\n"
        with (
            mock.patch("predict_agent.db.LEDGER_SCHEMA", broken),
            self.assertRaises(sqlite3.OperationalError),
        ):
            connect(self.path)
        self.assertEqual(self.schema_dump(), before)

    def test_tables_without_a_version_are_refused(self) -> None:
        self.path.parent.mkdir(parents=True)
        raw = sqlite3.connect(self.path)
        try:
            raw.execute("CREATE TABLE stray (x INTEGER)")
            raw.commit()
        finally:
            raw.close()
        with self.assertRaisesRegex(RuntimeError, "no schema version"):
            connect(self.path)


if __name__ == "__main__":
    unittest.main()
```

Apply to `tests/predict/test_collect.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/tests/predict/test_collect.py b/tests/predict/test_collect.py
index 20e8167..415baa5 100644
--- a/tests/predict/test_collect.py
+++ b/tests/predict/test_collect.py
@@ -320,7 +320,7 @@ class CorrectionPassCollectTests(CollectTestCase):

     def test_resolution_observation_retains_gamma_evidence_and_times(self) -> None:
         self.discover_one(self.run_id)
-        times = iter([NOW, NOW + timedelta(seconds=5)])
+        times = iter([NOW - timedelta(seconds=2), NOW, NOW + timedelta(seconds=5)])
         closed = gamma_market(closed=True, outcomePrices='["0", "1"]')
         poll = RoutedOpener(
             {
@@ -331,6 +331,7 @@ class CorrectionPassCollectTests(CollectTestCase):
         poll_resolutions(self.conn, self.client(poll), self.run_id, lambda: next(times))
         row = self.conn.execute("SELECT * FROM resolution_observations").fetchone()
         self.assertEqual(json.loads(row["gamma_json"])["outcomePrices"], '["0", "1"]')
+        self.assertEqual(row["resolution_requested_at"], "2026-10-05T11:59:58Z")
         self.assertEqual(row["resolution_fetched_at"], "2026-10-05T12:00:00Z")
         self.assertEqual(row["gamma_fetched_at"], "2026-10-05T12:00:05Z")
         self.assertEqual(row["cross_check"], "CONFIRMED")
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_schema -v`
Expected: ERROR `No module named 'predict_agent.ledger_schema'`.

- [ ] **Step 3: Implement**

`src/predict_agent/ledger_schema.py` (create):

```python
"""Ledger tables (Plan 2). Every table is append-only except the documented transitions:
research_attempts STARTED -> SUCCEEDED|FAILED, paper_tickets OPEN -> SETTLED and
cohorts ACTIVE -> CLOSED.

A cohort is one research identity (prompt, model, research settings, scoring version,
baseline window) and owns the shared forecasts and baselines. Each cohort has one or more
portfolios (variant `primary` plus pre-registered shadows such as `shadow_mid`), each with
its own frozen policy, FUNDING entry, cash, tickets and decisions (spec §4, §5)."""

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
    baseline_window_seconds INTEGER NOT NULL CHECK (baseline_window_seconds > 0),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED')),
    started_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS portfolios (
    portfolio_id TEXT PRIMARY KEY,
    cohort_id TEXT NOT NULL REFERENCES cohorts (cohort_id),
    variant TEXT NOT NULL,
    policy_hash TEXT NOT NULL REFERENCES artifacts (artifact_hash),
    starting_bankroll TEXT NOT NULL,
    UNIQUE (cohort_id, variant)
);
CREATE TABLE IF NOT EXISTS research_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cohort_id TEXT NOT NULL REFERENCES cohorts (cohort_id),
    condition_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('entry', 'update')),
    started_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('STARTED', 'SUCCEEDED', 'FAILED')),
    finished_at TEXT,
    cost_usd TEXT,
    error TEXT,
    CHECK ((status = 'STARTED') = (finished_at IS NULL)),
    CHECK ((status = 'STARTED') = (cost_usd IS NULL)),
    CHECK ((status = 'FAILED') = (error IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL UNIQUE REFERENCES research_attempts (attempt_id),
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
    portfolio_id TEXT NOT NULL REFERENCES portfolios (portfolio_id),
    forecast_id INTEGER NOT NULL REFERENCES forecasts (forecast_id),
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
    UNIQUE (portfolio_id, condition_id),
    UNIQUE (portfolio_id, forecast_id)
);
CREATE TABLE IF NOT EXISTS decisions (
    portfolio_id TEXT NOT NULL REFERENCES portfolios (portfolio_id),
    forecast_id INTEGER NOT NULL REFERENCES forecasts (forecast_id),
    condition_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('TRADED', 'REFUSED', 'NO_TIMELY_BASELINE')),
    reason TEXT,
    ticket_id INTEGER REFERENCES paper_tickets (ticket_id),
    decided_at TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, forecast_id),
    CHECK ((kind = 'TRADED') = (ticket_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS cash_ledger (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT NOT NULL REFERENCES portfolios (portfolio_id),
    entry_type TEXT NOT NULL CHECK (entry_type IN ('FUNDING', 'DEBIT', 'CREDIT')),
    amount TEXT NOT NULL,
    ticket_id INTEGER REFERENCES paper_tickets (ticket_id),
    at TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE,
    CHECK ((entry_type = 'FUNDING') = (ticket_id IS NULL)),
    UNIQUE (ticket_id, entry_type)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_funding_per_portfolio
    ON cash_ledger (portfolio_id) WHERE entry_type = 'FUNDING';
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
CREATE TRIGGER IF NOT EXISTS portfolios_no_update BEFORE UPDATE ON portfolios
BEGIN SELECT RAISE(ABORT, 'portfolios are immutable'); END;
CREATE TRIGGER IF NOT EXISTS portfolios_no_delete BEFORE DELETE ON portfolios
BEGIN SELECT RAISE(ABORT, 'portfolios are immutable'); END;
CREATE TRIGGER IF NOT EXISTS attempts_no_delete BEFORE DELETE ON research_attempts
BEGIN SELECT RAISE(ABORT, 'research attempts are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS attempts_finish_once BEFORE UPDATE ON research_attempts
WHEN NOT (
    OLD.status = 'STARTED' AND NEW.status IN ('SUCCEEDED', 'FAILED')
    AND NEW.attempt_id IS OLD.attempt_id AND NEW.cohort_id IS OLD.cohort_id
    AND NEW.condition_id IS OLD.condition_id AND NEW.kind IS OLD.kind
    AND NEW.started_at IS OLD.started_at
)
BEGIN SELECT RAISE(ABORT, 'research attempts only move STARTED -> SUCCEEDED|FAILED'); END;
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
CREATE TRIGGER IF NOT EXISTS observations_no_delete BEFORE DELETE ON resolution_observations
BEGIN SELECT RAISE(ABORT, 'resolution observations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS tickets_no_delete BEFORE DELETE ON paper_tickets
BEGIN SELECT RAISE(ABORT, 'tickets are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS tickets_open_to_settled_only BEFORE UPDATE ON paper_tickets
WHEN NOT (
    OLD.status = 'OPEN' AND NEW.status = 'SETTLED'
    AND NEW.ticket_id IS OLD.ticket_id AND NEW.portfolio_id IS OLD.portfolio_id
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
    AND NEW.baseline_window_seconds IS OLD.baseline_window_seconds
    AND NEW.started_at IS OLD.started_at
)
BEGIN SELECT RAISE(ABORT, 'cohorts only move ACTIVE -> CLOSED'); END;
"""
```

Apply to `src/predict_agent/db.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/src/predict_agent/db.py b/src/predict_agent/db.py
index b40be9d..5c401b5 100644
--- a/src/predict_agent/db.py
+++ b/src/predict_agent/db.py
@@ -9,9 +9,15 @@ from datetime import datetime
 from pathlib import Path
 from typing import Any

+from .ledger_schema import LEDGER_SCHEMA
 from .util import canonical_json, isoformat, sha256_json

-SCHEMA_VERSION = 2
+SCHEMA_VERSION = 3
+# Statements that bring an older version's existing tables up to date. CREATE ... IF NOT
+# EXISTS in SCHEMA and LEDGER_SCHEMA adds the new tables.
+MIGRATIONS: dict[int, tuple[str, ...]] = {
+    2: ("ALTER TABLE resolution_observations ADD COLUMN resolution_requested_at TEXT",),
+}
 GENESIS_HASH = "0" * 64

 SCHEMA = """
@@ -87,7 +93,8 @@ CREATE TABLE IF NOT EXISTS resolution_observations (
         CHECK (cross_check IN ('CONFIRMED', 'UNCHECKED', 'MISMATCH', 'NOT_APPLICABLE')),
     was_disputed INTEGER NOT NULL,
     new_version_q INTEGER NOT NULL,
-    raw_json TEXT NOT NULL
+    raw_json TEXT NOT NULL,
+    resolution_requested_at TEXT
 );
 CREATE TABLE IF NOT EXISTS refusals (
     id INTEGER PRIMARY KEY AUTOINCREMENT,
@@ -127,7 +134,40 @@ BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
 """


+def _statements(script: str) -> list[str]:
+    """Split a DDL script into statements; trigger bodies contain inner semicolons."""
+    statements: list[str] = []
+    buffer = ""
+    for line in script.splitlines(keepends=True):
+        buffer += line
+        if sqlite3.complete_statement(buffer):
+            statements.append(buffer.strip())
+            buffer = ""
+    if buffer.strip():
+        raise RuntimeError("schema script ends with an incomplete statement")
+    return statements
+
+
+def _stored_version(conn: sqlite3.Connection) -> int | None:
+    table = conn.execute(
+        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
+    ).fetchone()
+    if table is None:
+        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone():
+            raise RuntimeError("predict database has tables but no schema version")
+        return None
+    versions = [row["version"] for row in conn.execute("SELECT version FROM schema_version")]
+    if not versions:
+        raise RuntimeError("predict database has an empty schema_version table")
+    if len(versions) != 1:
+        raise RuntimeError(f"unsupported predict schema version {versions}")
+    version: int = versions[0]
+    return version
+
+
 def connect(path: Path) -> sqlite3.Connection:
+    """Open the database. Creation and migration run in one transaction after the stored
+    version is checked, so a refused or failed migration leaves the file unchanged."""
     path.parent.mkdir(parents=True, exist_ok=True)
     os.chmod(path.parent, 0o700)
     if not path.exists():
@@ -136,12 +176,22 @@ def connect(path: Path) -> sqlite3.Connection:
     conn = sqlite3.connect(path, isolation_level=None)
     try:
         conn.row_factory = sqlite3.Row
-        conn.executescript(SCHEMA)
-        rows = conn.execute("SELECT version FROM schema_version").fetchall()
-        if not rows:
-            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
-        elif [row["version"] for row in rows] != [SCHEMA_VERSION]:
-            raise RuntimeError(f"unsupported predict schema version {rows[0]['version']}")
+        conn.execute("PRAGMA foreign_keys = ON")
+        with transaction(conn):
+            version = _stored_version(conn)
+            if version != SCHEMA_VERSION:
+                if version is not None and version not in MIGRATIONS:
+                    raise RuntimeError(f"unsupported predict schema version {version}")
+                for statement in _statements(SCHEMA + LEDGER_SCHEMA):
+                    conn.execute(statement)
+                if version is None:
+                    conn.execute(
+                        "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
+                    )
+                else:
+                    for statement in MIGRATIONS[version]:
+                        conn.execute(statement)
+                    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
     except BaseException:
         conn.close()
         raise
```

`connect` runs no DDL when the stored version is already current, so a dropped trigger is **reported** by `doctor` (Task 7) rather than silently recreated.

Apply to `src/predict_agent/collect.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/src/predict_agent/collect.py b/src/predict_agent/collect.py
index a2cdff6..a2961ab 100644
--- a/src/predict_agent/collect.py
+++ b/src/predict_agent/collect.py
@@ -314,10 +314,12 @@ def poll_resolutions(
     """Observe resolution state for every tracked market, cross-check against Gamma, and
     refresh each market's rules from the same Gamma response (clarifications often arrive
     after a market has left discovery). Both source responses and their retrieval times are
-    stored with the observation."""
+    stored with the observation, plus the time the resolution request started, so
+    settlement can tell overlapping polls apart."""
     ids = [r["condition_id"] for r in conn.execute("SELECT condition_id FROM markets ORDER BY 1")]
     if not ids:
         return 0
+    resolution_requested_at = now_fn()
     rows = fetch_resolutions(client, ids)
     resolution_fetched_at = now_fn()
     gamma = fetch_gamma_markets(client, ids)
@@ -360,8 +362,8 @@ def poll_resolutions(
             conn.execute(
                 "INSERT INTO resolution_observations (run_id, condition_id, "
                 "resolution_fetched_at, gamma_fetched_at, gamma_json, status, outcome, "
-                "cross_check, was_disputed, new_version_q, raw_json) "
-                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
+                "cross_check, was_disputed, new_version_q, raw_json, resolution_requested_at) "
+                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (
                     run_id,
                     condition_id,
@@ -374,6 +376,7 @@ def poll_resolutions(
                     int(state.was_disputed),
                     int(state.new_version_q),
                     canonical_json(state.raw),
+                    isoformat(resolution_requested_at),
                 ),
             )
             if outcome == "UNKNOWN":
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_schema -v` → 5 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK (Plan 1's `test_reconnect_is_idempotent` reads `SCHEMA_VERSION`, so it follows the bump).

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/ledger_schema.py src/predict_agent/db.py src/predict_agent/collect.py tests/predict/test_ledger_schema.py tests/predict/test_collect.py
git commit -m "feat(predict): ledger schema v3 with atomic v2 migration"
```

---

### Task 2: Artifacts and backed cash entries

**Files:**
- Create: `src/predict_agent/artifacts.py`, `src/predict_agent/cash.py`, `tests/predict/test_ledger_cash.py`

**Interfaces:**
- Consumes: `db.append_journal`, `db.transaction`, `db.GENESIS_HASH`; `util.sha256_text`, `util.sha256_json`, `util.isoformat`.
- Produces:
  - `artifacts.ARTIFACT_KINDS`, `artifacts.store_artifact(conn, kind, content, now) -> str` (idempotent; refuses a hash stored under another kind), `artifacts.load_artifact(conn, digest) -> tuple[str, str]`, `artifacts.artifact_kind(conn, digest) -> str | None`
  - `cash.LedgerError(RuntimeError)`, `cash.InsufficientCash(LedgerError)`, `cash.money_text`, `cash.parse_money`, `cash.ENTRY_SIGNS`
  - `cash.append_cash_entry(conn, portfolio_id, entry_type, amount, ticket_id, now) -> int` — caller holds the transaction and has already written the backing record; FUNDING: no ticket, first entry, equals the portfolio bankroll; DEBIT: own `OPEN` ticket, equals `cost_total`, at most one; CREDIT: own ticket with a settlement row, equals `payout`, at most one; per-portfolio hash chain; journals `CASH_<TYPE>`; a DEBIT beyond available cash raises `InsufficientCash`
  - `cash.available_cash(conn, portfolio_id) -> Decimal`, `cash.cash_entry_hash(portfolio_id, entry_type, amount_text, ticket_id, at_text, prev_hash) -> str`
- DEBIT/CREDIT acceptance paths need tickets and settlements, so they are tested in Tasks 5–6 (`CashBackingTests`, exact-decimal sums) and Task 7.


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_ledger_cash.py` (create):

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from predict_agent.artifacts import load_artifact, store_artifact
from predict_agent.cash import LedgerError, append_cash_entry, available_cash
from predict_agent.db import connect, transaction
from tests.predict.fixtures import NOW


class CashTestCase(unittest.TestCase):
    """A bare cohort with one portfolio, written directly: Task 3 adds the real API."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        policy = store_artifact(self.conn, "policy", "{}", NOW)
        self.conn.execute(
            "INSERT INTO cohorts (cohort_id, identity_json, code_version, starting_bankroll, "
            "baseline_window_seconds, status, started_at) "
            "VALUES ('c1', '{}', 'test', '1000.10', 1800, 'ACTIVE', '2026-10-05T12:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO portfolios (portfolio_id, cohort_id, variant, policy_hash, "
            "starting_bankroll) VALUES ('p1', 'c1', 'primary', ?, '1000.10')",
            (policy,),
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def entry(self, entry_type: str, amount: str, ticket_id: int | None = None) -> int:
        with transaction(self.conn):
            return append_cash_entry(
                self.conn, "p1", entry_type, Decimal(amount), ticket_id, NOW
            )


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
        digest = store_artifact(self.conn, "policy", '{"x": 1}', NOW)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "UPDATE artifacts SET content = 'y' WHERE artifact_hash = ?", (digest,)
            )


class FundingTests(CashTestCase):
    def test_funding_equals_bankroll_exactly_and_journals(self) -> None:
        self.entry("FUNDING", "1000.10")
        balance = available_cash(self.conn, "p1")
        self.assertEqual(str(balance), "1000.10")
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM journal ORDER BY seq")]
        self.assertEqual(kinds, ["CASH_FUNDING"])

    def test_second_or_wrong_funding_is_refused(self) -> None:
        with self.assertRaisesRegex(LedgerError, "starting bankroll"):
            self.entry("FUNDING", "5000")
        self.entry("FUNDING", "1000.10")
        with self.assertRaisesRegex(LedgerError, "already funded"):
            self.entry("FUNDING", "1000.10")
        self.assertEqual(available_cash(self.conn, "p1"), Decimal("1000.10"))

    def test_entries_need_their_backing_record(self) -> None:
        self.entry("FUNDING", "1000.10")
        cases = {
            "funding tied to a ticket": ("FUNDING", "1000.10", 1),
            "debit without a ticket": ("DEBIT", "1", None),
            "credit without a ticket": ("CREDIT", "1", None),
            "credit for an unknown ticket": ("CREDIT", "1", 999),
        }
        for label, (entry_type, amount, ticket_id) in cases.items():
            with self.subTest(label), self.assertRaises(LedgerError):
                self.entry(entry_type, amount, ticket_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cash_ledger").fetchone()[0], 1)

    def test_unknown_portfolio_and_invalid_amounts_rejected(self) -> None:
        with self.assertRaisesRegex(LedgerError, "unknown portfolio"), transaction(self.conn):
            append_cash_entry(self.conn, "nope", "FUNDING", Decimal("1"), None, NOW)
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
                self.entry(entry_type, amount, 1)

    def test_cash_ledger_is_append_only(self) -> None:
        self.entry("FUNDING", "1000.10")
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

`src/predict_agent/artifacts.py` (create):

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


def artifact_kind(conn: sqlite3.Connection, digest: str) -> str | None:
    row = conn.execute("SELECT kind FROM artifacts WHERE artifact_hash = ?", (digest,)).fetchone()
    return None if row is None else str(row["kind"])
```

`src/predict_agent/cash.py` (create):

```python
"""Per-portfolio virtual cash. Amounts are Decimal text and are summed in Python: SQLite's
SUM() would convert TEXT to float.

Every entry must be backed by the record it accounts for: the one FUNDING equals the
portfolio's starting bankroll and comes first; a DEBIT equals its OPEN ticket's cost; a
CREDIT equals its ticket's settlement payout. Nothing else can move cash."""

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


def available_cash(conn: sqlite3.Connection, portfolio_id: str) -> Decimal:
    total = Decimal("0")
    for row in conn.execute(
        "SELECT entry_type, amount FROM cash_ledger WHERE portfolio_id = ?", (portfolio_id,)
    ):
        total += ENTRY_SIGNS[row["entry_type"]] * parse_money(row["amount"])
    return total


def cash_entry_hash(
    portfolio_id: str,
    entry_type: str,
    amount_text: str,
    ticket_id: int | None,
    at_text: str,
    prev_hash: str,
) -> str:
    return sha256_json(
        {
            "portfolio_id": portfolio_id,
            "entry_type": entry_type,
            "amount": amount_text,
            "ticket_id": ticket_id,
            "at": at_text,
            "prev_hash": prev_hash,
        }
    )


def _check_backing(
    conn: sqlite3.Connection,
    portfolio_id: str,
    entry_type: str,
    amount: Decimal,
    ticket_id: int | None,
) -> None:
    portfolio = conn.execute(
        "SELECT starting_bankroll FROM portfolios WHERE portfolio_id = ?", (portfolio_id,)
    ).fetchone()
    if portfolio is None:
        raise LedgerError(f"unknown portfolio {portfolio_id[:12]}")
    if entry_type == "FUNDING":
        if ticket_id is not None:
            raise LedgerError("FUNDING is not tied to a ticket")
        if conn.execute(
            "SELECT 1 FROM cash_ledger WHERE portfolio_id = ?", (portfolio_id,)
        ).fetchone():
            raise LedgerError(f"portfolio {portfolio_id[:12]} is already funded")
        if amount != parse_money(portfolio["starting_bankroll"]):
            raise LedgerError("FUNDING must equal the portfolio's starting bankroll")
        return
    if ticket_id is None:
        raise LedgerError(f"a {entry_type} needs a ticket")
    ticket = conn.execute(
        "SELECT portfolio_id, status, cost_total FROM paper_tickets WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    if ticket is None:
        raise LedgerError(f"unknown ticket {ticket_id}")
    if ticket["portfolio_id"] != portfolio_id:
        raise LedgerError(f"ticket {ticket_id} belongs to another portfolio")
    if conn.execute(
        "SELECT 1 FROM cash_ledger WHERE ticket_id = ? AND entry_type = ?",
        (ticket_id, entry_type),
    ).fetchone():
        raise LedgerError(f"ticket {ticket_id} already has a {entry_type}")
    if entry_type == "DEBIT":
        if ticket["status"] != "OPEN" or amount != parse_money(ticket["cost_total"]):
            raise LedgerError(f"DEBIT must equal OPEN ticket {ticket_id}'s cost_total")
        return
    settlement = conn.execute(
        "SELECT payout FROM settlements WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    if settlement is None:
        raise LedgerError(f"CREDIT for ticket {ticket_id} has no settlement")
    if amount != parse_money(settlement["payout"]):
        raise LedgerError(f"CREDIT must equal ticket {ticket_id}'s settlement payout")


def append_cash_entry(
    conn: sqlite3.Connection,
    portfolio_id: str,
    entry_type: str,
    amount: Decimal,
    ticket_id: int | None,
    now: datetime,
) -> int:
    """Append one backed cash entry and its journal entry. The caller holds the transaction
    and has already written the backing record (portfolio, ticket or settlement)."""
    if entry_type not in ENTRY_SIGNS:
        raise LedgerError(f"unknown cash entry type {entry_type!r}")
    amount_text = money_text(amount)
    if amount < 0 or (entry_type == "FUNDING" and amount == 0):
        raise LedgerError(f"invalid {entry_type} amount {amount_text}")
    _check_backing(conn, portfolio_id, entry_type, amount, ticket_id)
    if entry_type == "DEBIT":
        available = available_cash(conn, portfolio_id)
        if available < amount:
            raise InsufficientCash(
                f"portfolio {portfolio_id[:12]}: debit {amount_text} exceeds available "
                f"{money_text(available)}"
            )
    last = conn.execute(
        "SELECT entry_hash FROM cash_ledger WHERE portfolio_id = ? "
        "ORDER BY entry_id DESC LIMIT 1",
        (portfolio_id,),
    ).fetchone()
    prev_hash = last["entry_hash"] if last else GENESIS_HASH
    at_text = isoformat(now)
    entry_hash = cash_entry_hash(
        portfolio_id, entry_type, amount_text, ticket_id, at_text, prev_hash
    )
    cursor = conn.execute(
        "INSERT INTO cash_ledger (portfolio_id, entry_type, amount, ticket_id, at, entry_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (portfolio_id, entry_type, amount_text, ticket_id, at_text, entry_hash),
    )
    append_journal(
        conn,
        f"CASH_{entry_type}",
        {
            "portfolio_id": portfolio_id,
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

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_cash -v` → 9 tests OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/artifacts.py src/predict_agent/cash.py tests/predict/test_ledger_cash.py
git commit -m "feat(predict): content-addressed artifacts and backed cash entries"
```

---

### Task 3: Cohorts and portfolios

**Files:**
- Create: `src/predict_agent/cohorts.py`, `tests/predict/test_ledger_cohorts.py`

**Interfaces:**
- Consumes: `artifacts.artifact_kind`; `cash.append_cash_entry`, `cash.money_text`, `cash.LedgerError`; `db.transaction`, `db.append_journal`; `util.*`.
- Produces:
  - `cohorts.PRIMARY = "primary"`
  - `cohorts.CohortIdentity` frozen dataclass: `portfolios: Mapping[str, str]` (variant → policy hash, must include `primary`; variant names `^[a-z][a-z0-9_]{0,31}$`), `prompt_hash`, `model_id`, `research_settings: Mapping[str, Any]`, `scoring_version`, `baseline_window_seconds: int`, `generation: int = 1`; method `record()`
  - `cohorts.cohort_id_for(identity) -> str`, `cohorts.portfolio_id_for(cohort_id, variant) -> str`, `cohorts.validate_identity(identity)`
  - `cohorts.ensure_cohort(conn, identity, *, starting_bankroll, code_version, now) -> str` — returns the active cohort for this identity, or opens it (closing every other active cohort, creating and funding each portfolio once with the same bankroll); refuses a closed identity (advises `generation`), unknown artifacts, invalid identities, a non-positive bankroll
  - `cohorts.active_cohort(conn) -> str | None`, `cohorts.cohort_portfolios(conn, cohort_id) -> dict[str, str]` (variant → portfolio id), `cohorts.code_version(root) -> str`


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_ledger_cohorts.py` (create):

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
    cohort_portfolios,
    ensure_cohort,
)
from predict_agent.db import connect
from tests.predict.fixtures import NOW

REPO = Path(__file__).resolve().parents[2]


class CohortTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.policy = store_artifact(self.conn, "policy", '{"price": "bounds"}', NOW)
        self.shadow = store_artifact(self.conn, "policy", '{"price": "p_mid"}', NOW)
        prompt = store_artifact(self.conn, "prompt", "Forecast without prices.", NOW)
        self.identity = CohortIdentity(
            portfolios={"primary": self.policy, "shadow_mid": self.shadow},
            prompt_hash=prompt,
            model_id="claude-model-x",
            research_settings={"tools": ["WebSearch", "WebFetch"], "daily_usd": "5"},
            scoring_version="1",
            baseline_window_seconds=1800,
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def open(self, identity: CohortIdentity, bankroll: str = "1000") -> str:
        return ensure_cohort(
            self.conn, identity, starting_bankroll=Decimal(bankroll), code_version="abc", now=NOW
        )

    def test_each_portfolio_is_funded_once_and_independently(self) -> None:
        cohort = self.open(self.identity)
        self.assertEqual(self.open(self.identity), cohort)
        portfolios = cohort_portfolios(self.conn, cohort)
        self.assertEqual(set(portfolios), {"primary", "shadow_mid"})
        for portfolio_id in portfolios.values():
            self.assertEqual(available_cash(self.conn, portfolio_id), Decimal("1000"))
        self.assertEqual(active_cohort(self.conn), cohort)
        fundings = self.conn.execute(
            "SELECT COUNT(*) FROM cash_ledger WHERE entry_type = 'FUNDING'"
        ).fetchone()[0]
        self.assertEqual(fundings, 2)

    def test_every_identity_field_changes_the_cohort(self) -> None:
        other_prompt = store_artifact(self.conn, "prompt", "Forecast v2.", NOW)
        variants = {
            "portfolios": replace(self.identity, portfolios={"primary": self.policy}),
            "prompt_hash": replace(self.identity, prompt_hash=other_prompt),
            "model_id": replace(self.identity, model_id="claude-model-y"),
            "research_settings": replace(self.identity, research_settings={"tools": []}),
            "scoring_version": replace(self.identity, scoring_version="2"),
            "baseline_window_seconds": replace(self.identity, baseline_window_seconds=600),
            "generation": replace(self.identity, generation=2),
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
        old_primary = cohort_portfolios(self.conn, old)["primary"]
        new_primary = cohort_portfolios(self.conn, new)["primary"]
        self.assertEqual(available_cash(self.conn, old_primary), Decimal("1000"))
        self.assertEqual(available_cash(self.conn, new_primary), Decimal("250"))
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM journal ORDER BY seq")]
        self.assertIn("COHORT_CLOSED", kinds)

    def test_reopening_a_closed_identity_is_refused_but_a_new_generation_opens(self) -> None:
        self.open(self.identity)
        self.open(replace(self.identity, model_id="claude-model-y"))
        with self.assertRaisesRegex(LedgerError, "generation"):
            self.open(self.identity)
        restarted = self.open(replace(self.identity, generation=2))
        self.assertEqual(active_cohort(self.conn), restarted)

    def test_invalid_identities_and_bankrolls_refused(self) -> None:
        bad = {
            "unknown prompt": replace(self.identity, prompt_hash="0" * 64),
            "prompt used as policy": replace(
                self.identity, portfolios={"primary": self.identity.prompt_hash}
            ),
            "no primary": replace(self.identity, portfolios={"shadow_mid": self.shadow}),
            "bad variant": replace(
                self.identity, portfolios={"primary": self.policy, "Shadow Mid": self.shadow}
            ),
            "zero window": replace(self.identity, baseline_window_seconds=0),
            "generation 0": replace(self.identity, generation=0),
        }
        for label, identity in bad.items():
            with self.subTest(label), self.assertRaises(LedgerError):
                self.open(identity)
        for bankroll in ("0", "-5", "NaN"):
            with self.subTest(bankroll=bankroll), self.assertRaises(LedgerError):
                self.open(self.identity, bankroll=bankroll)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM portfolios").fetchone()[0], 0)

    def test_cohort_and_portfolio_rows_are_frozen(self) -> None:
        cohort = self.open(self.identity)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                "UPDATE cohorts SET starting_bankroll = '9' WHERE cohort_id = ?", (cohort,)
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE portfolios SET policy_hash = ?", (self.shadow,))

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

`src/predict_agent/cohorts.py` (create):

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_cohorts -v` → 7 tests OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/cohorts.py tests/predict/test_ledger_cohorts.py
git commit -m "feat(predict): cohorts with independently funded portfolio variants"
```

---

### Task 4: Research attempts, forecast records, baselines and resume state

**Files:**
- Create: `src/predict_agent/forecasts.py`, `tests/predict/ledger_fixtures.py`, `tests/predict/test_ledger_forecasts.py`

**Interfaces:**
- Consumes: `artifacts.artifact_kind`, `artifacts.store_artifact`; `cash.LedgerError`, `cash.money_text`; `cohorts.*`; `db.transaction`, `db.append_journal`; `util.*`.
- Produces:
  - `forecasts.ForecastError(LedgerError)`, `forecasts.ResumeStep` (`NEEDS_BASELINE`, `BASELINE_EXPIRED`, `NEEDS_DECISION`, `DONE`), `forecasts.BODY_FIELDS`, `forecasts.FORECAST_HASH_COLUMNS`, `forecasts.forecast_hash(values) -> str`
  - `forecasts.start_attempt(conn, cohort_id, condition_id, kind, now) -> int` (active cohort; before any paid call), `forecasts.fail_attempt(conn, attempt_id, cost_usd, error, now)`, `forecasts.unfinished_attempts(conn, cohort_id) -> list[int]`
  - `forecasts.ForecastRecord` frozen dataclass: `attempt_id`, `cohort_id`, `condition_id`, `rules_hash`, `kind`, `abstained`, `abstain_reason`, `p_low/p_mid/p_high`, `confidence`, `base_rate` (required unless abstained), `body` (needs `evidence: list`, `rules_interpretation: str`, `exposure_flags: list`), `research_input_hash`, `transcript_hash`, `cost_usd`
  - `forecasts.validate_forecast(record)`, `forecasts.record_forecast(conn, record, now) -> int` (marks its `STARTED` attempt `SUCCEEDED` with the forecast's cost in the same transaction; one entry per market per cohort)
  - `forecasts.baseline_deadline(forecast_row) -> datetime`, `forecasts.attach_baseline(conn, forecast_id, yes_snapshot_id, no_snapshot_id, now)` (both fetched inside the window; set once), `forecasts.mark_no_timely_baseline(conn, forecast_id, now)` (only after the deadline; entry forecasts get `NO_TIMELY_BASELINE` in every portfolio)
  - `forecasts.undecided_portfolios(conn, forecast_id) -> list[str]`, `forecasts.resume_step(conn, forecast_id, now) -> ResumeStep` (uses the cohort's frozen window), `forecasts.unfinished_forecasts(conn, cohort_id) -> list[int]`
  - Test helpers in `tests/predict/ledger_fixtures.py`: `seed_market`, `seed_snapshot`, `seed_observation(conn, outcome, *, cross_check, status, condition_id, fetched_at, requested_at)`, `seed_cohort(conn, *, model_id, bankroll, shadow)`, `portfolio(conn, cohort_id, variant)`, `forecast_record(conn, cohort_id, rules_hash, *, at, **overrides)` (opens its attempt), `seed_entry_forecast`, `seed_baselined_forecast(...) -> (forecast_id, yes_id, no_id)`; Task 5 adds `ticket_draft`


- [ ] **Step 1: Write the failing tests**

`tests/predict/ledger_fixtures.py` (create):

```python
"""Seed helpers for ledger tests. Rows mirror what Plan 1 collection and later plans write."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from predict_agent.artifacts import store_artifact
from predict_agent.cohorts import PRIMARY, CohortIdentity, cohort_portfolios, ensure_cohort
from predict_agent.forecasts import (
    ForecastRecord,
    attach_baseline,
    record_forecast,
    start_attempt,
)
from predict_agent.util import canonical_json, isoformat, sha256_json
from tests.predict.fixtures import CONDITION_ID, NO_TOKEN, NOW, YES_TOKEN

PRIMARY_POLICY = '{"min_edge": "0.05", "price": "bounds"}'
SHADOW_POLICY = '{"min_edge": "0.05", "price": "p_mid"}'
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
    fetched_at: datetime = NOW,
    requested_at: datetime | None = None,
) -> int:
    """An observation whose resolution request ran over [requested_at, fetched_at]
    (default: one second before fetched_at)."""
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
            isoformat(fetched_at),
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
```

`tests/predict/test_ledger_forecasts.py` (create):

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
    fail_attempt,
    mark_no_timely_baseline,
    record_forecast,
    resume_step,
    start_attempt,
    unfinished_attempts,
    unfinished_forecasts,
    validate_forecast,
)
from tests.predict.fixtures import CONDITION_ID, NOW
from tests.predict.ledger_fixtures import (
    forecast_record,
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_snapshot,
)


class ForecastTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.rules_hash = seed_market(self.conn)
        self.cohort = seed_cohort(self.conn, shadow=True)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def books(self, at_offset: timedelta) -> tuple[int, int]:
        return (
            seed_snapshot(self.conn, "YES", NOW + at_offset),
            seed_snapshot(self.conn, "NO", NOW + at_offset),
        )


class AttemptTests(ForecastTestCase):
    def test_forecast_closes_its_attempt_with_its_cost(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        attempt = self.conn.execute(
            "SELECT a.status, a.cost_usd FROM research_attempts a "
            "JOIN forecasts f ON f.attempt_id = a.attempt_id WHERE f.forecast_id = ?",
            (forecast_id,),
        ).fetchone()
        self.assertEqual(tuple(attempt), ("SUCCEEDED", "0.12"))
        self.assertEqual(unfinished_attempts(self.conn, self.cohort), [])

    def test_failed_attempt_keeps_its_cost_and_cannot_be_reused(self) -> None:
        attempt = start_attempt(self.conn, self.cohort, CONDITION_ID, "entry", NOW)
        self.assertEqual(unfinished_attempts(self.conn, self.cohort), [attempt])
        fail_attempt(self.conn, attempt, Decimal("0.07"), "schema rejected", NOW)
        row = self.conn.execute("SELECT status, cost_usd, error FROM research_attempts").fetchone()
        self.assertEqual(tuple(row), ("FAILED", "0.07", "schema rejected"))
        with self.assertRaisesRegex(ForecastError, "not a STARTED attempt"):
            seed_entry_forecast(self.conn, self.cohort, self.rules_hash, attempt_id=attempt)
        with self.assertRaisesRegex(ForecastError, "not STARTED"):
            fail_attempt(self.conn, attempt, Decimal("0"), "again", NOW)

    def test_attempt_must_match_the_forecast(self) -> None:
        attempt = start_attempt(self.conn, self.cohort, CONDITION_ID, "update", NOW)
        with self.assertRaisesRegex(ForecastError, "not a STARTED attempt"):
            seed_entry_forecast(self.conn, self.cohort, self.rules_hash, attempt_id=attempt)

    def test_attempts_only_finish_once(self) -> None:
        seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE research_attempts SET cost_usd = '0'")


class RecordTests(ForecastTestCase):
    def test_records_entry_forecast_and_journals(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        row = self.conn.execute(
            "SELECT p_mid, base_rate, cost_usd, kind FROM forecasts WHERE forecast_id = ?",
            (forecast_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("0.60", "0.30", "0.12", "entry"))
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
            "missing base rate": replace(base, base_rate=None),
            "base rate above 1": replace(base, base_rate=Decimal("1.5")),
            "nan cost": replace(base, cost_usd=Decimal("NaN")),
            "unknown kind": replace(base, kind="draft"),
            "bad hash": replace(base, transcript_hash="xyz"),
            "empty body": replace(base, body={}),
            "evidence not a list": replace(
                base,
                body={"evidence": "x", "rules_interpretation": "r", "exposure_flags": []},
            ),
        }
        for label, record in bad.items():
            with self.subTest(label), self.assertRaises(ForecastError):
                validate_forecast(record)

    def test_abstention_is_valid_without_base_rate(self) -> None:
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
            base_rate=None,
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
        yes, no = self.books(timedelta(seconds=5))
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

    def test_books_fetched_after_the_window_are_refused(self) -> None:
        # Collection started inside the window but the books arrived at +31 minutes.
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        late = NOW + timedelta(minutes=31)
        self.assertEqual(
            resume_step(self.conn, forecast_id, late), ResumeStep.BASELINE_EXPIRED
        )
        yes, no = self.books(timedelta(minutes=31))
        with self.assertRaisesRegex(ForecastError, "after the baseline window"):
            attach_baseline(self.conn, forecast_id, yes, no, late)
        self.assertEqual(
            resume_step(self.conn, forecast_id, late), ResumeStep.BASELINE_EXPIRED
        )

    def test_timely_books_may_be_attached_late(self) -> None:
        # Books fetched at +29 minutes, attached after a crash at +45 minutes.
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        yes, no = self.books(timedelta(minutes=29))
        attach_baseline(self.conn, forecast_id, yes, no, NOW + timedelta(minutes=45))
        self.assertEqual(
            resume_step(self.conn, forecast_id, NOW + timedelta(minutes=45)),
            ResumeStep.NEEDS_DECISION,
        )

    def test_baseline_sides_and_market_must_match(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        yes, no = self.books(timedelta(seconds=1))
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
        self.assertEqual(resume_step(self.conn, forecast_id, later), ResumeStep.NEEDS_BASELINE)
        yes, no = self.books(timedelta(minutes=1))
        attach_baseline(self.conn, forecast_id, yes, no, later)
        self.assertEqual(resume_step(self.conn, forecast_id, later), ResumeStep.NEEDS_DECISION)
        self.assertEqual(unfinished_forecasts(self.conn, self.cohort), [forecast_id])

    def test_late_resume_marks_no_timely_baseline_in_every_portfolio(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        with self.assertRaisesRegex(ForecastError, "still open"):
            mark_no_timely_baseline(self.conn, forecast_id, NOW + timedelta(minutes=29))
        late = NOW + timedelta(minutes=31)
        mark_no_timely_baseline(self.conn, forecast_id, late)
        self.assertEqual(resume_step(self.conn, forecast_id, late), ResumeStep.DONE)
        decisions = [r[0] for r in self.conn.execute("SELECT kind FROM decisions")]
        self.assertEqual(decisions, ["NO_TIMELY_BASELINE", "NO_TIMELY_BASELINE"])
        self.assertEqual(unfinished_forecasts(self.conn, self.cohort), [])

    def test_update_forecast_is_done_after_baseline(self) -> None:
        seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        update_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash, kind="update")
        later = NOW + timedelta(minutes=1)
        yes, no = self.books(timedelta(minutes=1))
        attach_baseline(self.conn, update_id, yes, no, later)
        self.assertEqual(resume_step(self.conn, update_id, later), ResumeStep.DONE)
        self.assertNotIn(update_id, unfinished_forecasts(self.conn, self.cohort))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_forecasts -v`
Expected: ERROR `No module named 'predict_agent.forecasts'`.

- [ ] **Step 3: Implement**

`src/predict_agent/forecasts.py` (create):

```python
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
    _valid_cost(cost_usd)
    if not error:
        raise ForecastError("a failed attempt needs an error")
    with transaction(conn):
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
            portfolios = conn.execute(
                "SELECT portfolio_id FROM portfolios WHERE cohort_id = ?",
                (forecast["cohort_id"],),
            ).fetchall()
            for portfolio in portfolios:
                conn.execute(
                    "INSERT INTO decisions (portfolio_id, forecast_id, condition_id, kind, "
                    "reason, ticket_id, decided_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
                    (
                        portfolio["portfolio_id"],
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
    otherwise mark NO_TIMELY_BASELINE. Books fetched now would be refused."""
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
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_forecasts -v` → 18 tests OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/forecasts.py tests/predict/ledger_fixtures.py tests/predict/test_ledger_forecasts.py
git commit -m "feat(predict): research attempts, forecast records, timely baselines, resume state"
```

---

### Task 5: Tickets, decisions, equity

**Files:**
- Create: `src/predict_agent/tickets.py`, `tests/predict/test_ledger_tickets.py`
- Modify: `tests/predict/ledger_fixtures.py`

**Interfaces:**
- Consumes: `cash.*`; `db.transaction`, `db.append_journal`; fixtures from Task 4.
- Produces:
  - `tickets.Fill(price, shares)`, `tickets.TicketDraft`: `portfolio_id`, `forecast_id`, `snapshot_id`, `condition_id`, `outcome`, `fills`, `fee`, `policy_hash`, `rules_hash`
  - `tickets.ticket_totals(draft) -> (shares, cost_total)`, `tickets.TICKET_HASH_COLUMNS`, `tickets.ticket_hash(values) -> str`
  - `tickets.open_ticket(conn, draft, now) -> int` — one transaction: checks the portfolio's cohort is active, **the draft's policy equals the portfolio's frozen policy**, the forecast is a non-abstained entry forecast of that cohort/market with the same rules, the portfolio has not decided it, and the snapshot is the forecast's timely baseline for the bought side; then ticket `OPEN`, backed `DEBIT`, decision `TRADED`, journal `TICKET_OPENED`
  - `tickets.record_refusal_decision(conn, portfolio_id, forecast_id, reason, now)`
  - `tickets.open_cost(conn, portfolio_id) -> Decimal`, `tickets.equity(conn, portfolio_id) -> Decimal` (available cash + cost basis of open tickets, spec §5)


- [ ] **Step 1: Write the failing tests**

Apply to `tests/predict/ledger_fixtures.py` (adds `ticket_draft` now that `tickets` exists):

```diff
--- a/tests/predict/ledger_fixtures.py
+++ b/tests/predict/ledger_fixtures.py
@@ -17,6 +17,7 @@
     record_forecast,
     start_attempt,
 )
+from predict_agent.tickets import Fill, TicketDraft
 from predict_agent.util import canonical_json, isoformat, sha256_json
 from tests.predict.fixtures import CONDITION_ID, NO_TOKEN, NOW, YES_TOKEN

@@ -200,3 +201,33 @@
     no = seed_snapshot(conn, "NO", later, condition_id)
     attach_baseline(conn, forecast, yes, no, later)
     return forecast, yes, no
+
+
+def ticket_draft(
+    conn: sqlite3.Connection,
+    portfolio_id: str,
+    forecast_id: int,
+    snapshot_id: int,
+    *,
+    outcome: str = "YES",
+    fills: tuple[Fill, ...] = (Fill(Decimal("0.40"), Decimal("10")),),
+    fee: Decimal = Decimal("0"),
+) -> TicketDraft:
+    """A draft that satisfies every ledger check, using the portfolio's own policy."""
+    forecast = conn.execute(
+        "SELECT condition_id, rules_hash FROM forecasts WHERE forecast_id = ?", (forecast_id,)
+    ).fetchone()
+    policy = conn.execute(
+        "SELECT policy_hash FROM portfolios WHERE portfolio_id = ?", (portfolio_id,)
+    ).fetchone()
+    return TicketDraft(
+        portfolio_id=portfolio_id,
+        forecast_id=forecast_id,
+        snapshot_id=snapshot_id,
+        condition_id=forecast["condition_id"],
+        outcome=outcome,
+        fills=fills,
+        fee=fee,
+        policy_hash=policy["policy_hash"],
+        rules_hash=forecast["rules_hash"],
+    )
```

`tests/predict/test_ledger_tickets.py` (create):

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

from predict_agent.artifacts import store_artifact
from predict_agent.cash import InsufficientCash, LedgerError, append_cash_entry, available_cash
from predict_agent.db import connect, transaction
from predict_agent.tickets import (
    Fill,
    equity,
    open_cost,
    open_ticket,
    record_refusal_decision,
)
from tests.predict.fixtures import NOW
from tests.predict.ledger_fixtures import (
    portfolio,
    seed_baselined_forecast,
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_snapshot,
    ticket_draft,
)

LATER = NOW + timedelta(seconds=1)


class TicketTestCase(unittest.TestCase):
    bankroll = "1000"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.rules_hash = seed_market(self.conn)
        self.cohort = seed_cohort(self.conn, bankroll=self.bankroll, shadow=True)
        self.primary = portfolio(self.conn, self.cohort)
        self.shadow = portfolio(self.conn, self.cohort, "shadow_mid")
        self.forecast, self.yes, self.no = seed_baselined_forecast(
            self.conn, self.cohort, self.rules_hash
        )
        self.draft = ticket_draft(
            self.conn,
            self.primary,
            self.forecast,
            self.yes,
            fills=(Fill(Decimal("0.40"), Decimal("10")), Fill(Decimal("0.41"), Decimal("5"))),
            fee=Decimal("0.1"),
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def state(self) -> tuple[int, int, int, int]:
        return (
            self.count("paper_tickets"),
            self.count("decisions"),
            self.count("cash_ledger"),
            self.count("journal"),
        )


class OpenTicketTests(TicketTestCase):
    def test_open_ticket_debits_decides_and_keeps_equity(self) -> None:
        ticket_id = open_ticket(self.conn, self.draft, LATER)
        row = self.conn.execute(
            "SELECT shares, cost_total, status FROM paper_tickets WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("15", "6.15", "OPEN"))
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("993.85"))
        self.assertEqual(open_cost(self.conn, self.primary), Decimal("6.15"))
        self.assertEqual(equity(self.conn, self.primary), Decimal("1000"))
        decision = self.conn.execute("SELECT kind, ticket_id FROM decisions").fetchone()
        self.assertEqual(tuple(decision), ("TRADED", ticket_id))

    def test_open_ticket_failure_leaves_no_partial_state(self) -> None:
        before = self.state()
        with (
            mock.patch("predict_agent.tickets.append_journal", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            open_ticket(self.conn, self.draft, LATER)
        self.assertEqual(self.state(), before)
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("1000"))

    def test_policy_other_than_the_portfolios_is_refused_without_state(self) -> None:
        other = store_artifact(self.conn, "policy", '{"min_edge": "0.01"}', NOW)
        before = self.state()
        for policy in (other, self.conn.execute(
            "SELECT policy_hash FROM portfolios WHERE portfolio_id = ?", (self.shadow,)
        ).fetchone()[0]):
            with (
                self.subTest(policy=policy[:12]),
                self.assertRaisesRegex(LedgerError, "frozen policy"),
            ):
                open_ticket(self.conn, replace(self.draft, policy_hash=policy), LATER)
        self.assertEqual(self.state(), before)

    def test_shadow_portfolio_trades_the_same_forecast_with_its_own_cash(self) -> None:
        open_ticket(self.conn, self.draft, LATER)
        shadow_draft = ticket_draft(self.conn, self.shadow, self.forecast, self.no, outcome="NO")
        open_ticket(self.conn, shadow_draft, LATER)
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("993.85"))
        self.assertEqual(available_cash(self.conn, self.shadow), Decimal("996"))
        self.assertEqual(self.count("forecasts"), 1)
        self.assertEqual(self.count("decisions"), 2)

    def test_snapshot_must_be_the_forecast_baseline_for_that_side(self) -> None:
        with self.assertRaisesRegex(LedgerError, "baseline"):
            open_ticket(self.conn, replace(self.draft, snapshot_id=self.no), LATER)
        stray = seed_snapshot(self.conn, "YES", LATER)
        with self.assertRaisesRegex(LedgerError, "baseline"):
            open_ticket(self.conn, replace(self.draft, snapshot_id=stray), LATER)

    def test_forecast_without_timely_baseline_is_refused(self) -> None:
        other = "0x" + "7" * 64
        rules = seed_market(self.conn, other)
        bare = seed_entry_forecast(self.conn, self.cohort, rules, condition_id=other)
        late_yes = seed_snapshot(self.conn, "YES", NOW + timedelta(minutes=31), other)
        draft = replace(
            self.draft, forecast_id=bare, condition_id=other, rules_hash=rules, snapshot_id=late_yes
        )
        with self.assertRaisesRegex(LedgerError, "baseline"):
            open_ticket(self.conn, draft, NOW + timedelta(minutes=31))

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


class CashBackingTests(TicketTestCase):
    def cash_entry(self, portfolio_id: str, entry_type: str, amount: str, ticket: int) -> None:
        with transaction(self.conn):
            append_cash_entry(self.conn, portfolio_id, entry_type, Decimal(amount), ticket, LATER)

    def test_credit_against_an_open_ticket_is_refused(self) -> None:
        ticket = open_ticket(self.conn, self.draft, LATER)
        with self.assertRaisesRegex(LedgerError, "no settlement"):
            self.cash_entry(self.primary, "CREDIT", "15", ticket)
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("993.85"))

    def test_second_or_foreign_debit_is_refused(self) -> None:
        ticket = open_ticket(self.conn, self.draft, LATER)
        with self.assertRaisesRegex(LedgerError, "already has a DEBIT"):
            self.cash_entry(self.primary, "DEBIT", "6.15", ticket)
        with self.assertRaisesRegex(LedgerError, "another portfolio"):
            self.cash_entry(self.shadow, "DEBIT", "6.15", ticket)

    def test_cash_sums_are_exact_decimal(self) -> None:
        # Three 0.1 debits must leave exactly 0.7 of 1.0, not 0.7000000000000001.
        cohort = seed_cohort(self.conn, model_id="exact", bankroll="1.0")
        primary = portfolio(self.conn, cohort)
        for digit in "234":
            condition = "0x" + digit * 64
            rules = seed_market(self.conn, condition)
            forecast, yes, _ = seed_baselined_forecast(
                self.conn, cohort, rules, condition_id=condition
            )
            draft = ticket_draft(
                self.conn, primary, forecast, yes, fills=(Fill(Decimal("0.1"), Decimal("1")),)
            )
            open_ticket(self.conn, draft, LATER)
        balance = available_cash(self.conn, primary)
        self.assertEqual(balance, Decimal("0.7"))
        self.assertEqual(str(balance), "0.7")


class SmallBankrollTests(TicketTestCase):
    bankroll = "5"

    def test_insufficient_cash_rolls_back(self) -> None:
        before = self.state()
        with self.assertRaises(InsufficientCash):
            open_ticket(self.conn, self.draft, LATER)
        self.assertEqual(self.state(), before)
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("5"))


class RefusalDecisionTests(TicketTestCase):
    def test_refusal_decision_is_recorded_once_per_portfolio(self) -> None:
        record_refusal_decision(self.conn, self.primary, self.forecast, "EDGE_BELOW_MIN", LATER)
        row = self.conn.execute("SELECT kind, reason, ticket_id FROM decisions").fetchone()
        self.assertEqual(tuple(row), ("REFUSED", "EDGE_BELOW_MIN", None))
        with self.assertRaisesRegex(LedgerError, "already decided"):
            record_refusal_decision(self.conn, self.primary, self.forecast, "AGAIN", LATER)
        with self.assertRaisesRegex(LedgerError, "already decided"):
            open_ticket(self.conn, self.draft, LATER)
        record_refusal_decision(self.conn, self.shadow, self.forecast, "EDGE_BELOW_MIN", LATER)
        self.assertEqual(self.count("decisions"), 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_tickets -v`
Expected: ERROR `No module named 'predict_agent.tickets'`.

- [ ] **Step 3: Implement**

`src/predict_agent/tickets.py` (create):

```python
"""Paper tickets and entry decisions, per portfolio. Opening a ticket writes the ticket, its
cash debit, its TRADED decision and the journal entry in one transaction (spec §4)."""

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
TICKET_HASH_COLUMNS = (
    "portfolio_id",
    "forecast_id",
    "snapshot_id",
    "condition_id",
    "outcome",
    "direction",
    "shares",
    "fills_json",
    "fee",
    "cost_total",
    "policy_hash",
    "rules_hash",
    "created_at",
)


@dataclass(frozen=True)
class Fill:
    price: Decimal
    shares: Decimal


@dataclass(frozen=True)
class TicketDraft:
    portfolio_id: str
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
    return sha256_json({column: values[column] for column in TICKET_HASH_COLUMNS})


def _portfolio(conn: sqlite3.Connection, portfolio_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT p.*, c.status AS cohort_status FROM portfolios p "
        "JOIN cohorts c ON c.cohort_id = p.cohort_id WHERE p.portfolio_id = ?",
        (portfolio_id,),
    ).fetchone()
    if row is None:
        raise LedgerError(f"unknown portfolio {portfolio_id[:12]}")
    found: sqlite3.Row = row
    return found


def _decided(conn: sqlite3.Connection, portfolio_id: str, forecast_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM decisions WHERE portfolio_id = ? AND forecast_id = ?",
        (portfolio_id, forecast_id),
    )
    return row.fetchone() is not None


def open_ticket(conn: sqlite3.Connection, draft: TicketDraft, now: datetime) -> int:
    shares, cost_total = ticket_totals(draft)
    with transaction(conn):
        portfolio = _portfolio(conn, draft.portfolio_id)
        if portfolio["cohort_status"] != "ACTIVE":
            raise LedgerError(f"portfolio {draft.portfolio_id[:12]}'s cohort is not active")
        if draft.policy_hash != portfolio["policy_hash"]:
            raise LedgerError("ticket policy differs from the portfolio's frozen policy")
        forecast = conn.execute(
            "SELECT * FROM forecasts WHERE forecast_id = ?", (draft.forecast_id,)
        ).fetchone()
        if (
            forecast is None
            or forecast["cohort_id"] != portfolio["cohort_id"]
            or forecast["condition_id"] != draft.condition_id
            or forecast["kind"] != "entry"
            or forecast["abstained"]
        ):
            raise LedgerError("ticket needs a non-abstained entry forecast of this cohort/market")
        if forecast["rules_hash"] != draft.rules_hash:
            raise LedgerError("ticket rules hash differs from the forecast's rules version")
        if _decided(conn, draft.portfolio_id, draft.forecast_id):
            raise LedgerError(f"forecast {draft.forecast_id} is already decided here")
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
        values: dict[str, Any] = {
            "portfolio_id": draft.portfolio_id,
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
            raise LedgerError(f"market already traded in this portfolio: {error}") from None
        if cursor.lastrowid is None:
            raise LedgerError("ticket insert returned no row id")
        ticket_id = cursor.lastrowid
        append_cash_entry(conn, draft.portfolio_id, "DEBIT", cost_total, ticket_id, now)
        conn.execute(
            "INSERT INTO decisions (portfolio_id, forecast_id, condition_id, kind, reason, "
            "ticket_id, decided_at) VALUES (?, ?, ?, 'TRADED', NULL, ?, ?)",
            (draft.portfolio_id, draft.forecast_id, draft.condition_id, ticket_id, isoformat(now)),
        )
        append_journal(
            conn,
            "TICKET_OPENED",
            {
                "ticket_id": ticket_id,
                "portfolio_id": draft.portfolio_id,
                "condition_id": draft.condition_id,
                "outcome": draft.outcome,
                "cost_total": values["cost_total"],
                "ticket_hash": values["ticket_hash"],
            },
            now,
        )
    return ticket_id


def record_refusal_decision(
    conn: sqlite3.Connection, portfolio_id: str, forecast_id: int, reason: str, now: datetime
) -> None:
    if not reason:
        raise LedgerError("a refusal decision needs a reason")
    with transaction(conn):
        portfolio = _portfolio(conn, portfolio_id)
        forecast = conn.execute(
            "SELECT cohort_id, condition_id, kind FROM forecasts WHERE forecast_id = ?",
            (forecast_id,),
        ).fetchone()
        if (
            forecast is None
            or forecast["kind"] != "entry"
            or forecast["cohort_id"] != portfolio["cohort_id"]
        ):
            raise LedgerError(f"forecast {forecast_id} is not an entry forecast of this cohort")
        if _decided(conn, portfolio_id, forecast_id):
            raise LedgerError(f"forecast {forecast_id} is already decided here")
        conn.execute(
            "INSERT INTO decisions (portfolio_id, forecast_id, condition_id, kind, reason, "
            "ticket_id, decided_at) VALUES (?, ?, ?, 'REFUSED', ?, NULL, ?)",
            (portfolio_id, forecast_id, forecast["condition_id"], reason, isoformat(now)),
        )
        append_journal(
            conn,
            "DECISION_REFUSED",
            {"portfolio_id": portfolio_id, "forecast_id": forecast_id, "reason": reason},
            now,
        )


def open_cost(conn: sqlite3.Connection, portfolio_id: str) -> Decimal:
    total = Decimal("0")
    for row in conn.execute(
        "SELECT cost_total FROM paper_tickets WHERE portfolio_id = ? AND status = 'OPEN'",
        (portfolio_id,),
    ):
        total += parse_money(row["cost_total"])
    return total


def equity(conn: sqlite3.Connection, portfolio_id: str) -> Decimal:
    return available_cash(conn, portfolio_id) + open_cost(conn, portfolio_id)
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_tickets -v` → 15 tests OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/tickets.py tests/predict/ledger_fixtures.py tests/predict/test_ledger_tickets.py
git commit -m "feat(predict): per-portfolio paper tickets, decisions and equity"
```

---

### Task 6: Settlement

**Files:**
- Create: `src/predict_agent/settlement.py`, `tests/predict/test_ledger_settlement.py`

**Interfaces:**
- Consumes: `cash.append_cash_entry`, `cash.money_text`, `cash.parse_money`; `db.transaction`, `db.append_journal`; `util.parse_datetime`; tickets and fixtures (tests).
- Produces:
  - `settlement.SETTLEABLE_OUTCOMES`, `settlement.payout_per_share(held, outcome) -> Decimal` (YES/NO → 1 or 0; HALF → 0.5)
  - `settlement.PendingReason` (`AWAITING_RESOLUTION`, `AWAITING_CONFIRMATION`, `AMBIGUOUS_EVIDENCE`, `UNSETTLEABLE`), `settlement.PendingSettlement(ticket_id, condition_id, reason, resolved_since)` with `age(now)`, `settlement.SettlementSummary(settled, pending)` with `count(reason)`
  - `settlement.governing_observation(rows) -> (row | None, ambiguous: bool)` — latest by `resolution_fetched_at`; ambiguous when contradictory evidence ties it or finished after its request started
  - `settlement.settle_open_tickets(conn, now) -> SettlementSummary` — per `OPEN` ticket (any cohort, open or closed), one transaction that re-reads the ticket and the market's observations, then either records why it waits or writes settlement row, `SETTLED`, backed `CREDIT` (possibly 0) and journal `TICKET_SETTLED`


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_ledger_settlement.py` (create):

```python
from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from predict_agent import db
from predict_agent.cash import available_cash
from predict_agent.db import connect
from predict_agent.forecasts import ForecastError
from predict_agent.settlement import PendingReason, payout_per_share, settle_open_tickets
from predict_agent.tickets import open_ticket
from tests.predict.fixtures import NOW
from tests.predict.ledger_fixtures import (
    portfolio,
    seed_baselined_forecast,
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_observation,
    ticket_draft,
)

LATER = NOW + timedelta(seconds=1)
RESOLVED_AT = NOW + timedelta(days=20)
SETTLE_AT = NOW + timedelta(days=30)


class SettlementTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "data" / "predict.sqlite3"
        self.conn = connect(self.path)
        self.rules_hash = seed_market(self.conn)
        self.cohort = seed_cohort(self.conn)
        self.primary = portfolio(self.conn, self.cohort)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def open_test_ticket(self, outcome: str) -> int:
        """Buys 10 shares of `outcome` at 0.40 with no fee: cost 4."""
        forecast, yes, no = seed_baselined_forecast(self.conn, self.cohort, self.rules_hash)
        snapshot = yes if outcome == "YES" else no
        draft = ticket_draft(self.conn, self.primary, forecast, snapshot, outcome=outcome)
        return open_ticket(self.conn, draft, LATER)

    def observe(self, outcome: str | None, minutes: int = 0, **kwargs: object) -> int:
        at = RESOLVED_AT + timedelta(minutes=minutes)
        return seed_observation(self.conn, outcome, fetched_at=at, **kwargs)  # type: ignore[arg-type]

    def settlement(self, ticket_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM settlements WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        assert row is not None
        return row

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


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
        for held, outcome in (("YES", "UNKNOWN"), ("MAYBE", "HALF")):
            with self.subTest(held=held, outcome=outcome), self.assertRaises(ValueError):
                payout_per_share(held, outcome)


class SettleTests(SettlementTestCase):
    def test_winning_yes_ticket_settles_at_one(self) -> None:
        ticket = self.open_test_ticket("YES")
        self.observe("YES")
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual((summary.settled, summary.pending), (1, ()))
        row = self.settlement(ticket)
        self.assertEqual(Decimal(row["payout"]), Decimal("10"))
        self.assertEqual(Decimal(row["net_pnl"]), Decimal("6"))
        self.assertEqual(row["outcome"], "YES")
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("1006"))
        status = self.conn.execute("SELECT status FROM paper_tickets").fetchone()[0]
        self.assertEqual(status, "SETTLED")

    def test_losing_ticket_settles_with_zero_credit(self) -> None:
        ticket = self.open_test_ticket("NO")
        self.observe("YES")
        settle_open_tickets(self.conn, SETTLE_AT)
        row = self.settlement(ticket)
        self.assertEqual(Decimal(row["payout"]), Decimal("0"))
        self.assertEqual(Decimal(row["net_pnl"]), Decimal("-4"))
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("996"))

    def test_half_pays_half_on_either_side(self) -> None:
        ticket = self.open_test_ticket("NO")
        self.observe("HALF")
        settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual(Decimal(self.settlement(ticket)["net_pnl"]), Decimal("1"))
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("1001"))

    def test_waiting_tickets_report_reason_and_age(self) -> None:
        cases = (
            (dict(outcome=None, status="posed", cross_check="NOT_APPLICABLE"),
             PendingReason.AWAITING_RESOLUTION, None),
            (dict(outcome="YES", cross_check="UNCHECKED"),
             PendingReason.AWAITING_CONFIRMATION, timedelta(days=10)),
            (dict(outcome="UNKNOWN", cross_check="MISMATCH"),
             PendingReason.UNSETTLEABLE, timedelta(days=10)),
        )
        for kwargs, reason, age in cases:
            with self.subTest(reason=reason):
                self.tearDown()
                self.setUp()
                self.open_test_ticket("YES")
                self.observe(**kwargs)  # type: ignore[arg-type]
                summary = settle_open_tickets(self.conn, SETTLE_AT)
                self.assertEqual(summary.settled, 0)
                (item,) = summary.pending
                self.assertEqual(item.reason, reason)
                self.assertEqual(item.age(SETTLE_AT), age)
                self.assertEqual(self.count("settlements"), 0)

    def test_no_observation_awaits_resolution(self) -> None:
        self.open_test_ticket("YES")
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual(summary.count(PendingReason.AWAITING_RESOLUTION), 1)

    def test_latest_observation_by_time_governs(self) -> None:
        self.open_test_ticket("YES")
        self.observe("YES", minutes=0)
        self.observe("YES", minutes=30, cross_check="UNCHECKED")
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual(summary.count(PendingReason.AWAITING_CONFIRMATION), 1)

    def test_delayed_poll_inserted_last_does_not_displace_newer_evidence(self) -> None:
        self.open_test_ticket("YES")
        self.observe("UNKNOWN", minutes=60, cross_check="MISMATCH")
        self.observe("YES", minutes=0)  # older evidence, inserted later (higher id)
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual((summary.settled, summary.count(PendingReason.UNSETTLEABLE)), (0, 1))

    def test_overlapping_polls_with_different_evidence_wait(self) -> None:
        self.open_test_ticket("YES")
        # A slow poll requested at 0 and finished at +10; a fast one ran +2..+3.
        self.observe("UNKNOWN", minutes=3, cross_check="MISMATCH",
                     requested_at=RESOLVED_AT + timedelta(minutes=2))
        self.observe("YES", minutes=10, requested_at=RESOLVED_AT)
        summary = settle_open_tickets(self.conn, SETTLE_AT)
        self.assertEqual(summary.count(PendingReason.AMBIGUOUS_EVIDENCE), 1)
        self.observe("YES", minutes=40)  # a later, non-overlapping confirmation settles
        self.assertEqual(settle_open_tickets(self.conn, SETTLE_AT).settled, 1)

    def test_superseding_observation_committed_before_settlement_wins(self) -> None:
        # Another process records a newer MISMATCH just before settlement takes its lock:
        # the governing observation must be read inside the settlement transaction.
        self.open_test_ticket("YES")
        self.observe("YES")
        other = connect(self.path)
        original = db.transaction

        @contextlib.contextmanager
        def racing_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
            seed_observation(
                other, "UNKNOWN", cross_check="MISMATCH",
                fetched_at=RESOLVED_AT + timedelta(hours=1),
            )
            with original(conn) as inner:
                yield inner

        try:
            with mock.patch("predict_agent.settlement.transaction", racing_transaction):
                summary = settle_open_tickets(self.conn, SETTLE_AT)
        finally:
            other.close()
        self.assertEqual((summary.settled, summary.count(PendingReason.UNSETTLEABLE)), (0, 1))
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("996"))

    def test_settle_is_idempotent(self) -> None:
        self.open_test_ticket("YES")
        self.observe("YES")
        settle_open_tickets(self.conn, SETTLE_AT)
        second = settle_open_tickets(self.conn, SETTLE_AT + timedelta(hours=1))
        self.assertEqual((second.settled, second.pending), (0, ()))
        self.assertEqual(self.count("settlements"), 1)
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("1006"))

    def test_closed_cohort_still_settles_but_cannot_open(self) -> None:
        self.open_test_ticket("YES")
        seed_cohort(self.conn, model_id="m2")
        self.observe("YES")
        self.assertEqual(settle_open_tickets(self.conn, SETTLE_AT).settled, 1)
        self.assertEqual(available_cash(self.conn, self.primary), Decimal("1006"))
        other = "0x" + "8" * 64
        rules = seed_market(self.conn, other)
        with self.assertRaisesRegex(ForecastError, "not active"):
            seed_entry_forecast(self.conn, self.cohort, rules, condition_id=other)

    def test_settlement_rows_are_immutable(self) -> None:
        self.open_test_ticket("YES")
        self.observe("YES")
        settle_open_tickets(self.conn, SETTLE_AT)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE settlements SET payout = '999'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE paper_tickets SET status = 'OPEN'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM resolution_observations")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_settlement -v`
Expected: ERROR `No module named 'predict_agent.settlement'`.

- [ ] **Step 3: Implement**

`src/predict_agent/settlement.py` (create):

```python
"""Settle open paper tickets from official resolutions (spec §4). There is no voiding.

The governing observation for a market is chosen inside the settlement transaction, by
observation time, not insertion order: the one whose resolution fetch finished last.
Each observation covers the interval [resolution_requested_at, resolution_fetched_at];
if any observation with different evidence (status, outcome, cross-check) overlaps the
governing one, the order is ambiguous and the ticket waits. Only a governing observation
that is resolved, CONFIRMED against Gamma and maps to YES/NO/HALF settles."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from .cash import append_cash_entry, money_text, parse_money
from .db import append_journal, transaction
from .util import isoformat, parse_datetime

SETTLEABLE_OUTCOMES = ("YES", "NO", "HALF")


class PendingReason(StrEnum):
    AWAITING_RESOLUTION = "AWAITING_RESOLUTION"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    AMBIGUOUS_EVIDENCE = "AMBIGUOUS_EVIDENCE"
    UNSETTLEABLE = "UNSETTLEABLE"


@dataclass(frozen=True)
class PendingSettlement:
    ticket_id: int
    condition_id: str
    reason: PendingReason
    resolved_since: datetime | None  # first observation reporting `resolved`, if any

    def age(self, now: datetime) -> timedelta | None:
        return None if self.resolved_since is None else now - self.resolved_since


@dataclass(frozen=True)
class SettlementSummary:
    settled: int
    pending: tuple[PendingSettlement, ...]

    def count(self, reason: PendingReason) -> int:
        return sum(1 for item in self.pending if item.reason is reason)


def payout_per_share(held: str, outcome: str) -> Decimal:
    if held not in ("YES", "NO"):
        raise ValueError(f"no payout for holding {held!r}")
    if outcome == "HALF":
        return Decimal("0.5")
    if outcome in ("YES", "NO"):
        return Decimal("1") if held == outcome else Decimal("0")
    raise ValueError(f"no payout for holding {held!r} at outcome {outcome!r}")


def _evidence(row: sqlite3.Row) -> tuple[str, str | None, str]:
    return row["status"], row["outcome"], row["cross_check"]


def _interval(row: sqlite3.Row) -> tuple[datetime, datetime]:
    end = parse_datetime(row["resolution_fetched_at"])
    requested = row["resolution_requested_at"]
    # Rows from before schema v3 carry no request time and are treated as instantaneous.
    return (end if requested is None else parse_datetime(requested)), end


def governing_observation(
    rows: Sequence[sqlite3.Row],
) -> tuple[sqlite3.Row | None, bool]:
    """(governing observation, ambiguous). None when there are no observations."""
    if not rows:
        return None, False
    latest_end = max(_interval(row)[1] for row in rows)
    candidates = [row for row in rows if _interval(row)[1] == latest_end]
    governing = candidates[0]
    if len({_evidence(row) for row in candidates}) > 1:
        return governing, True
    start = _interval(governing)[0]
    for row in rows:
        if _evidence(row) != _evidence(governing) and _interval(row)[1] >= start:
            return governing, True
    return governing, False


def _resolved_since(rows: Sequence[sqlite3.Row]) -> datetime | None:
    times = [_interval(row)[1] for row in rows if row["status"] == "resolved"]
    return min(times) if times else None


def _pending_reason(
    observation: sqlite3.Row | None, ambiguous: bool
) -> PendingReason | None:
    if observation is None:
        return PendingReason.AWAITING_RESOLUTION
    if ambiguous:
        return PendingReason.AMBIGUOUS_EVIDENCE
    if observation["status"] != "resolved":
        return PendingReason.AWAITING_RESOLUTION
    if observation["cross_check"] == "UNCHECKED":
        return PendingReason.AWAITING_CONFIRMATION
    if (
        observation["cross_check"] != "CONFIRMED"
        or observation["outcome"] not in SETTLEABLE_OUTCOMES
    ):
        return PendingReason.UNSETTLEABLE
    return None


def settle_open_tickets(conn: sqlite3.Connection, now: datetime) -> SettlementSummary:
    settled = 0
    pending: list[PendingSettlement] = []
    ticket_ids = [
        row["ticket_id"]
        for row in conn.execute(
            "SELECT ticket_id FROM paper_tickets WHERE status = 'OPEN' ORDER BY ticket_id"
        )
    ]
    for ticket_id in ticket_ids:
        with transaction(conn):
            ticket = conn.execute(
                "SELECT * FROM paper_tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if ticket["status"] != "OPEN":
                continue
            rows = conn.execute(
                "SELECT * FROM resolution_observations WHERE condition_id = ? ORDER BY id",
                (ticket["condition_id"],),
            ).fetchall()
            observation, ambiguous = governing_observation(rows)
            reason = _pending_reason(observation, ambiguous)
            if reason is not None or observation is None:
                pending.append(
                    PendingSettlement(
                        ticket_id,
                        ticket["condition_id"],
                        reason or PendingReason.AWAITING_RESOLUTION,
                        _resolved_since(rows),
                    )
                )
                continue
            per_share = payout_per_share(ticket["outcome"], observation["outcome"])
            payout = parse_money(ticket["shares"]) * per_share
            net_pnl = payout - parse_money(ticket["cost_total"])
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
                    ticket_id,
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
                "UPDATE paper_tickets SET status = 'SETTLED' WHERE ticket_id = ?", (ticket_id,)
            )
            append_cash_entry(conn, ticket["portfolio_id"], "CREDIT", payout, ticket_id, now)
            append_journal(
                conn,
                "TICKET_SETTLED",
                {
                    "ticket_id": ticket_id,
                    "observation_id": observation["id"],
                    "outcome": observation["outcome"],
                    "payout": money_text(payout),
                    "net_pnl": money_text(net_pnl),
                },
                now,
            )
            settled += 1
    return SettlementSummary(settled, tuple(pending))
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_settlement -v` → 13 tests OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/settlement.py tests/predict/test_ledger_settlement.py
git commit -m "feat(predict): settle from the governing confirmed resolution"
```

---

### Task 7: Ledger invariants, `settle` command, `doctor` checks

**Files:**
- Create: `src/predict_agent/invariants.py`, `tests/predict/test_ledger_invariants.py`
- Modify: `src/predict_agent/cli.py`, `CLAUDE.md`

**Interfaces:**
- Consumes: `cash.*`, `cohorts.portfolio_id_for`, `forecasts.forecast_hash`, `tickets.ticket_hash`, `settlement.*`, `db.SCHEMA`, `db.GENESIS_HASH`, `db.verify_journal`, `ledger_schema.LEDGER_SCHEMA`.
- Produces:
  - `invariants.EXPECTED_TRIGGERS` (every trigger named in `SCHEMA + LEDGER_SCHEMA`)
  - `invariants.verify_ledger(conn) -> list[str]` — empty when sound; one line per problem. Hashes: artifacts, cohort identity, forecasts, tickets, per-portfolio cash chains; triggers present. Relationships: portfolios match the cohort identity (ids, policies, bankroll); exactly one first FUNDING equal to the bankroll; running balance never negative; every ticket-linked entry belongs to a ticket of the same portfolio; forecasts link to a `SUCCEEDED` attempt with the same cohort/market/cost and to stored artifacts; timely baselines inside the window; tickets backed by forecast, baseline, portfolio policy and a matching `TRADED` decision; one DEBIT = cost; CREDIT only with a settlement and = payout; settlements backed by a resolved `CONFIRMED` observation of the same market and outcome with correct payout arithmetic; decisions in the forecast's cohort on entry forecasts
  - CLI: `predict-agent settle` (offline; prints settled/pending and one line per pending ticket with reason and age; exit 0); `doctor` exits 3 when the journal is broken **or** `verify_ledger` reports problems


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_ledger_invariants.py` (create):

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

from predict_agent.artifacts import store_artifact
from predict_agent.cash import cash_entry_hash
from predict_agent.cli import main
from predict_agent.db import GENESIS_HASH, connect
from predict_agent.invariants import verify_ledger
from predict_agent.settlement import settle_open_tickets
from predict_agent.tickets import Fill, open_ticket, ticket_hash
from predict_agent.util import isoformat
from tests.predict.fixtures import NOW
from tests.predict.ledger_fixtures import (
    portfolio,
    seed_baselined_forecast,
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_observation,
    seed_snapshot,
    ticket_draft,
)

REPO = Path(__file__).resolve().parents[2]
LATER = NOW + timedelta(seconds=1)
SETTLE_AT = NOW + timedelta(days=30)


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
        self.rules_hash = seed_market(self.conn)
        self.cohort = seed_cohort(self.conn, shadow=True)
        self.primary = portfolio(self.conn, self.cohort)
        self.shadow = portfolio(self.conn, self.cohort, "shadow_mid")
        self.forecast, yes, no = seed_baselined_forecast(self.conn, self.cohort, self.rules_hash)
        self.ticket = open_ticket(
            self.conn,
            ticket_draft(
                self.conn,
                self.primary,
                self.forecast,
                yes,
                fills=(Fill(Decimal("0.40"), Decimal("10")),),
                fee=Decimal("0.02"),
            ),
            LATER,
        )
        self.shadow_ticket = open_ticket(
            self.conn, ticket_draft(self.conn, self.shadow, self.forecast, no, outcome="NO"), LATER
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(argv, root=self.root, now_fn=lambda: SETTLE_AT)
        return code, out.getvalue()

    def assert_problem(self, fragment: str) -> None:
        problems = verify_ledger(self.conn)
        self.assertTrue(any(fragment in p for p in problems), problems)

    def append_raw_cash(self, portfolio_id: str, entry_type: str, amount: str, ticket: int | None):
        """Append a correctly chained entry, bypassing the API's backing checks."""
        last = self.conn.execute(
            "SELECT entry_hash FROM cash_ledger WHERE portfolio_id = ? "
            "ORDER BY entry_id DESC LIMIT 1",
            (portfolio_id,),
        ).fetchone()
        prev_hash = last[0] if last else GENESIS_HASH
        at = isoformat(LATER)
        digest = cash_entry_hash(portfolio_id, entry_type, amount, ticket, at, prev_hash)
        self.conn.execute(
            "INSERT INTO cash_ledger (portfolio_id, entry_type, amount, ticket_id, at, "
            "entry_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (portfolio_id, entry_type, amount, ticket, at, digest),
        )

    def test_sound_ledger_has_no_problems_before_and_after_settlement(self) -> None:
        self.assertEqual(verify_ledger(self.conn), [])
        seed_observation(self.conn, "YES", fetched_at=NOW + timedelta(days=20))
        self.assertEqual(settle_open_tickets(self.conn, SETTLE_AT).settled, 2)
        self.assertEqual(verify_ledger(self.conn), [])

    # Hash tampering (a trigger was dropped first).

    def test_dropped_trigger_is_reported(self) -> None:
        self.conn.execute("DROP TRIGGER cash_no_update")
        self.assert_problem("missing immutability trigger cash_no_update")

    def test_tampered_cash_entry_breaks_chain(self) -> None:
        self.conn.execute("DROP TRIGGER cash_no_update")
        self.conn.execute("UPDATE cash_ledger SET amount = '5000' WHERE entry_type = 'FUNDING'")
        self.assert_problem("cash chain")

    def test_tampered_ticket_is_detected(self) -> None:
        self.conn.execute("DROP TRIGGER tickets_open_to_settled_only")
        self.conn.execute("UPDATE paper_tickets SET cost_total = '0.01'")
        self.assert_problem("ticket hash")
        self.assert_problem("debit")

    def test_altered_prompt_artifact_is_detected(self) -> None:
        self.conn.execute("DROP TRIGGER artifacts_no_update")
        self.conn.execute(
            "UPDATE artifacts SET content = 'Use market prices.' WHERE kind = 'prompt'"
        )
        self.assert_problem("does not match its hash")

    def test_altered_forecast_probability_is_detected(self) -> None:
        self.conn.execute("DROP TRIGGER forecasts_no_update")
        self.conn.execute("UPDATE forecasts SET p_mid = '0.90'")
        self.assert_problem("forecast hash")

    def test_settled_without_settlement_is_detected(self) -> None:
        self.conn.execute("DROP TRIGGER tickets_open_to_settled_only")
        self.conn.execute("UPDATE paper_tickets SET status = 'SETTLED'")
        self.assert_problem("without a settlement")

    # Every hash valid, relationship broken.

    def test_second_funding_is_detected(self) -> None:
        self.conn.execute("DROP INDEX one_funding_per_portfolio")
        self.append_raw_cash(self.primary, "FUNDING", "1000", None)
        self.assert_problem("exactly one first FUNDING")

    def test_credit_against_open_ticket_is_detected(self) -> None:
        self.append_raw_cash(self.primary, "CREDIT", "10", self.ticket)
        self.assert_problem("credit without a settlement")

    def test_entry_attributed_to_another_portfolios_ticket_is_detected(self) -> None:
        self.append_raw_cash(self.primary, "CREDIT", "0", self.shadow_ticket)
        self.assert_problem("ticket of another portfolio")

    def test_ticket_with_another_policy_and_valid_hash_is_detected(self) -> None:
        other = store_artifact(self.conn, "policy", '{"min_edge": "0.01"}', NOW)
        self.conn.execute("DROP TRIGGER tickets_open_to_settled_only")
        row = dict(
            self.conn.execute(
                "SELECT * FROM paper_tickets WHERE ticket_id = ?", (self.ticket,)
            ).fetchone()
        )
        row["policy_hash"] = other
        self.conn.execute(
            "UPDATE paper_tickets SET policy_hash = ?, ticket_hash = ? WHERE ticket_id = ?",
            (other, ticket_hash(row), self.ticket),
        )
        problems = verify_ledger(self.conn)
        self.assertFalse(any("ticket hash" in p for p in problems), problems)
        self.assert_problem("frozen policy")

    def test_wrong_settlement_arithmetic_is_detected(self) -> None:
        seed_observation(self.conn, "YES", fetched_at=NOW + timedelta(days=20))
        settle_open_tickets(self.conn, SETTLE_AT)
        self.conn.execute("DROP TRIGGER settlements_no_update")
        self.conn.execute(
            "UPDATE settlements SET payout_per_share = '0.5' WHERE ticket_id = ?", (self.ticket,)
        )
        self.assert_problem("payout arithmetic")

    def test_settlement_from_an_unconfirmed_observation_is_detected(self) -> None:
        seed_observation(self.conn, "YES", fetched_at=NOW + timedelta(days=20))
        settle_open_tickets(self.conn, SETTLE_AT)
        unchecked = seed_observation(
            self.conn, "YES", cross_check="UNCHECKED", fetched_at=NOW + timedelta(days=21)
        )
        self.conn.execute("DROP TRIGGER settlements_no_update")
        self.conn.execute("UPDATE settlements SET observation_id = ?", (unchecked,))
        self.assert_problem("not backed by a confirmed resolution")

    def test_late_baseline_inserted_directly_is_detected(self) -> None:
        other = "0x" + "6" * 64
        rules = seed_market(self.conn, other)
        forecast = seed_entry_forecast(self.conn, self.cohort, rules, condition_id=other)
        late = NOW + timedelta(minutes=31)
        self.conn.execute(
            "INSERT INTO forecast_baselines (forecast_id, yes_snapshot_id, no_snapshot_id, "
            "reason, attached_at) VALUES (?, ?, ?, NULL, ?)",
            (
                forecast,
                seed_snapshot(self.conn, "YES", late, other),
                seed_snapshot(self.conn, "NO", late, other),
                isoformat(late),
            ),
        )
        self.assert_problem("outside the baseline window")

    # Commands.

    def test_settle_command_runs_offline_and_reports_pending_age(self) -> None:
        seed_observation(
            self.conn, "YES", cross_check="UNCHECKED", fetched_at=NOW + timedelta(days=20)
        )
        code, output = self.run_cli(["settle"])
        self.assertEqual(code, 0, output)
        self.assertIn("settled 0; pending 2", output)
        self.assertIn("AWAITING_CONFIRMATION (resolved 10 days, 0:00:00 ago)", output)

    def test_doctor_fails_on_ledger_problem(self) -> None:
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)
        self.conn.execute("DROP TRIGGER forecasts_no_update")
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 3)
        self.assertIn("missing immutability trigger forecasts_no_update", output)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_invariants -v`
Expected: ERROR `No module named 'predict_agent.invariants'`.

- [ ] **Step 3: Implement**

`src/predict_agent/invariants.py` (create):

```python
"""Ledger invariant checks used by `predict-agent doctor`. Triggers make tampering hard;
these checks make it visible if it happens anyway. They verify every hash (artifacts,
cohort identities, forecasts, tickets, cash chains) and every accounting relationship
between records, independently of the code paths that wrote them."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta
from decimal import Decimal

from .cash import available_cash, cash_entry_hash, parse_money
from .cohorts import portfolio_id_for
from .db import GENESIS_HASH, SCHEMA
from .forecasts import forecast_hash
from .ledger_schema import LEDGER_SCHEMA
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


def verify_ledger(conn: sqlite3.Connection) -> list[str]:
    problems = _trigger_problems(conn) + _artifact_problems(conn)
    for cohort in conn.execute("SELECT * FROM cohorts ORDER BY started_at").fetchall():
        problems += _cohort_problems(conn, cohort)
    for portfolio in conn.execute("SELECT * FROM portfolios ORDER BY portfolio_id").fetchall():
        problems += _cash_problems(conn, portfolio)
    for forecast in conn.execute("SELECT * FROM forecasts ORDER BY forecast_id").fetchall():
        problems += _forecast_problems(conn, forecast)
    for ticket in conn.execute("SELECT * FROM paper_tickets ORDER BY ticket_id").fetchall():
        problems += _ticket_problems(conn, ticket)
    problems += _decision_problems(conn)
    return problems
```

Apply to `src/predict_agent/cli.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/src/predict_agent/cli.py b/src/predict_agent/cli.py
index 0845646..9d5d9b7 100644
--- a/src/predict_agent/cli.py
+++ b/src/predict_agent/cli.py
@@ -20,7 +20,9 @@ from .config import ConfigError, DiscoveryConfig, Settings, load_discovery_confi
 from .db import connect, record_refusal, verify_journal
 from .gamma import ParseError
 from .http import FetchError, JsonClient
+from .invariants import verify_ledger
 from .report import render_markdown, shortlist
+from .settlement import settle_open_tickets
 from .util import utc_now


@@ -33,6 +35,7 @@ def _parser() -> argparse.ArgumentParser:
     sub.add_parser("discover", help="discover eligible markets")
     sub.add_parser("snapshot", help="snapshot books for the latest discovery run")
     sub.add_parser("resolve", help="poll resolution state for known markets")
+    sub.add_parser("settle", help="settle open paper tickets from stored resolutions (offline)")
     report = sub.add_parser("report", help="write the shortlist report")
     which = report.add_mutually_exclusive_group(required=True)
     which.add_argument("--run")
@@ -85,11 +88,28 @@ def main(
     if args.command == "doctor":
         conn = connect(settings.database_path)
         try:
-            ok = verify_journal(conn)
+            journal_ok = verify_journal(conn)
+            problems = verify_ledger(conn)
         finally:
             conn.close()
-        print(f"policy {policy_hash[:12]} ok; journal chain {'ok' if ok else 'BROKEN'}")
-        return 0 if ok else 3
+        print(f"policy {policy_hash[:12]} ok; journal chain {'ok' if journal_ok else 'BROKEN'}")
+        for problem in problems:
+            print(f"ledger: {problem}")
+        print(f"ledger: {'ok' if not problems else f'{len(problems)} problem(s)'}")
+        return 0 if journal_ok and not problems else 3
+    if args.command == "settle":
+        now = now_fn()
+        conn = connect(settings.database_path)
+        try:
+            summary = settle_open_tickets(conn, now)
+        finally:
+            conn.close()
+        print(f"settled {summary.settled}; pending {len(summary.pending)}")
+        for item in summary.pending:
+            age = item.age(now)
+            since = "never resolved" if age is None else f"resolved {age} ago"
+            print(f"pending ticket {item.ticket_id} {item.condition_id}: {item.reason} ({since})")
+        return 0
     http = client or JsonClient()
     if args.command == "report":
         run_id = args.run or _latest_discovery_run(settings.database_path)
```

Apply to `CLAUDE.md` (`git apply` accepts this hunk as written):

```diff
diff --git a/CLAUDE.md b/CLAUDE.md
index 71cf15c..954223f 100644
--- a/CLAUDE.md
+++ b/CLAUDE.md
@@ -33,5 +33,6 @@ Paper-first, human-approved investing agent. Flow: ingest (fail-closed snapshots
 - Spec: `docs/superpowers/specs/2026-10-05-polymarket-paper-forecaster-design.md`; plans in `docs/superpowers/plans/`.
 - Separate package: `predict_agent` must never import `japan_agent` (enforced by `tests/predict/test_cli_isolation.py`). `japan_agent` is frozen.
 - Phase 1 has **no execution code**: no wallet, keys, signing, or non-GET HTTP. Never attempt to circumvent Polymarket's UK geoblock; the geoblock check is audit-only.
-- CLI: `PYTHONPATH=src python3 -m predict_agent.cli doctor|discover|snapshot|resolve|report|run-data`. Needs `config/predict-policy.json` (human-owned copy of the `.example`; code never writes it).
+- CLI: `PYTHONPATH=src python3 -m predict_agent.cli doctor|discover|snapshot|resolve|settle|report|run-data`. Needs `config/predict-policy.json` (human-owned copy of the `.example`; code never writes it).
 - Live API quirks pinned in `tests/predict/fixtures.py`: JSON-string list fields, worst-first books on both sides, resolution rows with or without `payouts` (micro-USDC when present), Gamma `/events` offset paging is capped (use `/events/keyset` + `after_cursor`), and the CLOB book `timestamp` behaved as a last-change time in live observation (not documented): store both server and fetch times and never refuse a book only because its timestamp is old. Gamma `/markets` needs repeated `condition_ids` params (comma-joined matches nothing) and returns closed markets only with `closed=true`.
+- Ledger (Plan 2): a cohort is one research identity (prompt, model, research settings, scoring version, baseline window, generation) owning the shared forecasts; each cohort has portfolios (`primary` plus pre-registered shadows like `shadow_mid`), each with its own frozen policy, FUNDING, cash, tickets and decisions. Money is Decimal text summed in Python (never SQL `SUM`); compare stored timestamps parsed, not as strings. Every cash entry must be backed (FUNDING = bankroll, DEBIT = its OPEN ticket's cost, CREDIT = its settlement payout). Write ledger state only through `cohorts`/`forecasts`/`tickets`/`settlement` functions — each commits with its journal entry. Settlement picks the governing resolution by observation time inside its transaction and waits on overlapping contradictory polls. `predict-agent settle` is offline and lists pending tickets with reason and age; `doctor` also runs `verify_ledger` (hashes, triggers and accounting relationships).
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_invariants -v` → 16 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → 291 tests OK. Also run `bash -n scripts/daily.sh scripts/weekly.sh && git diff --check`.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/invariants.py src/predict_agent/cli.py tests/predict/test_ledger_invariants.py CLAUDE.md
git commit -m "feat(predict): ledger hash and relationship invariants, settle command"
```

---

## Self-Review Record

1. **Spec coverage (build-order item 2):** backed cash ledger with never-negative cash (Tasks 1–2, 5–6), immutable artifacts (Task 2), cohorts with portfolio variants and independent funding/closing (Task 3), durable research attempts incl. failed spend, forecasts with required base rate, one entry per market, baselines bound to timely post-forecast snapshots, `NO_TIMELY_BASELINE`, resume (Task 4), per-portfolio tickets with per-level fills, fee, cost, frozen policy, decisions, equity on cost basis (Task 5), settlement incl. HALF, no voiding, evidence ordering and pending reasons with age (Task 6), hash and relationship invariants and ops commands (Task 7). Deferred by design: policy sizing/caps, book walking and the shadow p_mid policy logic (Plan 3 — storage is ready), research/forecast generation, budget enforcement and recovery of `STARTED` attempts (Plan 4 — `research_attempts` is ready), scoring report incl. `rules_changed_since_first_forecast` and pending-settlement ages (Plan 5).
2. **Placeholder scan:** none.
3. **Type consistency:** `LedgerError` family shared via `cash`; `CohortIdentity`, `ForecastRecord`, `TicketDraft`/`Fill`, `PendingSettlement`/`SettlementSummary` names match across tasks and fixtures; all ledger keys are `portfolio_id` except forecasts, baselines and attempts, which are `cohort_id`.
4. **Task order:** each task's tests import only modules from earlier tasks (`ledger_fixtures.ticket_draft` arrives with `tickets` in Task 5).
5. **Extraction check:** the plan was regenerated from the verified worktree and re-extracted task by task onto `f9c3cc6`, running each task's module and the full suite after every task.

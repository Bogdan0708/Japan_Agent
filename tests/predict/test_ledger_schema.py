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

    def test_connect_to_a_current_database_does_not_take_the_write_lock(self) -> None:
        connect(self.path).close()
        holder = sqlite3.connect(self.path, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            conn = connect(self.path)  # would block on the write lock if it began IMMEDIATE
            try:
                self.assertEqual(
                    conn.execute("SELECT version FROM schema_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
            finally:
                conn.close()
        finally:
            holder.execute("ROLLBACK")
            holder.close()

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

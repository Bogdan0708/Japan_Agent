from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from predict_agent.db import append_journal, connect, transaction, verify_journal
from tests.predict.fixtures import NOW


class DbTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "data" / "predict.sqlite3"
        self.conn = connect(self.path)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_permissions(self) -> None:
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_reconnect_is_idempotent(self) -> None:
        connect(self.path).close()
        version = self.conn.execute("SELECT version FROM schema_version").fetchall()
        self.assertEqual([row["version"] for row in version], [1])

    def test_journal_chain_verifies_and_detects_tampering(self) -> None:
        with transaction(self.conn):
            append_journal(self.conn, "A", {"x": 1}, NOW)
            append_journal(self.conn, "B", {"y": "0.10"}, NOW)
        self.assertTrue(verify_journal(self.conn))
        self.conn.execute("DROP TRIGGER journal_no_update")
        self.conn.execute("UPDATE journal SET payload_json = '{\"x\":2}' WHERE seq = 1")
        self.assertFalse(verify_journal(self.conn))

    def test_journal_rows_are_immutable(self) -> None:
        with transaction(self.conn):
            append_journal(self.conn, "A", {}, NOW)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE journal SET kind = 'Z'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM journal")

    def test_transaction_rolls_back_journal_on_error(self) -> None:
        with self.assertRaises(RuntimeError), transaction(self.conn):
            append_journal(self.conn, "A", {}, NOW)
            raise RuntimeError("boom")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()

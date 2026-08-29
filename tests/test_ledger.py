from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from japan_agent.storage import Database


class LedgerImmutabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "agent.sqlite3")
        self.database.initialize()
        self.database.append_event(kind="TEST_ONE", aggregate_id=None, payload={"n": 1})
        self.database.append_event(kind="TEST_TWO", aggregate_id=None, payload={"n": 2})

    def test_update_is_rejected_by_trigger(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute("UPDATE events SET kind = 'TAMPERED' WHERE sequence = 1")

    def test_delete_is_rejected_by_trigger(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.connect() as connection:
                connection.execute("DELETE FROM events WHERE sequence = 1")

    def test_intact_chain_verifies(self) -> None:
        self.assertEqual(self.database.verify_event_chain(), 2)

    def test_tampered_payload_breaks_verification(self) -> None:
        # Simulate an attacker with direct file access who first removes the
        # trigger; the hash chain is the second, independent line of defense.
        with self.database.connect() as connection:
            connection.execute("DROP TRIGGER events_block_update")
            connection.execute(
                "UPDATE events SET payload_json = '{\"n\": 99}' WHERE sequence = 1"
            )
        with self.assertRaises(ValueError):
            self.database.verify_event_chain()

    def test_deleted_event_breaks_verification(self) -> None:
        with self.database.connect() as connection:
            connection.execute("DROP TRIGGER events_block_delete")
            connection.execute("DELETE FROM events WHERE sequence = 1")
        with self.assertRaises(ValueError):
            self.database.verify_event_chain()

    def test_reordered_events_break_verification(self) -> None:
        with self.database.connect() as connection:
            connection.execute("DROP TRIGGER events_block_update")
            connection.execute("UPDATE events SET sequence = -1 WHERE sequence = 1")
            connection.execute("UPDATE events SET sequence = 1 WHERE sequence = 2")
            connection.execute("UPDATE events SET sequence = 2 WHERE sequence = -1")
        with self.assertRaises(ValueError):
            self.database.verify_event_chain()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from japan_agent.research.snapshot import ResearchSnapshotAssembler, SnapshotIncomplete
from japan_agent.storage import Database

from .helpers import NOW, snapshot


class ResearchSnapshotGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "agent.sqlite3")
        self.database.initialize()
        self.database.save_snapshot(snapshot())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def record(self, source: str, age: timedelta = timedelta()) -> None:
        observed = NOW - age
        self.database.record_ingest_run(
            source=source,
            completed_at=observed,
            observed_through=observed,
            item_count=0,
        )

    def test_missing_source_fails_closed(self) -> None:
        for source in ("PRICE", "JQUANTS", "EDINET", "TDNET"):
            self.record(source)
        with self.assertRaises(SnapshotIncomplete) as raised:
            ResearchSnapshotAssembler(self.database).assemble(
                whitelist_tickers=["TEST_EQ"], now=NOW
            )
        self.assertIn("NEWS: no successful ingest", str(raised.exception))

    def test_complete_fresh_bundle_passes(self) -> None:
        for source in ("PRICE", "JQUANTS", "EDINET", "TDNET", "NEWS"):
            self.record(source)
        bundle = ResearchSnapshotAssembler(self.database).assemble(
            whitelist_tickers=["TEST_EQ"], now=NOW
        )
        self.assertEqual(bundle["whitelist_tickers"], ["TEST_EQ"])
        self.assertEqual(bundle["prices"][0]["ticker"], "TEST_EQ")

    def test_stale_news_fails_closed(self) -> None:
        for source in ("PRICE", "JQUANTS", "EDINET", "TDNET", "NEWS"):
            self.record(source, timedelta(hours=25) if source == "NEWS" else timedelta())
        with self.assertRaises(SnapshotIncomplete) as raised:
            ResearchSnapshotAssembler(self.database).assemble(
                whitelist_tickers=["TEST_EQ"], now=NOW
            )
        self.assertIn("NEWS: ingest age", str(raised.exception))


if __name__ == "__main__":
    unittest.main()


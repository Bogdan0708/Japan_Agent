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

    def record(
        self,
        source: str,
        age: timedelta = timedelta(),
        *,
        observed_age: timedelta | None = None,
    ) -> None:
        completed = NOW - age
        observed = NOW - (observed_age if observed_age is not None else age)
        self.database.record_ingest_run(
            source=source,
            completed_at=completed,
            observed_through=observed,
            item_count=0,
        )

    def record_all_fresh(self, except_source: str | None = None) -> None:
        for source in ("PRICE", "JQUANTS", "EDINET", "TDNET", "NEWS"):
            if source != except_source:
                self.record(source)

    def assemble(self):
        return ResearchSnapshotAssembler(self.database).assemble(
            whitelist_tickers=["TEST_EQ"], now=NOW
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

    def test_fresh_retrieval_of_old_observations_fails_closed(self) -> None:
        self.record_all_fresh(except_source="NEWS")
        self.record("NEWS", timedelta(), observed_age=timedelta(hours=48))
        with self.assertRaises(SnapshotIncomplete) as raised:
            self.assemble()
        self.assertIn("NEWS: observations end", str(raised.exception))

    def test_implausibly_future_coverage_fails_closed(self) -> None:
        self.record_all_fresh(except_source="EDINET")
        self.record("EDINET", timedelta(), observed_age=-timedelta(days=365))
        with self.assertRaises(SnapshotIncomplete) as raised:
            self.assemble()
        self.assertIn("EDINET: observed_through claims implausible future", str(raised.exception))

    def test_modest_clock_skew_is_tolerated(self) -> None:
        self.record_all_fresh(except_source="EDINET")
        self.record("EDINET", timedelta(), observed_age=-timedelta(minutes=10))
        bundle = self.assemble()
        self.assertEqual(bundle["whitelist_tickers"], ["TEST_EQ"])

    def test_jquants_is_not_exempt_from_the_future_check(self) -> None:
        self.record_all_fresh(except_source="JQUANTS")
        self.record("JQUANTS", timedelta(), observed_age=-timedelta(days=30))
        with self.assertRaises(SnapshotIncomplete):
            self.assemble()


if __name__ == "__main__":
    unittest.main()


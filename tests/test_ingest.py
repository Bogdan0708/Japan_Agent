from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from japan_agent.ingest import normalize_price_gbp
from japan_agent.ingest.prices import NormalizedPriceImporter, normalize_currency_code
from japan_agent.ingest.research_sources import ResearchSourceIngester
from japan_agent.storage import Database


class ManualImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.database = Database(root / "agent.sqlite3")
        self.database.initialize()
        self.path = root / "prices.json"

    def test_invalid_item_leaves_no_partial_import(self) -> None:
        good = {
            "ticker": "TEST_EQ",
            "native_price": "10",
            "native_currency": "GBP",
            "observed_at": "2026-08-28T12:00:00Z",
            "source": "manual",
        }
        bad = dict(good, ticker="TEST2_EQ", native_price="-1")
        self.path.write_text(json.dumps([good, bad]), encoding="utf-8")
        with self.assertRaises(ValueError):
            NormalizedPriceImporter(self.database).import_file(self.path)
        self.assertIsNone(self.database.latest_snapshot("TEST_EQ"))
        self.assertEqual(list(self.database.iter_events()), [])
        self.assertIsNone(self.database.latest_successful_ingest("PRICE"))


class PriceNormalizationTests(unittest.TestCase):
    def test_gbx_is_pence(self) -> None:
        self.assertEqual(normalize_price_gbp(Decimal("523.5"), "GBX"), Decimal("5.235"))

    def test_yahoo_gbp_lowercase_p_is_pence(self) -> None:
        self.assertEqual(normalize_currency_code("GBp"), "GBX")
        self.assertEqual(normalize_price_gbp(Decimal("850"), "GBp"), Decimal("8.5"))

    def test_usd_requires_explicit_fx(self) -> None:
        with self.assertRaises(ValueError):
            normalize_price_gbp(Decimal("100"), "USD")
        self.assertEqual(
            normalize_price_gbp(
                Decimal("100"), "USD", gbp_per_native_unit=Decimal("0.75")
            ),
            Decimal("75.00"),
        )


class EdinetCoverageTests(unittest.TestCase):
    """observed_through must never claim more coverage than could be retrieved."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "agent.sqlite3")
        self.database.initialize()

    def ingest(self, *, filing_date, retrieved_at) -> datetime:
        ResearchSourceIngester(self.database).ingest_edinet_list(
            response={"results": []},
            filing_date=filing_date,
            retrieved_at=retrieved_at,
        )
        run = self.database.latest_successful_ingest("EDINET")
        assert run is not None
        return run["observed_through"]

    def test_historical_date_covers_through_tokyo_end_of_day(self) -> None:
        from datetime import date

        observed = self.ingest(
            filing_date=date(2026, 8, 20),
            retrieved_at=datetime(2026, 8, 28, 6, 0, tzinfo=UTC),
        )
        tokyo_end = datetime(
            2026, 8, 20, 23, 59, 59, 999999, tzinfo=ZoneInfo("Asia/Tokyo")
        ).astimezone(UTC)
        self.assertEqual(observed, tokyo_end)

    def test_current_partial_date_covers_only_through_retrieval(self) -> None:
        from datetime import date

        # 06:15 UTC on the 28th is 15:15 in Tokyo — the 28th is still running,
        # so coverage must stop at the retrieval moment, not end of day.
        retrieved = datetime(2026, 8, 28, 6, 15, tzinfo=UTC)
        observed = self.ingest(filing_date=date(2026, 8, 28), retrieved_at=retrieved)
        self.assertEqual(observed, retrieved)


if __name__ == "__main__":
    unittest.main()

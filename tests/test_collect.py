from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from japan_agent.ingest.collect import WhitelistPriceCollector, load_data_symbols
from japan_agent.models import InstrumentRule, Sleeve
from japan_agent.storage import Database

from .helpers import NOW


def rule(ticker: str, currency: str) -> InstrumentRule:
    return InstrumentRule(
        ticker=ticker,
        display_name=f"{ticker} test instrument",
        sleeve=Sleeve.CORE,
        instrument_type="ETF",
        native_currency=currency,
    )


class FakeSource:
    def __init__(self, closes: dict[str, tuple[Decimal, str, datetime]]):
        self.closes = closes

    def latest_daily_close(self, symbol: str) -> tuple[Decimal, str, datetime]:
        if symbol not in self.closes:
            raise RuntimeError(f"no observation for {symbol}")
        return self.closes[symbol]


class CollectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "agent.sqlite3")
        self.database.initialize()

    def ingest_run_count(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM ingest_runs WHERE source = 'PRICE'"
            ).fetchone()
        return int(row["count"])

    def test_usd_instrument_uses_matched_fx(self) -> None:
        source = FakeSource(
            {
                "SONY": (Decimal("20"), "USD", NOW),
                "USDGBP=X": (Decimal("0.75"), "GBP", NOW - timedelta(days=1)),
            }
        )
        snapshots = WhitelistPriceCollector(self.database, source).collect(
            {"SONY_US_EQ": rule("SONY_US_EQ", "USD")},
            {"SONY_US_EQ": "SONY"},
            source_label="test",
        )
        self.assertEqual(snapshots[0].price_gbp, Decimal("15.000000"))
        stored = self.database.latest_snapshot("SONY_US_EQ")
        assert stored is not None
        self.assertEqual(stored.observed_at, NOW)
        self.assertEqual(self.ingest_run_count(), 1)

    def test_gbx_instrument_needs_no_fx(self) -> None:
        source = FakeSource({"RBOT.L": (Decimal("850"), "GBX", NOW)})
        snapshots = WhitelistPriceCollector(self.database, source).collect(
            {"RBOT_EQ": rule("RBOT_EQ", "GBX")},
            {"RBOT_EQ": "RBOT.L"},
            source_label="test",
        )
        self.assertEqual(snapshots[0].price_gbp, Decimal("8.500000"))

    def test_yahoo_gbp_lowercase_p_is_pence_not_pounds(self) -> None:
        # Yahoo spells London pence "GBp"; uppercasing it to GBP would turn
        # 850p into £850 — a silent 100x error.
        source = FakeSource({"RBOT.L": (Decimal("850"), "GBp", NOW)})
        snapshots = WhitelistPriceCollector(self.database, source).collect(
            {"RBOT_EQ": rule("RBOT_EQ", "GBX")},
            {"RBOT_EQ": "RBOT.L"},
            source_label="test",
        )
        self.assertEqual(snapshots[0].price_gbp, Decimal("8.500000"))
        self.assertEqual(snapshots[0].native_currency, "GBX")

    def test_stale_fx_fails_closed_without_heartbeat(self) -> None:
        source = FakeSource(
            {
                "SONY": (Decimal("20"), "USD", NOW),
                "USDGBP=X": (Decimal("0.75"), "GBP", NOW - timedelta(days=6)),
            }
        )
        with self.assertRaises(ValueError):
            WhitelistPriceCollector(self.database, source).collect(
                {"SONY_US_EQ": rule("SONY_US_EQ", "USD")},
                {"SONY_US_EQ": "SONY"},
                source_label="test",
            )
        self.assertIsNone(self.database.latest_snapshot("SONY_US_EQ"))
        self.assertEqual(self.ingest_run_count(), 0)

    def test_currency_mismatch_fails_closed(self) -> None:
        source = FakeSource({"SONY": (Decimal("20"), "JPY", NOW)})
        with self.assertRaises(ValueError):
            WhitelistPriceCollector(self.database, source).collect(
                {"SONY_US_EQ": rule("SONY_US_EQ", "USD")},
                {"SONY_US_EQ": "SONY"},
                source_label="test",
            )

    def test_unmapped_ticker_fails_closed(self) -> None:
        source = FakeSource({})
        with self.assertRaises(ValueError):
            WhitelistPriceCollector(self.database, source).collect(
                {"SONY_US_EQ": rule("SONY_US_EQ", "USD")}, {}, source_label="test"
            )

    def test_one_bad_instrument_blocks_the_whole_run(self) -> None:
        source = FakeSource({"RBOT.L": (Decimal("850"), "GBX", NOW)})
        with self.assertRaises(RuntimeError):
            WhitelistPriceCollector(self.database, source).collect(
                {
                    "RBOT_EQ": rule("RBOT_EQ", "GBX"),
                    "SONY_US_EQ": rule("SONY_US_EQ", "USD"),
                },
                {"RBOT_EQ": "RBOT.L", "SONY_US_EQ": "SONY"},
                source_label="test",
            )
        self.assertIsNone(self.database.latest_snapshot("RBOT_EQ"))
        self.assertEqual(self.ingest_run_count(), 0)

    def test_data_symbols_file_must_be_nonempty(self) -> None:
        path = Path(self.tmp.name) / "data-symbols.json"
        path.write_text('{"symbols": {}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_data_symbols(path)
        path.write_text('{"symbols": {"RBOT_EQ": "RBOT.L"}}', encoding="utf-8")
        self.assertEqual(load_data_symbols(path), {"RBOT_EQ": "RBOT.L"})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from ..models import InstrumentRule, PriceSnapshot
from ..storage import Database
from .prices import NormalizedPriceImporter

# Daily closes are compared across venues; weekends and holidays make small gaps
# normal, but a wider gap means the FX observation cannot honestly price today's
# equity close.
MAX_FX_SKEW = timedelta(days=4)

GBP_CURRENCIES = {"GBP", "GBX", "GBPENCE"}


class DailyCloseSource(Protocol):
    def latest_daily_close(self, symbol: str) -> tuple[Decimal, str, datetime]: ...


def load_data_symbols(path: Path) -> dict[str, str]:
    """Map exact whitelist tickers to market-data symbols (e.g. T212 ticker -> yfinance symbol).

    The broker ticker and the data-vendor symbol are different namespaces; an
    unmapped ticker fails closed rather than guessing.
    """
    value = json.loads(path.read_text(encoding="utf-8"))
    symbols = value.get("symbols")
    if not isinstance(symbols, dict) or not symbols:
        raise ValueError("data-symbols file must contain a non-empty 'symbols' object")
    return {str(ticker): str(symbol) for ticker, symbol in symbols.items()}


class WhitelistPriceCollector:
    """Collect timestamped daily closes for every whitelisted instrument.

    Fail-closed: any missing mapping, currency mismatch, or stale FX aborts the
    whole run before an ingest heartbeat is recorded, so research cannot run on
    a partially collected picture.
    """

    def __init__(self, database: Database, source: DailyCloseSource):
        self.database = database
        self.source = source
        self.importer = NormalizedPriceImporter(database)

    def collect(
        self,
        whitelist: dict[str, InstrumentRule],
        data_symbols: dict[str, str],
        *,
        source_label: str,
    ) -> list[PriceSnapshot]:
        if not whitelist:
            raise ValueError("verified whitelist is empty")
        items = [
            self._observe(rule, data_symbols, source_label=source_label)
            for rule in whitelist.values()
        ]
        snapshots = [self.importer.import_item(item) for item in items]
        completed_at = max(snapshot.observed_at for snapshot in snapshots)
        self.database.record_ingest_run(
            source="PRICE",
            completed_at=completed_at,
            observed_through=completed_at,
            item_count=len(snapshots),
        )
        return snapshots

    def _observe(
        self, rule: InstrumentRule, data_symbols: dict[str, str], *, source_label: str
    ) -> dict[str, object]:
        symbol = data_symbols.get(rule.ticker)
        if not symbol:
            raise ValueError(f"no market-data symbol is mapped for {rule.ticker}")
        price, currency, observed_at = self.source.latest_daily_close(symbol)
        currency = currency.upper()
        expected = rule.native_currency.upper()
        if currency != expected:
            raise ValueError(
                f"{rule.ticker}: upstream currency {currency} does not match "
                f"whitelisted native currency {expected}"
            )
        item: dict[str, object] = {
            "ticker": rule.ticker,
            "native_price": str(price),
            "native_currency": currency,
            "observed_at": observed_at.isoformat(),
            "source": source_label,
        }
        if currency not in GBP_CURRENCIES:
            item["gbp_per_native_unit"] = str(
                self._gbp_conversion(currency, price_observed_at=observed_at)
            )
        return item

    def _gbp_conversion(self, currency: str, *, price_observed_at: datetime) -> Decimal:
        fx_price, fx_currency, fx_observed_at = self.source.latest_daily_close(
            f"{currency}GBP=X"
        )
        if fx_currency.upper() != "GBP":
            raise ValueError(f"FX pair {currency}GBP quoted in {fx_currency}, not GBP")
        if abs(fx_observed_at - price_observed_at) > MAX_FX_SKEW:
            raise ValueError(
                f"FX observation for {currency} is {abs(fx_observed_at - price_observed_at)} "
                "away from the equity observation; refusing a stale conversion"
            )
        return fx_price

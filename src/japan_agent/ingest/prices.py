from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..models import PriceSnapshot, decimal
from ..storage import Database
from ..time import parse_datetime


def normalize_price_gbp(
    native_price: Decimal,
    native_currency: str,
    *,
    gbp_per_native_unit: Decimal | None = None,
) -> Decimal:
    """Normalize quote currency units to pounds.

    LSE frequently reports GBX (pence), which must be divided by 100. Foreign
    currencies require an explicit timestamp-matched GBP conversion; silently
    treating USD or JPY as GBP is forbidden.
    """
    currency = native_currency.upper()
    if native_price <= 0:
        raise ValueError("native price must be positive")
    if currency == "GBP":
        return native_price
    if currency in {"GBX", "GBPENCE"}:
        return native_price / Decimal("100")
    if gbp_per_native_unit is None or gbp_per_native_unit <= 0:
        raise ValueError(f"a positive GBP conversion is required for {native_currency}")
    return native_price * gbp_per_native_unit


class NormalizedPriceImporter:
    """Import timestamped upstream observations; fetch time is never used as market time."""

    def __init__(self, database: Database):
        self.database = database

    def import_file(self, path: Path) -> list[PriceSnapshot]:
        value = json.loads(path.read_text(encoding="utf-8"))
        items = value if isinstance(value, list) else value.get("prices", [])
        if not isinstance(items, list) or not items:
            raise ValueError("price import must contain a non-empty prices list")
        snapshots: list[PriceSnapshot] = []
        for item in items:
            snapshots.append(self.import_item(item))
        completed_at = max(snapshot.observed_at for snapshot in snapshots)
        self.database.record_ingest_run(
            source="PRICE",
            completed_at=completed_at,
            observed_through=completed_at,
            item_count=len(snapshots),
        )
        return snapshots

    def import_item(self, item: dict[str, Any]) -> PriceSnapshot:
        native_price = decimal(item["native_price"])
        native_currency = str(item["native_currency"]).upper()
        conversion = (
            decimal(item["gbp_per_native_unit"])
            if item.get("gbp_per_native_unit") is not None
            else None
        )
        snapshot = PriceSnapshot(
            ticker=str(item["ticker"]),
            native_price=native_price,
            native_currency=native_currency,
            price_gbp=normalize_price_gbp(
                native_price, native_currency, gbp_per_native_unit=conversion
            ).quantize(Decimal("0.000001")),
            observed_at=parse_datetime(str(item["observed_at"])),
            source=str(item["source"]),
        )
        self.database.save_snapshot(snapshot, raw=item)
        self.database.append_event(
            kind="PRICE_SNAPSHOT_INGESTED",
            aggregate_id=snapshot.ticker,
            payload={
                "ticker": snapshot.ticker,
                "observed_at": snapshot.observed_at,
                "source": snapshot.source,
                "native_currency": snapshot.native_currency,
            },
        )
        return snapshot


class YFinanceDailySource:
    """Optional yfinance adapter used only for research data, never broker execution."""

    def latest_daily_close(self, symbol: str) -> tuple[Decimal, str, datetime]:
        try:
            import yfinance as yf
        except ImportError as error:
            raise RuntimeError("install the 'data' extra to use yfinance") from error
        ticker = yf.Ticker(symbol)
        history = ticker.history(period="5d", interval="1d", auto_adjust=False)
        if history.empty:
            raise RuntimeError(f"yfinance returned no daily observations for {symbol}")
        row = history.dropna(subset=["Close"]).iloc[-1]
        timestamp = history.dropna(subset=["Close"]).index[-1].to_pydatetime()
        if timestamp.tzinfo is None:
            raise RuntimeError("upstream market timestamp lacks timezone information")
        currency = str(ticker.fast_info.get("currency") or "").upper()
        if not currency:
            raise RuntimeError("upstream quote currency is missing")
        return decimal(row["Close"]), currency, timestamp

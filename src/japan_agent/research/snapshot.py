from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..models import canonical_json
from ..storage import Database
from ..time import ensure_utc


class SnapshotIncomplete(RuntimeError):
    pass


SOURCE_MAX_AGES = {
    "PRICE": timedelta(hours=72),
    "JQUANTS": timedelta(days=7),
    "EDINET": timedelta(hours=36),
    "TDNET": timedelta(hours=36),
    "NEWS": timedelta(hours=24),
}

SOURCE_ITEM_LIMITS = {
    "EDINET": 35,
    "TDNET": 30,
    "NEWS": 25,
    "JQUANTS": 10,
}

# Tolerated disagreement between this machine's clock and an upstream
# timestamp. Anything further ahead is a dishonest coverage claim, not skew.
CLOCK_SKEW = timedelta(minutes=15)


class ResearchSnapshotAssembler:
    def __init__(self, database: Database):
        self.database = database

    def assemble(self, *, whitelist_tickers: list[str], now: datetime) -> dict[str, Any]:
        now = ensure_utc(now)
        failures: list[str] = []
        ingest_status: dict[str, Any] = {}
        for source, max_age in SOURCE_MAX_AGES.items():
            run = self.database.latest_successful_ingest(source)
            if run is None:
                failures.append(f"{source}: no successful ingest")
                continue
            age = now - run["completed_at"]
            if age < timedelta(0) or age > max_age:
                failures.append(f"{source}: ingest age {age} exceeds {max_age}")
            # A recent retrieval of old data must not look fresh: the observation
            # window itself has to reach into the freshness horizon. J-Quants is
            # exempt because its free tier is inherently 12 weeks delayed. In the
            # other direction, coverage claimed beyond now + clock skew is a
            # dishonest observed_through, never acceptable for any source.
            observation_lag = now - run["observed_through"]
            if observation_lag < -CLOCK_SKEW:
                failures.append(
                    f"{source}: observed_through claims implausible future coverage "
                    f"({-observation_lag} ahead)"
                )
            elif source != "JQUANTS" and observation_lag > max_age:
                failures.append(
                    f"{source}: observations end {observation_lag} ago, beyond {max_age}"
                )
            ingest_status[source] = run
        prices: list[dict[str, Any]] = []
        for ticker in whitelist_tickers:
            snapshot = self.database.latest_snapshot(ticker)
            if snapshot is None:
                failures.append(f"PRICE: no quote for {ticker}")
            else:
                price_age = now - ensure_utc(snapshot.observed_at)
                if price_age < timedelta(0) or price_age > SOURCE_MAX_AGES["PRICE"]:
                    failures.append(
                        f"PRICE: {ticker} observation age {price_age} exceeds "
                        f"{SOURCE_MAX_AGES['PRICE']}"
                    )
                prices.append(snapshot.to_dict())
        if failures:
            raise SnapshotIncomplete("; ".join(failures))
        # Per-source quotas stop one busy feed (typically EDINET) from crowding
        # every other source out of the model context. No whitelist-ticker filter
        # here: EDINET/J-Quants use Japanese security codes, not T212 tickers.
        items: list[dict[str, Any]] = []
        for source, item_limit in SOURCE_ITEM_LIMITS.items():
            items.extend(
                self.database.recent_research_items(
                    since=now - timedelta(days=120), limit=item_limit, source=source
                )
            )
        items.sort(key=lambda item: str(item["published_at"]), reverse=True)
        bundle = {
            "assembled_at": now,
            "whitelist_tickers": whitelist_tickers,
            "ingest_status": ingest_status,
            "prices": prices,
            "research_items": items,
            "data_notes": [
                "J-Quants free-plan observations are 12 weeks delayed and are historical context.",
                "A successful ingest timestamp does not make an older underlying observation "
                "current.",
                "Raw licensed datasets must not be copied into the public journal.",
            ],
        }
        # Prove the bundle is serializable before it reaches an external model process.
        canonical_json(bundle)
        return bundle


def permitted_citations(bundle: dict[str, Any]) -> list[str]:
    """The exact citation IDs the model was shown in this bundle — nothing else
    may be cited by the resulting decision."""
    references = [f"PRICE:{price['ticker']}" for price in bundle["prices"]]
    references.extend(
        f"{item['source']}:{item['external_id']}" for item in bundle["research_items"]
    )
    return references

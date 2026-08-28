from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..storage import Database
from ..time import parse_datetime


class SourceRequestError(RuntimeError):
    pass


def _parse_edinet_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    return parsed.astimezone(UTC)


def _get_json(
    url: str, *, params: dict[str, str], headers: dict[str, str] | None = None
) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{encoded}",
        headers={
            "Accept": "application/json",
            "User-Agent": "japan-tech-analyst/0.1",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise SourceRequestError("research source request failed") from error
    if not isinstance(value, dict):
        raise SourceRequestError("research source returned a non-object response")
    return value


class JQuantsV2Client:
    """J-Quants v2 local ingest. Free-plan data is 12 weeks delayed."""

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("J-Quants API key is required")
        self.api_key = api_key

    def daily_bars(self, *, code: str, trading_date: date) -> dict[str, Any]:
        return _get_json(
            "https://api.jquants.com/v2/equities/bars/daily",
            params={"code": code, "date": trading_date.strftime("%Y%m%d")},
            headers={"x-api-key": self.api_key},
        )


class EdinetV2Client:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("EDINET API key is required")
        self.api_key = api_key

    def document_list(self, filing_date: date) -> dict[str, Any]:
        return _get_json(
            "https://api.edinet-fsa.go.jp/api/v2/documents.json",
            params={
                "date": filing_date.isoformat(),
                "type": "2",
                "Subscription-Key": self.api_key,
            },
        )


class ResearchSourceIngester:
    def __init__(self, database: Database):
        self.database = database

    def ingest_jquants_daily_bars(
        self, *, response: dict[str, Any], code: str, trading_date: date, retrieved_at: datetime
    ) -> None:
        if not response or "error" in response or "message" in response:
            raise ValueError("J-Quants response does not contain a successful data payload")
        external_id = f"daily-bars:{code}:{trading_date.isoformat()}"
        published = datetime.combine(trading_date, datetime.min.time(), tzinfo=UTC)
        self.database.save_research_item(
            source="JQUANTS",
            external_id=external_id,
            headline=f"J-Quants historical daily bar snapshot for code {code}",
            published_at=published,
            retrieved_at=retrieved_at,
            ticker=code,
            payload={"free_plan_delay_weeks": 12, "response": response},
        )
        self.database.record_ingest_run(
            source="JQUANTS",
            completed_at=retrieved_at,
            observed_through=published,
            item_count=1,
        )

    def ingest_edinet_list(
        self, *, response: dict[str, Any], filing_date: date, retrieved_at: datetime
    ) -> int:
        results = response.get("results", [])
        if not isinstance(results, list):
            raise ValueError("EDINET response results must be a list")
        count = 0
        for item in results:
            if not isinstance(item, dict) or not item.get("docID"):
                continue
            submitted = item.get("submitDateTime") or f"{filing_date.isoformat()}T00:00:00+09:00"
            published_at = _parse_edinet_time(str(submitted))
            self.database.save_research_item(
                source="EDINET",
                external_id=str(item["docID"]),
                headline=str(item.get("docDescription") or "EDINET filing"),
                published_at=published_at,
                retrieved_at=retrieved_at,
                ticker=str(item.get("secCode") or "") or None,
                payload=item,
            )
            count += 1
        observed_through = datetime.combine(filing_date, datetime.max.time(), tzinfo=UTC)
        self.database.record_ingest_run(
            source="EDINET",
            completed_at=retrieved_at,
            observed_through=observed_through,
            item_count=count,
        )
        return count

    def ingest_digest_file(self, path: Path, *, expected_source: str) -> int:
        source = expected_source.upper()
        if source not in {"TDNET", "NEWS"}:
            raise ValueError("digest source must be TDNET or NEWS")
        value = json.loads(path.read_text(encoding="utf-8"))
        generated_at = parse_datetime(str(value["generated_at"]))
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("digest items must be a list")
        count = 0
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("each digest item must be an object")
            headline = str(item["headline"]).strip()
            url = str(item["url"]).strip()
            published_at = parse_datetime(str(item["published_at"]))
            if not headline or not url.startswith("https://"):
                raise ValueError("digest items require a headline and HTTPS source URL")
            external_id = str(item.get("external_id") or hashlib.sha256(url.encode()).hexdigest())
            self.database.save_research_item(
                source=source,
                external_id=external_id,
                headline=headline,
                published_at=published_at,
                retrieved_at=generated_at,
                url=url,
                ticker=str(item.get("ticker") or "") or None,
                payload=item,
            )
            count += 1
        self.database.record_ingest_run(
            source=source,
            completed_at=generated_at,
            observed_through=generated_at,
            item_count=count,
        )
        return count

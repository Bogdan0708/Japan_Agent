from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not allowed")
    return value.astimezone(UTC)


def parse_datetime(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def isoformat(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def from_epoch_ms(value: str | int) -> datetime:
    millis = int(value)
    return datetime.fromtimestamp(millis // 1000, UTC) + timedelta(milliseconds=millis % 1000)


def canonical_json(value: Any) -> str:
    def default(item: Any) -> Any:
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, datetime):
            return isoformat(item)
        raise TypeError(f"cannot serialize {type(item)!r}")

    return json.dumps(value, default=default, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))

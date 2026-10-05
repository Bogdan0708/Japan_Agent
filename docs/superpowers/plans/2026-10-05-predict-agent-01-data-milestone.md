# predict-agent Plan 1 — Data Milestone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the read-only data layer of `predict_agent`: discover eligible Polymarket child markets, record immutable rules versions, take order-book snapshots, poll resolution state, and produce a shortlist report with eligible-market counts after every exclusion — with no Claude calls, no trading, and no wallet code.

**Architecture:** New stdlib-only package `src/predict_agent/`, no imports from `japan_agent`. Pure parsing/eligibility functions (`gamma.py`, `clob.py`, `resolution.py`) are separated from I/O (`http.py`) and persistence (`db.py`), and wired together in `collect.py`; `cli.py` exposes `predict-agent`. SQLite holds all state, including a hash-chained journal table written in the same transaction as the state it records.

**Tech Stack:** Python ≥ 3.11 stdlib only (`urllib`, `sqlite3`, `decimal`, `json`, `argparse`), `unittest`, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-10-05-polymarket-paper-forecaster-design.md` (§3 data flow stages 1, 3 and 6; §4 data model; §7 error handling; §10 build order item 1).

**Plan series.** The spec's build order has five milestones. This is Plan 1. Plans 2–5 (ledger; policy + paper fills; research layer; full report + cron) are written after this plan lands, because they depend on the contract facts this milestone pins in code and fixtures.

## Global Constraints

- Core code is stdlib-only; `predict_agent` must never import `japan_agent` (spec §3).
- Money and prices use `Decimal` only, never float; timestamps are timezone-aware UTC; a book's observation time (`observed_at`) is never conflated with fetch time (`fetched_at`).
- Only unauthenticated HTTP GETs. Bounded retries: 3 retries with exponential backoff and jitter on timeouts, network errors, 408/429/5xx; honour `Retry-After`; any other 4xx or malformed body fails that request immediately (spec §7).
- **No wallet, private key, signing or order-submission code** anywhere in `predict_agent` (spec §7).
- Never attempt to circumvent geoblocking; `GET https://polymarket.com/api/geoblock` is recorded for audit on every run and never blocks a read-only run.
- `config/predict-policy.json` is human-edited; code reads it, never writes it. Missing → fail closed with a clear message.
- SQLite at `data/predict.sqlite3`; directory mode 0700, file mode 0600 (spec §4).
- Fail closed with a reason code; an empty shortlist is a valid outcome (spec §7).
- Canonical test run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -v` — zero network, resource leaks are errors.
- Ruff line length 100, target py311; mypy strict.

## Contract facts verified live on 2026-10-05 (pinned by fixtures in Task 2)

These were observed from `gamma-api.polymarket.com`, `clob.polymarket.com` and `data-api.polymarket.com` from the user's UK connection. Where docs and live data disagree, code follows live data and fails closed on anything else.

1. Gamma `/events?active=true&closed=false&tag_slug=<slug>&limit=&offset=` paginates with `offset`. **Active events contain closed child markets** (526 of 2,020 in a sample) — eligibility is evaluated per child market.
2. Gamma market fields used: `conditionId`, `question`, `description` (the rules text), `resolutionSource` (often `""`), `endDate` (ISO-8601 `Z`), `version` (`"v1"` in all 2,020 sampled), `closed`, `acceptingOrders`, `negRisk`, `outcomes` and `clobTokenIds` (**JSON-encoded strings**), `liquidityNum` (number), `bestBid`/`bestAsk` (numbers), `feesEnabled`, `feeSchedule` (`{"rate":0.04,"exponent":1,"takerOnly":true,"rebateRate":0.25}` or `null`), `orderPriceMinTickSize`, `orderMinSize`. Market-level `category` may be `null`; category comes from event `tags[].slug`.
3. Docs say V2 markets use `positionIds` instead of `clobTokenIds`. Phase 1 supports `version == "v1"` only; anything else is refused `UNSUPPORTED_VERSION`.
4. CLOB `/book?token_id=` returns `market` (condition id), `asset_id`, `timestamp` (epoch **milliseconds**, string), `hash`, `bids`/`asks` as `{"price":"0.01","size":"265.22"}` strings, `tick_size`, `min_order_size`, `neg_risk`, `last_trade_price`. **Both sides arrive worst-price first** (bids ascending, asks descending). Sizes show 2 decimal places. **`timestamp` is the time of the book's last change, not the response time**: a smoke run found 254 of 1,568 books with `timestamp` > 120 s old, and re-fetching one returned the same `hash` with the age growing 40 s → 42 s. A quiet book is current as of `fetched_at`. Freshness is therefore measured from `fetched_at` to time of use (enforced in Plan 3 via `max_book_age_seconds`); Plan 1 records `observed_at` (= last change) and refuses only a timestamp in the future (`CLOCK_SKEW`).
5. `orderMinSize` unit is contradictory in docs (Gamma page: USDC; CLOB guide: shares). Plan 3 must satisfy both; this milestone only records it.
6. Data API `/v2/resolutions?condition=<id,...>` (≤ 20 ids) returns `{"data":[...]}`. An open market: `status:"posed"`, `price:"69"` (unset sentinel). A resolved market: `status:"resolved"`, `price:"0"`, **no `payouts`, no `resolved_at`**, `last_update_timestamp` as epoch-seconds string. UMA prices: `"1000000000000000000"` = YES, `"0"` = NO, `"500000000000000000"` = 50/50. `new_version_q` means the rules were updated after posing.
7. Gamma `/markets?condition_ids=<id>` returns a closed market **only with `closed=true`**; its `outcomePrices` is `'["0", "1"]'` for a NO resolution. Used as a cross-check for the resolution outcome.
8. Fee rates per docs: politics 0.04, economics 0.05, geopolitics 0 (fee-free, `feesEnabled:false`, `feeSchedule:null`). Fee-enabled markets carry `feeType` like `"politics_fees"` even when tagged economics — always use the market's own `feeSchedule`.

## Review Focus

1. **A market whose resolution was already proposed while Gamma still says open** — expect it to be refused `RESOLUTION_IN_PROGRESS`, never shortlisted (Task 7 test `test_discover_refuses_market_with_proposed_resolution`).
2. **A rules text edit on an already-known market** — expect a new immutable `rules_versions` row, `current_rules_hash` moved, a `RULES_CHANGED` journal entry, and the old version still present (Task 7 test `test_rules_change_creates_new_version_and_journals`).
3. **Resolution API says NO but Gamma `outcomePrices` says YES** — expect outcome `UNKNOWN`, never a guessed settlement (Task 5 test `test_reconcile_mismatch_is_unknown`).
4. **Retry-After of 3600 seconds on a 429** — expect sleep capped at 30 s, not an hour-long hang (Task 3 test `test_retry_after_is_capped`).
5. **Same market appearing under two tags (politics + geopolitics)** — expect one market row, one category chosen by configured precedence, and one count in the report (Task 7 test `test_event_under_two_tags_is_counted_once`).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/predict_agent/__init__.py` | Package marker and one-line docstring |
| `src/predict_agent/util.py` | UTC time helpers, epoch-ms parsing, canonical JSON, SHA-256 |
| `src/predict_agent/config.py` | `Settings` paths; `DiscoveryConfig` loaded from `config/predict-policy.json` |
| `src/predict_agent/http.py` | `JsonClient`: GET-only JSON client with bounded retries |
| `src/predict_agent/db.py` | SQLite connect/permissions/schema, `transaction()`, hash-chained journal |
| `src/predict_agent/gamma.py` | Parse Gamma markets, rules hashing, category, fee rate, eligibility, event pagination |
| `src/predict_agent/clob.py` | Parse/sort order books, book refusals, book hashing |
| `src/predict_agent/resolution.py` | Parse resolution rows, outcome mapping, Gamma cross-check, batching |
| `src/predict_agent/collect.py` | Orchestrate discover / snapshot / resolve / geoblock against the DB |
| `src/predict_agent/report.py` | Shortlist data + Markdown rendering |
| `src/predict_agent/cli.py` | `predict-agent` argparse entry point |
| `config/predict-policy.example.json` | Committed example; human copies to `predict-policy.json` |
| `tests/predict/__init__.py` | Test package marker |
| `tests/predict/fixtures.py` | Fixtures shaped from verified live responses |
| `tests/predict/fakes.py` | Scripted opener / fake HTTP responses |
| `tests/predict/test_*.py` | One test module per source module |

Modify: `pyproject.toml` (script + mypy package), `.gitignore`, `CLAUDE.md`.

---

### Task 1: Package skeleton, util helpers, config loading

**Files:**
- Create: `src/predict_agent/__init__.py`, `src/predict_agent/util.py`, `src/predict_agent/config.py`, `config/predict-policy.example.json`, `tests/predict/__init__.py`, `tests/predict/test_util_config.py`
- Modify: `pyproject.toml`, `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `util.utc_now() -> datetime`, `util.ensure_utc(datetime) -> datetime`, `util.parse_datetime(str) -> datetime`, `util.isoformat(datetime) -> str`, `util.from_epoch_ms(str | int) -> datetime`, `util.canonical_json(Any) -> str`, `util.sha256_json(Any) -> str`, `util.sha256_text(str) -> str`
  - `config.ConfigError(ValueError)`
  - `config.DiscoveryConfig` (frozen dataclass): `tag_categories: tuple[tuple[str, str], ...]`, `min_liquidity: Decimal`, `min_days_to_end: int`, `max_days_to_end: int`, `known_outcome_threshold: Decimal`, `max_book_age_seconds: int`, `page_size: int`, `max_pages: int`
  - `config.load_discovery_config(path: Path) -> tuple[DiscoveryConfig, str]` (second element = SHA-256 of file text)
  - `config.Settings` (frozen dataclass): `root`, `database_path`, `reports_dir`, `policy_path`; `Settings.from_root(root: Path) -> Settings`

- [ ] **Step 1: Write the failing tests**

`tests/predict/__init__.py`:

```python
```

`tests/predict/test_util_config.py`:

```python
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from predict_agent.config import ConfigError, Settings, load_discovery_config
from predict_agent.util import (
    canonical_json,
    from_epoch_ms,
    isoformat,
    parse_datetime,
    sha256_json,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "predict-policy.example.json"


class UtilTests(unittest.TestCase):
    def test_from_epoch_ms_is_exact_utc(self) -> None:
        self.assertEqual(
            from_epoch_ms("1791210333803"),
            datetime(2026, 10, 5, 14, 25, 33, 803000, tzinfo=UTC),
        )

    def test_parse_and_format_round_trip_z_suffix(self) -> None:
        value = parse_datetime("2026-10-28T03:59:00Z")
        self.assertEqual(isoformat(value), "2026-10-28T03:59:00Z")

    def test_naive_datetime_rejected(self) -> None:
        with self.assertRaises(ValueError):
            isoformat(datetime(2026, 1, 1))

    def test_canonical_json_serialises_decimal_as_exact_string(self) -> None:
        self.assertEqual(canonical_json({"b": Decimal("0.10"), "a": 1}), '{"a":1,"b":"0.10"}')

    def test_sha256_json_is_key_order_independent(self) -> None:
        self.assertEqual(sha256_json({"a": 1, "b": 2}), sha256_json({"b": 2, "a": 1}))


class ConfigTests(unittest.TestCase):
    def test_example_policy_loads(self) -> None:
        config, digest = load_discovery_config(EXAMPLE)
        self.assertEqual(config.tag_categories[0], ("geopolitics", "geopolitics"))
        self.assertEqual(config.known_outcome_threshold, Decimal("0.98"))
        self.assertEqual(len(digest), 64)

    def test_missing_policy_fails_closed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(ConfigError, "predict-policy.example.json"),
        ):
            load_discovery_config(Path(tmp) / "predict-policy.json")

    def test_invalid_window_rejected(self) -> None:
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw["discovery"]["min_days_to_end"] = 100
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "min_days_to_end"):
                load_discovery_config(path)

    def test_empty_tags_rejected(self) -> None:
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw["discovery"]["tag_categories"] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "tag_categories"):
                load_discovery_config(path)

    def test_settings_paths(self) -> None:
        settings = Settings.from_root(Path("/x"))
        self.assertEqual(settings.database_path, Path("/x/data/predict.sqlite3"))
        self.assertEqual(settings.policy_path, Path("/x/config/predict-policy.json"))
        self.assertEqual(settings.reports_dir, Path("/x/data/reports"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_util_config -v`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'predict_agent'`.

- [ ] **Step 3: Write the implementation**

`src/predict_agent/__init__.py`:

```python
"""Paper-only prediction-market forecaster (Phase 1). No execution code lives here."""
```

`src/predict_agent/util.py`:

```python
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
```

`src/predict_agent/config.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .util import sha256_text


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DiscoveryConfig:
    tag_categories: tuple[tuple[str, str], ...]
    min_liquidity: Decimal
    min_days_to_end: int
    max_days_to_end: int
    known_outcome_threshold: Decimal
    max_book_age_seconds: int
    page_size: int
    max_pages: int


@dataclass(frozen=True)
class Settings:
    root: Path
    database_path: Path
    reports_dir: Path
    policy_path: Path

    @classmethod
    def from_root(cls, root: Path) -> Settings:
        return cls(
            root=root,
            database_path=root / "data" / "predict.sqlite3",
            reports_dir=root / "data" / "reports",
            policy_path=root / "config" / "predict-policy.json",
        )


def _decimal(raw: dict[str, Any], key: str) -> Decimal:
    try:
        return Decimal(str(raw[key]))
    except (KeyError, InvalidOperation) as error:
        raise ConfigError(f"discovery.{key} must be a decimal") from error


def _positive_int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"discovery.{key} must be a positive integer")
    return value


def load_discovery_config(path: Path) -> tuple[DiscoveryConfig, str]:
    if not path.exists():
        raise ConfigError(
            f"{path} is missing: copy config/predict-policy.example.json to "
            "config/predict-policy.json and review every value"
        )
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)["discovery"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ConfigError(f"{path} must be JSON with a 'discovery' object") from error
    pairs = raw.get("tag_categories")
    if not isinstance(pairs, list) or not pairs:
        raise ConfigError("discovery.tag_categories must be a non-empty list")
    tag_categories: list[tuple[str, str]] = []
    for pair in pairs:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(item, str) and item for item in pair)
        ):
            raise ConfigError("discovery.tag_categories entries must be [tag_slug, category]")
        tag_categories.append((pair[0], pair[1]))
    config = DiscoveryConfig(
        tag_categories=tuple(tag_categories),
        min_liquidity=_decimal(raw, "min_liquidity"),
        min_days_to_end=_positive_int(raw, "min_days_to_end"),
        max_days_to_end=_positive_int(raw, "max_days_to_end"),
        known_outcome_threshold=_decimal(raw, "known_outcome_threshold"),
        max_book_age_seconds=_positive_int(raw, "max_book_age_seconds"),
        page_size=_positive_int(raw, "page_size"),
        max_pages=_positive_int(raw, "max_pages"),
    )
    if config.min_days_to_end >= config.max_days_to_end:
        raise ConfigError("discovery.min_days_to_end must be below max_days_to_end")
    if not Decimal("0.5") < config.known_outcome_threshold < Decimal("1"):
        raise ConfigError("discovery.known_outcome_threshold must be in (0.5, 1)")
    if config.min_liquidity < 0:
        raise ConfigError("discovery.min_liquidity must not be negative")
    return config, sha256_text(text)
```

`config/predict-policy.example.json`:

```json
{
  "discovery": {
    "tag_categories": [
      ["geopolitics", "geopolitics"],
      ["economics", "economics"],
      ["politics", "politics"]
    ],
    "min_liquidity": "5000",
    "min_days_to_end": 2,
    "max_days_to_end": 90,
    "known_outcome_threshold": "0.98",
    "max_book_age_seconds": 120,
    "page_size": 100,
    "max_pages": 20
  }
}
```

Modify `pyproject.toml` — under `[project.scripts]` add the second line, and change the mypy `packages` line:

```toml
[project.scripts]
japan-agent = "japan_agent.cli:main"
predict-agent = "predict_agent.cli:main"
```

```toml
packages = ["japan_agent", "predict_agent"]
```

Append to `.gitignore`:

```gitignore
# predict_agent runtime state (human-owned policy is never committed)
data/predict.sqlite3*
data/reports/
config/predict-policy.json
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_util_config -v`
Expected: 10 tests, OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`
Expected: no ruff findings; mypy `Success: no issues found`.

```bash
git add src/predict_agent tests/predict config/predict-policy.example.json pyproject.toml .gitignore
git commit -m "feat(predict): package skeleton, util helpers, discovery config"
```

---

### Task 2: Fixtures from verified live shapes, and the scripted HTTP fake

**Files:**
- Create: `tests/predict/fixtures.py`, `tests/predict/fakes.py`, `tests/predict/test_fixtures_contract.py`

**Interfaces:**
- Consumes: nothing from `predict_agent`.
- Produces (used by every later test):
  - `fixtures.NOW: datetime` (2026-10-05 12:00 UTC)
  - `fixtures.gamma_market(**overrides) -> dict[str, Any]`
  - `fixtures.gamma_event(markets: list[dict], tags: tuple[str, ...] = ("politics",), event_id: str = "17526") -> dict[str, Any]`
  - `fixtures.clob_book(**overrides) -> dict[str, Any]`
  - `fixtures.resolution_row(**overrides) -> dict[str, Any]`
  - `fixtures.CONDITION_ID`, `fixtures.YES_TOKEN`, `fixtures.NO_TOKEN`
  - `fakes.FakeResponse(body: bytes)`, `fakes.ScriptedOpener(outcomes: list[object])` with `.requests: list[str]`, `fakes.http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError`, `fakes.RoutedOpener(routes: dict[str, list[object]])` (matches on URL substring, in insertion order)

- [ ] **Step 1: Write the contract test**

`tests/predict/test_fixtures_contract.py`:

```python
from __future__ import annotations

import json
import unittest

from tests.predict.fixtures import clob_book, gamma_event, gamma_market, resolution_row


class FixtureContractTests(unittest.TestCase):
    """Pins the live API shapes observed on 2026-10-05 (see plan 'Contract facts')."""

    def test_gamma_market_encodes_lists_as_json_strings(self) -> None:
        market = gamma_market()
        self.assertIsInstance(market["outcomes"], str)
        self.assertEqual(json.loads(market["outcomes"]), ["Yes", "No"])
        self.assertEqual(len(json.loads(market["clobTokenIds"])), 2)
        self.assertEqual(market["version"], "v1")
        self.assertIsNone(market["category"])

    def test_gamma_event_tags_carry_slugs(self) -> None:
        event = gamma_event([gamma_market()], tags=("politics", "geopolitics"))
        self.assertEqual([tag["slug"] for tag in event["tags"]], ["politics", "geopolitics"])

    def test_clob_book_arrives_worst_first_with_ms_timestamp(self) -> None:
        book = clob_book()
        bids = [level["price"] for level in book["bids"]]
        asks = [level["price"] for level in book["asks"]]
        self.assertEqual(bids, sorted(bids))
        self.assertEqual(asks, sorted(asks, reverse=True))
        self.assertEqual(len(book["timestamp"]), 13)

    def test_resolved_row_has_price_but_no_payouts(self) -> None:
        row = resolution_row(status="resolved", price="0")
        self.assertNotIn("payouts", row)
        self.assertNotIn("resolved_at", row)
        self.assertEqual(row["proposed_price"], "69")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_fixtures_contract -v`
Expected: ERROR `ModuleNotFoundError: No module named 'tests.predict.fixtures'`.

- [ ] **Step 3: Write fixtures and fakes**

`tests/predict/fixtures.py`:

```python
"""Fixtures shaped from live Polymarket responses observed 2026-10-05 (field names,
types and encodings are real; values are trimmed or invented)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CONDITION_ID = "0x5186ed9650f7023d1e0d4ecfd782bff720de56f62c7b7eacfe1af33d3a7cbe1c"
YES_TOKEN = "92583605307183622503994220835856867832397015693472702018901335560560344783116"
NO_TOKEN = "11111111111111111111111111111111111111111111111111111111111111111111111111111"
NOW_MS = "1791201600000"  # == NOW


def gamma_market(**overrides: Any) -> dict[str, Any]:
    market: dict[str, Any] = {
        "id": "702001",
        "conditionId": CONDITION_ID,
        "questionID": "0x29fc63d66c840ff5e6f190f3b7f1eae0f6cb5c3b37b41cc63dad121632c80c4b",
        "question": "Will the IEA ask Germany to release oil stocks by October 31?",
        "description": "This market will resolve to \"Yes\" if the IEA formally asks ...",
        "resolutionSource": "",
        "endDate": "2026-11-01T03:59:00Z",
        "version": "v1",
        "category": None,
        "closed": False,
        "active": True,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "negRisk": False,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.36", "0.64"]',
        "clobTokenIds": json.dumps([YES_TOKEN, NO_TOKEN]),
        "liquidityNum": 76109.0796,
        "bestBid": 0.36,
        "bestAsk": 0.37,
        "feesEnabled": True,
        "feeType": "politics_fees",
        "feeSchedule": {"exponent": 1, "rate": 0.04, "takerOnly": True, "rebateRate": 0.25},
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
        "umaResolutionStatuses": "[]",
    }
    market.update(overrides)
    return market


def gamma_event(
    markets: list[dict[str, Any]],
    tags: tuple[str, ...] = ("politics",),
    event_id: str = "17526",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "title": "IEA oil stocks release",
        "active": True,
        "closed": False,
        "negRisk": False,
        "tags": [
            {"id": str(i), "slug": slug, "label": slug.title()} for i, slug in enumerate(tags)
        ],
        "markets": markets,
    }


def clob_book(**overrides: Any) -> dict[str, Any]:
    book: dict[str, Any] = {
        "market": CONDITION_ID,
        "asset_id": YES_TOKEN,
        "timestamp": NOW_MS,
        "hash": "f1f3a531ef41321b35a0c1ddc6e4ba78556646d7",
        "bids": [
            {"price": "0.30", "size": "100"},
            {"price": "0.35", "size": "12.33"},
            {"price": "0.36", "size": "50"},
        ],
        "asks": [
            {"price": "0.45", "size": "125.08"},
            {"price": "0.38", "size": "57.12"},
            {"price": "0.37", "size": "25"},
        ],
        "tick_size": "0.01",
        "min_order_size": "5",
        "neg_risk": False,
        "last_trade_price": "0.37",
    }
    book.update(overrides)
    return book


def resolution_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "question_id": "0x29fc63d66c840ff5e6f190f3b7f1eae0f6cb5c3b37b41cc63dad121632c80c4b",
        "condition_id": CONDITION_ID,
        "status": "posed",
        "extended_review": False,
        "was_disputed": False,
        "new_version_q": False,
        "proposed_price": "69",
        "reproposed_price": "69",
        "price": "69",
        "transaction_hash": "0x85786708c6034c31d9b76f15047ede9949658742b6d342fce204175230b468ff",
        "log_index": "372",
        "last_update_timestamp": "1790978871",
    }
    row.update(overrides)
    return row
```

`tests/predict/fakes.py`:

```python
from __future__ import annotations

import email.message
import json
import urllib.error
import urllib.request
from typing import Any


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://example.invalid", code, "error", headers, None)


def _respond(item: object) -> FakeResponse:
    if isinstance(item, BaseException):
        raise item
    if isinstance(item, bytes):
        return FakeResponse(item)
    return FakeResponse(json.dumps(item).encode("utf-8"))


class ScriptedOpener:
    """Returns (or raises) each scripted outcome in order, recording requested URLs."""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[str] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any:
        self.requests.append(request.full_url)
        if not self._outcomes:
            raise AssertionError(f"unexpected request {request.full_url}")
        return _respond(self._outcomes.pop(0))


class RoutedOpener:
    """Routes by URL substring (first matching key in insertion order); each route is a queue."""

    def __init__(self, routes: dict[str, list[object]]) -> None:
        self._routes = {key: list(value) for key, value in routes.items()}
        self.requests: list[str] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any:
        url = request.full_url
        self.requests.append(url)
        for key, queue in self._routes.items():
            if key in url:
                if not queue:
                    raise AssertionError(f"route {key!r} exhausted for {url}")
                return _respond(queue.pop(0))
        raise AssertionError(f"no route for {url}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_fixtures_contract -v`
Expected: 4 tests, OK.

- [ ] **Step 5: Commit**

```bash
git add tests/predict/fixtures.py tests/predict/fakes.py tests/predict/test_fixtures_contract.py
git commit -m "test(predict): fixtures pinned to live Polymarket shapes, HTTP fakes"
```

---

### Task 3: GET-only JSON client with bounded retries

**Files:**
- Create: `src/predict_agent/http.py`, `tests/predict/test_http.py`

**Interfaces:**
- Consumes: `tests.predict.fakes` (tests only).
- Produces:
  - `http.FetchError(RuntimeError)` with `.url: str`, `.reason: str`
  - `http.JsonClient(*, opener=None, sleep=time.sleep, jitter=random.random, timeout=20.0, max_retries=3, backoff_base=1.0)`
  - `JsonClient.get(url: str, params: Mapping[str, str | int] | None = None) -> Any`
  - Constant `http.MAX_SLEEP_SECONDS = 30.0`

- [ ] **Step 1: Write the failing tests**

`tests/predict/test_http.py`:

```python
from __future__ import annotations

import unittest
import urllib.error

from predict_agent.http import MAX_SLEEP_SECONDS, FetchError, JsonClient
from tests.predict.fakes import ScriptedOpener, http_error


def client(opener: ScriptedOpener, sleeps: list[float]) -> JsonClient:
    return JsonClient(opener=opener, sleep=sleeps.append, jitter=lambda: 0.0)


class JsonClientTests(unittest.TestCase):
    def test_success_returns_parsed_json_and_encodes_params(self) -> None:
        opener = ScriptedOpener([{"ok": True}])
        result = client(opener, []).get("https://h/x", {"a": "1", "limit": 2})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(opener.requests, ["https://h/x?a=1&limit=2"])

    def test_retries_5xx_then_succeeds_with_exponential_backoff(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([http_error(503), http_error(502), [1]])
        self.assertEqual(client(opener, sleeps).get("https://h/x"), [1])
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_network_error_is_retried(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([urllib.error.URLError("down"), TimeoutError(), {"x": 1}])
        self.assertEqual(client(opener, sleeps).get("https://h/x"), {"x": 1})
        self.assertEqual(len(sleeps), 2)

    def test_retries_are_bounded(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([http_error(500)] * 4)
        with self.assertRaisesRegex(FetchError, "retries exhausted"):
            client(opener, sleeps).get("https://h/x")
        self.assertEqual(len(opener.requests), 4)
        self.assertEqual(len(sleeps), 3)

    def test_non_retryable_4xx_fails_immediately(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([http_error(404)])
        with self.assertRaisesRegex(FetchError, "HTTP 404"):
            client(opener, sleeps).get("https://h/x")
        self.assertEqual(sleeps, [])

    def test_retry_after_header_is_honoured(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([http_error(429, retry_after="7"), {}])
        client(opener, sleeps).get("https://h/x")
        self.assertEqual(sleeps, [7.0])

    def test_retry_after_is_capped(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([http_error(429, retry_after="3600"), {}])
        client(opener, sleeps).get("https://h/x")
        self.assertEqual(sleeps, [MAX_SLEEP_SECONDS])

    def test_malformed_json_fails_without_retry(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([b"<html>blocked</html>"])
        with self.assertRaisesRegex(FetchError, "malformed JSON"):
            client(opener, sleeps).get("https://h/x")
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_http -v`
Expected: ERROR `No module named 'predict_agent.http'`.

- [ ] **Step 3: Implement**

`src/predict_agent/http.py`:

```python
"""GET-only JSON client. There is deliberately no POST/PUT/DELETE support."""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol

USER_AGENT = "predict-agent/0.1 (read-only research)"
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
MAX_SLEEP_SECONDS = 30.0


class FetchError(RuntimeError):
    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"{reason}: {url}")
        self.url = url
        self.reason = reason


class Response(Protocol):
    def read(self) -> bytes: ...

    def __enter__(self) -> Response: ...

    def __exit__(self, *args: object) -> None: ...


Opener = Callable[[urllib.request.Request, float], Response]


def _urlopen(request: urllib.request.Request, timeout: float) -> Response:
    response: Response = urllib.request.urlopen(request, timeout=timeout)
    return response


def _retry_after(header: str | None) -> float | None:
    if header is None:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        return None


class JsonClient:
    def __init__(
        self,
        *,
        opener: Opener | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self._opener: Opener = opener or _urlopen
        self._sleep = sleep
        self._jitter = jitter
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def get(self, url: str, params: Mapping[str, str | int] | None = None) -> Any:
        full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        request = urllib.request.Request(
            full_url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            method="GET",
        )
        for attempt in range(self._max_retries + 1):
            retry_after: float | None = None
            try:
                with self._opener(request, self._timeout) as response:
                    body = response.read()
            except urllib.error.HTTPError as error:
                status = error.code
                header = error.headers.get("Retry-After") if error.headers else None
                error.close()
                if status not in RETRYABLE_STATUS:
                    raise FetchError(full_url, f"HTTP {status}") from None
                retry_after = _retry_after(header)
                reason = f"HTTP {status}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                reason = f"network error {type(error).__name__}"
            else:
                try:
                    return json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise FetchError(full_url, "malformed JSON") from None
            if attempt == self._max_retries:
                raise FetchError(full_url, f"retries exhausted ({reason})")
            delay = (
                retry_after
                if retry_after is not None
                else self._backoff_base * (2**attempt) + self._jitter()
            )
            self._sleep(min(delay, MAX_SLEEP_SECONDS))
        raise AssertionError("unreachable")
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_http -v`
Expected: 8 tests, OK, no ResourceWarning.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/http.py tests/predict/test_http.py
git commit -m "feat(predict): GET-only JSON client with bounded retries"
```

---

### Task 4: Gamma parsing, category, fee rate, eligibility, rules hashing

**Files:**
- Create: `src/predict_agent/gamma.py`, `tests/predict/test_gamma.py`

**Interfaces:**
- Consumes: `config.DiscoveryConfig`, `config.load_discovery_config`; `util.parse_datetime`, `util.isoformat`, `util.sha256_json`; `http.JsonClient`.
- Produces:
  - `gamma.GAMMA_URL = "https://gamma-api.polymarket.com"`
  - `gamma.ParseError(ValueError)`
  - `gamma.MarketCandidate` frozen dataclass: `condition_id: str`, `event_id: str`, `question: str`, `rules_text: str`, `resolution_source: str`, `end_date: datetime | None`, `version: str`, `closed: bool`, `accepting_orders: bool`, `neg_risk: bool`, `outcomes: tuple[str, ...]`, `token_ids: tuple[str, ...]`, `tag_slugs: tuple[str, ...]`, `liquidity: Decimal | None`, `best_bid: Decimal | None`, `best_ask: Decimal | None`, `fees_enabled: bool`, `fee_schedule: dict[str, Any] | None`
  - `gamma.parse_market(raw_market: Mapping[str, Any], raw_event: Mapping[str, Any]) -> MarketCandidate`
  - `gamma.category_for(tag_slugs: tuple[str, ...], config: DiscoveryConfig) -> str | None`
  - `gamma.fee_rate(schedule: object) -> Decimal | None`
  - `gamma.rules_payload(candidate: MarketCandidate) -> dict[str, str | None]`, `gamma.rules_hash(candidate: MarketCandidate) -> str`
  - `gamma.ELIGIBILITY_REASONS: tuple[str, ...]` (check order, used by the report)
  - `gamma.eligibility_refusal(candidate, config, now) -> str | None`
  - `gamma.fetch_events(client: JsonClient, config: DiscoveryConfig) -> list[dict[str, Any]]` (deduplicated by event id, tag order preserved)

- [ ] **Step 1: Write the failing tests**

`tests/predict/test_gamma.py`:

```python
from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from predict_agent.config import load_discovery_config
from predict_agent.gamma import (
    ParseError,
    category_for,
    eligibility_refusal,
    fee_rate,
    fetch_events,
    parse_market,
    rules_hash,
)
from predict_agent.http import JsonClient
from tests.predict.fakes import ScriptedOpener
from tests.predict.fixtures import NOW, YES_TOKEN, gamma_event, gamma_market

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "predict-policy.example.json"
CONFIG, _ = load_discovery_config(EXAMPLE)


def candidate(tags: tuple[str, ...] = ("politics",), **overrides: object):  # type: ignore[no-untyped-def]
    market = gamma_market(**overrides)
    return parse_market(market, gamma_event([market], tags=tags))


class ParseTests(unittest.TestCase):
    def test_parses_json_string_fields_and_decimals(self) -> None:
        c = candidate()
        self.assertEqual(c.outcomes, ("Yes", "No"))
        self.assertEqual(c.token_ids[0], YES_TOKEN)
        self.assertEqual(c.best_ask, Decimal("0.37"))
        self.assertEqual(c.liquidity, Decimal("76109.0796"))
        self.assertEqual(c.tag_slugs, ("politics",))

    def test_missing_condition_id_is_parse_error(self) -> None:
        market = gamma_market()
        del market["conditionId"]
        with self.assertRaises(ParseError):
            parse_market(market, gamma_event([market]))

    def test_non_list_outcomes_is_parse_error(self) -> None:
        with self.assertRaises(ParseError):
            candidate(outcomes='{"a": 1}')

    def test_event_level_neg_risk_propagates(self) -> None:
        market = gamma_market()
        event = gamma_event([market])
        event["negRisk"] = True
        self.assertTrue(parse_market(market, event).neg_risk)


class CategoryAndFeeTests(unittest.TestCase):
    def test_category_precedence_follows_config_order(self) -> None:
        self.assertEqual(category_for(("politics", "geopolitics"), CONFIG), "geopolitics")
        self.assertIsNone(category_for(("sports",), CONFIG))

    def test_fee_rate_requires_exponent_one_and_taker_only(self) -> None:
        good = {"exponent": 1, "rate": 0.04, "takerOnly": True, "rebateRate": 0.25}
        self.assertEqual(fee_rate(good), Decimal("0.04"))
        self.assertIsNone(fee_rate({**good, "exponent": 2}))
        self.assertIsNone(fee_rate({**good, "takerOnly": False}))
        self.assertIsNone(fee_rate({**good, "rate": "abc"}))
        self.assertIsNone(fee_rate(None))


class EligibilityTests(unittest.TestCase):
    def test_eligible_market_has_no_refusal(self) -> None:
        self.assertIsNone(eligibility_refusal(candidate(), CONFIG, NOW))

    def test_refusal_reasons(self) -> None:
        cases = {
            "NOT_OPEN": {"closed": True},
            "UNSUPPORTED_VERSION": {"version": "v2"},
            "NEG_RISK": {"negRisk": True},
            "NON_BINARY": {"outcomes": '["A", "B", "C"]'},
            "NO_RULES": {"description": "   "},
            "LOW_LIQUIDITY": {"liquidityNum": 10},
            "NO_QUOTE": {"bestAsk": None},
            "OUTCOME_KNOWN": {"bestAsk": 0.99, "bestBid": 0.985},
            "FEE_UNKNOWN": {"feeSchedule": None},
        }
        for reason, overrides in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(eligibility_refusal(candidate(**overrides), CONFIG, NOW), reason)

    def test_not_accepting_orders_is_not_open(self) -> None:
        self.assertEqual(
            eligibility_refusal(candidate(acceptingOrders=False), CONFIG, NOW), "NOT_OPEN"
        )

    def test_low_bid_means_no_side_outcome_known(self) -> None:
        refusal = eligibility_refusal(candidate(bestBid=0.01, bestAsk=0.02), CONFIG, NOW)
        self.assertEqual(refusal, "OUTCOME_KNOWN")

    def test_unmapped_tag_has_no_category(self) -> None:
        self.assertEqual(eligibility_refusal(candidate(("sports",)), CONFIG, NOW), "NO_CATEGORY")

    def test_end_window_bounds(self) -> None:
        soon = (NOW + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        far = (NOW + timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for end in (soon, far):
            with self.subTest(end=end):
                self.assertEqual(
                    eligibility_refusal(candidate(endDate=end), CONFIG, NOW), "END_WINDOW"
                )
        self.assertEqual(eligibility_refusal(candidate(endDate=None), CONFIG, NOW), "END_WINDOW")

    def test_fee_free_market_without_schedule_is_eligible(self) -> None:
        c = candidate(("geopolitics",), feesEnabled=False, feeSchedule=None)
        self.assertIsNone(eligibility_refusal(c, CONFIG, NOW))


class RulesHashTests(unittest.TestCase):
    def test_rules_hash_changes_with_description_only(self) -> None:
        base = rules_hash(candidate())
        self.assertEqual(base, rules_hash(candidate(bestAsk=0.5)))
        self.assertNotEqual(base, rules_hash(candidate(description="Clarified wording.")))


class FetchEventsTests(unittest.TestCase):
    def test_paginates_with_offset_and_dedupes_across_tags(self) -> None:
        page_size = CONFIG.page_size
        first_page = [gamma_event([], event_id=str(i)) for i in range(page_size)]
        last_page = [gamma_event([], event_id="last")]
        duplicate = [gamma_event([], event_id="0")]
        opener = ScriptedOpener([first_page, last_page, [], duplicate])
        events = fetch_events(JsonClient(opener=opener, sleep=lambda _: None), CONFIG)
        self.assertEqual(len(events), page_size + 1)
        self.assertIn("offset=100", opener.requests[1])
        self.assertIn("tag_slug=geopolitics", opener.requests[0])
        self.assertIn("tag_slug=economics", opener.requests[2])

    def test_non_list_response_is_parse_error(self) -> None:
        opener = ScriptedOpener([{"error": "x"}])
        with self.assertRaises(ParseError):
            fetch_events(JsonClient(opener=opener, sleep=lambda _: None), CONFIG)


if __name__ == "__main__":
    unittest.main()
```

Note on `test_paginates_with_offset_and_dedupes_across_tags`: config tag order is geopolitics, economics, politics. Requests: geopolitics page 1 (100 events) → page 2 (1 event, fewer than page size, stop) → economics page 1 (empty, stop) → politics page 1 (event "0", a duplicate) → no further request because 1 < page size. Total distinct = 101.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_gamma -v`
Expected: ERROR `No module named 'predict_agent.gamma'`.

- [ ] **Step 3: Implement**

`src/predict_agent/gamma.py`:

```python
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import DiscoveryConfig
from .http import JsonClient
from .util import isoformat, parse_datetime, sha256_json

GAMMA_URL = "https://gamma-api.polymarket.com"
ELIGIBILITY_REASONS = (
    "NOT_OPEN",
    "UNSUPPORTED_VERSION",
    "NEG_RISK",
    "NON_BINARY",
    "NO_CATEGORY",
    "NO_RULES",
    "END_WINDOW",
    "LOW_LIQUIDITY",
    "NO_QUOTE",
    "OUTCOME_KNOWN",
    "FEE_UNKNOWN",
)


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class MarketCandidate:
    condition_id: str
    event_id: str
    question: str
    rules_text: str
    resolution_source: str
    end_date: datetime | None
    version: str
    closed: bool
    accepting_orders: bool
    neg_risk: bool
    outcomes: tuple[str, ...]
    token_ids: tuple[str, ...]
    tag_slugs: tuple[str, ...]
    liquidity: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    fees_enabled: bool
    fee_schedule: dict[str, Any] | None


def _string_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ParseError(f"invalid JSON list: {error}") from None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ParseError("expected a list of strings")
    return tuple(value)


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ParseError("boolean where a number was expected")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise ParseError(f"not a number: {value!r}") from None


def parse_market(raw_market: Mapping[str, Any], raw_event: Mapping[str, Any]) -> MarketCandidate:
    try:
        condition_id = raw_market["conditionId"]
        if not isinstance(condition_id, str) or not condition_id.startswith("0x"):
            raise ParseError("conditionId missing or malformed")
        end_raw = raw_market.get("endDate")
        tags = raw_event.get("tags") or []
        schedule = raw_market.get("feeSchedule")
        return MarketCandidate(
            condition_id=condition_id,
            event_id=str(raw_event["id"]),
            question=str(raw_market.get("question") or ""),
            rules_text=str(raw_market.get("description") or ""),
            resolution_source=str(raw_market.get("resolutionSource") or ""),
            end_date=parse_datetime(end_raw) if isinstance(end_raw, str) and end_raw else None,
            version=str(raw_market.get("version") or ""),
            closed=bool(raw_market.get("closed")),
            accepting_orders=bool(raw_market.get("acceptingOrders")),
            neg_risk=bool(raw_market.get("negRisk")) or bool(raw_event.get("negRisk")),
            outcomes=_string_list(raw_market.get("outcomes")),
            token_ids=_string_list(raw_market.get("clobTokenIds")),
            tag_slugs=tuple(str(tag["slug"]) for tag in tags if isinstance(tag, Mapping)),
            liquidity=_optional_decimal(raw_market.get("liquidityNum")),
            best_bid=_optional_decimal(raw_market.get("bestBid")),
            best_ask=_optional_decimal(raw_market.get("bestAsk")),
            fees_enabled=bool(raw_market.get("feesEnabled")),
            fee_schedule=dict(schedule) if isinstance(schedule, Mapping) else None,
        )
    except KeyError as error:
        raise ParseError(f"missing field {error}") from None
    except ValueError as error:
        if isinstance(error, ParseError):
            raise
        raise ParseError(str(error)) from None


def category_for(tag_slugs: tuple[str, ...], config: DiscoveryConfig) -> str | None:
    for slug, category in config.tag_categories:
        if slug in tag_slugs:
            return category
    return None


def fee_rate(schedule: object) -> Decimal | None:
    if not isinstance(schedule, Mapping):
        return None
    if schedule.get("exponent") != 1 or schedule.get("takerOnly") is not True:
        return None
    try:
        rate = Decimal(str(schedule.get("rate")))
    except InvalidOperation:
        return None
    if not Decimal("0") <= rate < Decimal("1"):
        return None
    return rate


def rules_payload(candidate: MarketCandidate) -> dict[str, str | None]:
    return {
        "question": candidate.question,
        "rules_text": candidate.rules_text,
        "resolution_source": candidate.resolution_source,
        "end_date": isoformat(candidate.end_date) if candidate.end_date else None,
    }


def rules_hash(candidate: MarketCandidate) -> str:
    return sha256_json(rules_payload(candidate))


def eligibility_refusal(
    candidate: MarketCandidate, config: DiscoveryConfig, now: datetime
) -> str | None:
    if candidate.closed or not candidate.accepting_orders:
        return "NOT_OPEN"
    if candidate.version != "v1":
        return "UNSUPPORTED_VERSION"
    if candidate.neg_risk:
        return "NEG_RISK"
    if candidate.outcomes != ("Yes", "No") or len(candidate.token_ids) != 2:
        return "NON_BINARY"
    if category_for(candidate.tag_slugs, config) is None:
        return "NO_CATEGORY"
    if not candidate.rules_text.strip():
        return "NO_RULES"
    if candidate.end_date is None:
        return "END_WINDOW"
    remaining = candidate.end_date - now
    if not (
        timedelta(days=config.min_days_to_end)
        <= remaining
        <= timedelta(days=config.max_days_to_end)
    ):
        return "END_WINDOW"
    if candidate.liquidity is None or candidate.liquidity < config.min_liquidity:
        return "LOW_LIQUIDITY"
    if candidate.best_bid is None or candidate.best_ask is None:
        return "NO_QUOTE"
    threshold = config.known_outcome_threshold
    if candidate.best_ask >= threshold or candidate.best_bid <= Decimal("1") - threshold:
        return "OUTCOME_KNOWN"
    if candidate.fees_enabled and fee_rate(candidate.fee_schedule) is None:
        return "FEE_UNKNOWN"
    return None


def fetch_events(client: JsonClient, config: DiscoveryConfig) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for slug, _category in config.tag_categories:
        for page in range(config.max_pages):
            batch = client.get(
                f"{GAMMA_URL}/events",
                {
                    "active": "true",
                    "closed": "false",
                    "tag_slug": slug,
                    "order": "id",
                    "ascending": "true",
                    "limit": config.page_size,
                    "offset": page * config.page_size,
                },
            )
            if not isinstance(batch, list):
                raise ParseError("Gamma /events did not return a list")
            for event in batch:
                if isinstance(event, dict) and "id" in event:
                    seen.setdefault(str(event["id"]), event)
            if len(batch) < config.page_size:
                break
    return list(seen.values())
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_gamma -v`
Expected: all tests OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/gamma.py tests/predict/test_gamma.py
git commit -m "feat(predict): Gamma parsing, per-child-market eligibility, rules hashing"
```

---

### Task 5: Resolution-state parsing, outcome mapping, Gamma cross-check

**Files:**
- Create: `src/predict_agent/resolution.py`, `tests/predict/test_resolution.py`

**Interfaces:**
- Consumes: `http.JsonClient`; `gamma.GAMMA_URL`, `gamma.ParseError`.
- Produces:
  - `resolution.DATA_API_URL = "https://data-api.polymarket.com"`
  - `resolution.OPEN_STATUSES = frozenset({"initialized", "posed", "active"})`
  - `resolution.ResolutionState` frozen dataclass: `condition_id: str`, `status: str`, `outcome: str | None` (`"YES"|"NO"|"HALF"|"UNKNOWN"` when resolved, `None` otherwise), `was_disputed: bool`, `new_version_q: bool`, `raw: dict[str, Any]`
  - `resolution.parse_resolution(row: Mapping[str, Any]) -> ResolutionState`
  - `resolution.gamma_outcome(outcome_prices: object) -> str | None`
  - `resolution.reconcile_outcome(state: ResolutionState, gamma: str | None) -> str | None`
  - `resolution.resolution_refusal(row: Mapping[str, Any] | None) -> str | None` (`"RESOLUTION_STATE_MISSING"` or `"RESOLUTION_IN_PROGRESS"`)
  - `resolution.fetch_resolutions(client, condition_ids: list[str]) -> dict[str, dict[str, Any]]` (batches of 20)
  - `resolution.fetch_closed_gamma_markets(client, condition_ids: list[str]) -> dict[str, dict[str, Any]]` (passes `closed=true`, batches of 20)

- [ ] **Step 1: Write the failing tests**

`tests/predict/test_resolution.py`:

```python
from __future__ import annotations

import unittest

from predict_agent.http import JsonClient
from predict_agent.resolution import (
    fetch_closed_gamma_markets,
    fetch_resolutions,
    gamma_outcome,
    parse_resolution,
    reconcile_outcome,
    resolution_refusal,
)
from tests.predict.fakes import ScriptedOpener
from tests.predict.fixtures import CONDITION_ID, resolution_row

YES = "1000000000000000000"
HALF = "500000000000000000"


class OutcomeMappingTests(unittest.TestCase):
    def test_open_market_has_no_outcome(self) -> None:
        state = parse_resolution(resolution_row())
        self.assertEqual(state.status, "posed")
        self.assertIsNone(state.outcome)

    def test_uma_price_mapping_when_payouts_absent(self) -> None:
        for price, expected in ((YES, "YES"), ("0", "NO"), (HALF, "HALF"), ("69", "UNKNOWN")):
            with self.subTest(price=price):
                state = parse_resolution(resolution_row(status="resolved", price=price))
                self.assertEqual(state.outcome, expected)

    def test_payouts_take_precedence_when_present(self) -> None:
        cases = (([1_000_000, 0], "YES"), ([0, 1_000_000], "NO"), ([500_000, 500_000], "HALF"))
        for payouts, expected in cases:
            with self.subTest(payouts=payouts):
                row = resolution_row(status="resolved", price="0", payouts=payouts)
                self.assertEqual(parse_resolution(row).outcome, expected)
        odd = resolution_row(status="resolved", payouts=[300_000, 700_000])
        self.assertEqual(parse_resolution(odd).outcome, "UNKNOWN")

    def test_disputed_then_resolved_still_has_outcome(self) -> None:
        state = parse_resolution(resolution_row(status="resolved", price=YES, was_disputed=True))
        self.assertEqual(state.outcome, "YES")
        self.assertTrue(state.was_disputed)

    def test_gamma_outcome(self) -> None:
        self.assertEqual(gamma_outcome('["1", "0"]'), "YES")
        self.assertEqual(gamma_outcome('["0", "1"]'), "NO")
        self.assertEqual(gamma_outcome('["0.5", "0.5"]'), "HALF")
        self.assertIsNone(gamma_outcome('["0.36", "0.64"]'))
        self.assertIsNone(gamma_outcome(None))

    def test_reconcile_agreement_keeps_outcome(self) -> None:
        state = parse_resolution(resolution_row(status="resolved", price="0"))
        self.assertEqual(reconcile_outcome(state, "NO"), "NO")
        self.assertEqual(reconcile_outcome(state, None), "NO")

    def test_reconcile_mismatch_is_unknown(self) -> None:
        state = parse_resolution(resolution_row(status="resolved", price="0"))
        self.assertEqual(reconcile_outcome(state, "YES"), "UNKNOWN")


class RefusalTests(unittest.TestCase):
    def test_refusals(self) -> None:
        self.assertEqual(resolution_refusal(None), "RESOLUTION_STATE_MISSING")
        self.assertIsNone(resolution_refusal(resolution_row(status="posed")))
        for status in ("proposed", "challenged", "reproposed", "disputed", "resolved"):
            with self.subTest(status=status):
                self.assertEqual(
                    resolution_refusal(resolution_row(status=status)), "RESOLUTION_IN_PROGRESS"
                )


class FetchTests(unittest.TestCase):
    def test_fetch_resolutions_batches_twenty(self) -> None:
        ids = [f"0x{i:064x}" for i in range(21)]
        opener = ScriptedOpener(
            [{"data": [resolution_row(condition_id=ids[0])]}, {"data": []}]
        )
        rows = fetch_resolutions(JsonClient(opener=opener, sleep=lambda _: None), ids)
        self.assertEqual(list(rows), [ids[0]])
        self.assertEqual(len(opener.requests), 2)
        self.assertIn("/v2/resolutions?condition=", opener.requests[0])

    def test_fetch_closed_gamma_markets_passes_closed_true(self) -> None:
        opener = ScriptedOpener([[{"conditionId": CONDITION_ID, "outcomePrices": '["0", "1"]'}]])
        rows = fetch_closed_gamma_markets(
            JsonClient(opener=opener, sleep=lambda _: None), [CONDITION_ID]
        )
        self.assertIn(CONDITION_ID, rows)
        self.assertIn("closed=true", opener.requests[0])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_resolution -v`
Expected: ERROR `No module named 'predict_agent.resolution'`.

- [ ] **Step 3: Implement**

`src/predict_agent/resolution.py`:

```python
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .gamma import GAMMA_URL, ParseError
from .http import JsonClient

DATA_API_URL = "https://data-api.polymarket.com"
OPEN_STATUSES = frozenset({"initialized", "posed", "active"})
BATCH = 20
MICRO = 1_000_000
_UMA_PRICES = {
    "1000000000000000000": "YES",
    "0": "NO",
    "500000000000000000": "HALF",
}
_PAYOUTS = {(MICRO, 0): "YES", (0, MICRO): "NO", (MICRO // 2, MICRO // 2): "HALF"}
_GAMMA_PRICES = {("1", "0"): "YES", ("0", "1"): "NO", ("0.5", "0.5"): "HALF"}


@dataclass(frozen=True)
class ResolutionState:
    condition_id: str
    status: str
    outcome: str | None
    was_disputed: bool
    new_version_q: bool
    raw: dict[str, Any]


def _outcome(row: Mapping[str, Any]) -> str | None:
    if row.get("status") != "resolved":
        return None
    payouts = row.get("payouts")
    if isinstance(payouts, list):
        if len(payouts) != 2 or not all(isinstance(p, int) for p in payouts):
            return "UNKNOWN"
        return _PAYOUTS.get((payouts[0], payouts[1]), "UNKNOWN")
    return _UMA_PRICES.get(str(row.get("price")), "UNKNOWN")


def parse_resolution(row: Mapping[str, Any]) -> ResolutionState:
    status = row.get("status")
    condition_id = row.get("condition_id")
    if not isinstance(status, str) or not isinstance(condition_id, str):
        raise ParseError("resolution row lacks status or condition_id")
    return ResolutionState(
        condition_id=condition_id,
        status=status,
        outcome=_outcome(row),
        was_disputed=bool(row.get("was_disputed")),
        new_version_q=bool(row.get("new_version_q")),
        raw=dict(row),
    )


def gamma_outcome(outcome_prices: object) -> str | None:
    if not isinstance(outcome_prices, str):
        return None
    try:
        prices = json.loads(outcome_prices)
    except json.JSONDecodeError:
        return None
    if not isinstance(prices, list) or len(prices) != 2:
        return None
    return _GAMMA_PRICES.get((str(prices[0]), str(prices[1])))


def reconcile_outcome(state: ResolutionState, gamma: str | None) -> str | None:
    if state.outcome in (None, "UNKNOWN") or gamma is None:
        return state.outcome
    return state.outcome if gamma == state.outcome else "UNKNOWN"


def resolution_refusal(row: Mapping[str, Any] | None) -> str | None:
    if row is None:
        return "RESOLUTION_STATE_MISSING"
    if row.get("status") not in OPEN_STATUSES:
        return "RESOLUTION_IN_PROGRESS"
    return None


def _batches(ids: list[str]) -> list[list[str]]:
    return [ids[start : start + BATCH] for start in range(0, len(ids), BATCH)]


def fetch_resolutions(client: JsonClient, condition_ids: list[str]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for batch in _batches(condition_ids):
        payload = client.get(f"{DATA_API_URL}/v2/resolutions", {"condition": ",".join(batch)})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ParseError("/v2/resolutions did not return a data list")
        for row in data:
            if isinstance(row, dict) and isinstance(row.get("condition_id"), str):
                rows[row["condition_id"]] = row
    return rows


def fetch_closed_gamma_markets(
    client: JsonClient, condition_ids: list[str]
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for batch in _batches(condition_ids):
        payload = client.get(
            f"{GAMMA_URL}/markets", {"condition_ids": ",".join(batch), "closed": "true"}
        )
        if not isinstance(payload, list):
            raise ParseError("Gamma /markets did not return a list")
        for market in payload:
            if isinstance(market, dict) and isinstance(market.get("conditionId"), str):
                rows[market["conditionId"]] = market
    return rows
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_resolution -v`
Expected: all OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/resolution.py tests/predict/test_resolution.py
git commit -m "feat(predict): resolution-state mapping with Gamma cross-check"
```

---

### Task 6: Order-book parsing, sorting, refusals; SQLite store and journal

**Files:**
- Create: `src/predict_agent/clob.py`, `src/predict_agent/db.py`, `tests/predict/test_clob.py`, `tests/predict/test_db.py`

**Interfaces:**
- Consumes: `util.*`; `gamma.ParseError`; `http.JsonClient`.
- Produces:
  - `clob.CLOB_URL = "https://clob.polymarket.com"`
  - `clob.Level` frozen dataclass `price: Decimal`, `size: Decimal`
  - `clob.BookSnapshot` frozen dataclass: `condition_id: str`, `token_id: str`, `observed_at: datetime`, `fetched_at: datetime`, `book_hash: str`, `bids: tuple[Level, ...]` (best first = highest), `asks: tuple[Level, ...]` (best first = lowest), `tick_size: Decimal`, `min_order_size: Decimal`; properties `best_bid -> Decimal | None`, `best_ask -> Decimal | None`; method `record() -> dict[str, Any]`
  - `clob.parse_book(raw: Mapping[str, Any], fetched_at: datetime) -> BookSnapshot`
  - `clob.book_refusal(snapshot: BookSnapshot, expected_condition_id: str, expected_token_id: str) -> str | None` (`"BOOK_MISMATCH"`, `"CLOCK_SKEW"`, `"EMPTY_SIDE"`, `"CROSSED_BOOK"`). `observed_at` is the book's last-change time (see Contract fact 4); there is no staleness refusal here.
  - `clob.snapshot_hash(snapshot: BookSnapshot) -> str`
  - `clob.fetch_book(client: JsonClient, token_id: str) -> Any`
  - `db.SCHEMA_VERSION = 1`, `db.GENESIS_HASH = "0" * 64`
  - `db.connect(path: Path) -> sqlite3.Connection` (dir 0700, file 0600, schema applied, `row_factory = sqlite3.Row`, autocommit mode)
  - `db.transaction(conn) -> ContextManager[sqlite3.Connection]` (`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`)
  - `db.append_journal(conn, kind: str, payload: dict[str, Any], at: datetime) -> str`
  - `db.verify_journal(conn) -> bool`
  - `db.record_refusal(conn, run_id: str, condition_id: str | None, stage: str, reason_code: str, detail: str, at: datetime) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/predict/test_clob.py`:

```python
from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from predict_agent.clob import book_refusal, parse_book, snapshot_hash
from predict_agent.gamma import ParseError
from tests.predict.fixtures import CONDITION_ID, NOW, YES_TOKEN, clob_book


def parsed(**overrides: object):  # type: ignore[no-untyped-def]
    return parse_book(clob_book(**overrides), fetched_at=NOW + timedelta(seconds=1))


class ParseBookTests(unittest.TestCase):
    def test_sorts_both_sides_best_first(self) -> None:
        book = parsed()
        self.assertEqual([lvl.price for lvl in book.bids][0], Decimal("0.36"))
        expected_asks = [Decimal(p) for p in ("0.37", "0.38", "0.45")]
        self.assertEqual([lvl.price for lvl in book.asks], expected_asks)
        self.assertEqual(book.best_bid, Decimal("0.36"))
        self.assertEqual(book.best_ask, Decimal("0.37"))

    def test_observed_and_fetched_times_are_distinct(self) -> None:
        book = parsed()
        self.assertEqual(book.observed_at, NOW)
        self.assertEqual(book.fetched_at, NOW + timedelta(seconds=1))

    def test_invalid_levels_rejected(self) -> None:
        bad_levels = (
            {"price": "1.2", "size": "1"},
            {"price": "0.5", "size": "0"},
            {"price": "x", "size": "1"},
        )
        for bad in bad_levels:
            with self.subTest(bad=bad), self.assertRaises(ParseError):
                parsed(bids=[bad])

    def test_missing_timestamp_rejected(self) -> None:
        raw = clob_book()
        del raw["timestamp"]
        with self.assertRaises(ParseError):
            parse_book(raw, fetched_at=NOW)


class RefusalTests(unittest.TestCase):
    def test_fresh_book_has_no_refusal(self) -> None:
        self.assertIsNone(book_refusal(parsed(), CONDITION_ID, YES_TOKEN))

    def test_quiet_book_with_old_last_change_is_accepted(self) -> None:
        quiet = parse_book(clob_book(), fetched_at=NOW + timedelta(hours=6))
        self.assertIsNone(book_refusal(quiet, CONDITION_ID, YES_TOKEN))

    def test_refusals(self) -> None:
        skewed = parse_book(clob_book(), fetched_at=NOW - timedelta(seconds=6))
        cases = {
            "BOOK_MISMATCH": parsed(asset_id="999"),
            "CLOCK_SKEW": skewed,
            "EMPTY_SIDE": parsed(asks=[]),
            "CROSSED_BOOK": parsed(bids=[{"price": "0.40", "size": "5"}]),
        }
        for reason, book in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(book_refusal(book, CONDITION_ID, YES_TOKEN), reason)

    def test_snapshot_hash_is_stable_and_content_bound(self) -> None:
        self.assertEqual(snapshot_hash(parsed()), snapshot_hash(parsed()))
        self.assertNotEqual(snapshot_hash(parsed()), snapshot_hash(parsed(hash="other")))


if __name__ == "__main__":
    unittest.main()
```

`tests/predict/test_db.py`:

```python
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from predict_agent.db import append_journal, connect, transaction, verify_journal
from tests.predict.fixtures import NOW


class DbTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "data" / "predict.sqlite3"
        self.conn = connect(self.path)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_permissions(self) -> None:
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_reconnect_is_idempotent(self) -> None:
        connect(self.path).close()
        version = self.conn.execute("SELECT version FROM schema_version").fetchall()
        self.assertEqual([row["version"] for row in version], [1])

    def test_journal_chain_verifies_and_detects_tampering(self) -> None:
        with transaction(self.conn):
            append_journal(self.conn, "A", {"x": 1}, NOW)
            append_journal(self.conn, "B", {"y": "0.10"}, NOW)
        self.assertTrue(verify_journal(self.conn))
        self.conn.execute("DROP TRIGGER journal_no_update")
        self.conn.execute("UPDATE journal SET payload_json = '{\"x\":2}' WHERE seq = 1")
        self.assertFalse(verify_journal(self.conn))

    def test_journal_rows_are_immutable(self) -> None:
        with transaction(self.conn):
            append_journal(self.conn, "A", {}, NOW)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE journal SET kind = 'Z'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM journal")

    def test_transaction_rolls_back_journal_on_error(self) -> None:
        with self.assertRaises(RuntimeError), transaction(self.conn):
            append_journal(self.conn, "A", {}, NOW)
            raise RuntimeError("boom")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_clob tests.predict.test_db -v`
Expected: ERROR `No module named 'predict_agent.clob'` / `'predict_agent.db'`.

- [ ] **Step 3: Implement**

`src/predict_agent/clob.py`:

```python
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .gamma import ParseError
from .http import JsonClient
from .util import from_epoch_ms, isoformat, sha256_json

CLOB_URL = "https://clob.polymarket.com"
CLOCK_SKEW_TOLERANCE = timedelta(seconds=5)


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class BookSnapshot:
    condition_id: str
    token_id: str
    observed_at: datetime
    fetched_at: datetime
    book_hash: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    tick_size: Decimal
    min_order_size: Decimal

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    def record(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "observed_at": isoformat(self.observed_at),
            "fetched_at": isoformat(self.fetched_at),
            "book_hash": self.book_hash,
            "bids": [[lvl.price, lvl.size] for lvl in self.bids],
            "asks": [[lvl.price, lvl.size] for lvl in self.asks],
            "tick_size": self.tick_size,
            "min_order_size": self.min_order_size,
        }


def _decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise ParseError(f"{field} is not a number: {value!r}") from None


def _levels(raw: object) -> list[Level]:
    if not isinstance(raw, list):
        raise ParseError("book side is not a list")
    levels: list[Level] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ParseError("book level is not an object")
        price = _decimal(item.get("price"), "price")
        size = _decimal(item.get("size"), "size")
        if not Decimal("0") < price < Decimal("1") or size <= 0:
            raise ParseError(f"book level out of range: {price} x {size}")
        levels.append(Level(price, size))
    return levels


def parse_book(raw: Mapping[str, Any], fetched_at: datetime) -> BookSnapshot:
    try:
        timestamp = raw["timestamp"]
        observed_at = from_epoch_ms(timestamp)
    except (KeyError, ValueError, TypeError):
        raise ParseError("book timestamp missing or malformed") from None
    return BookSnapshot(
        condition_id=str(raw.get("market") or ""),
        token_id=str(raw.get("asset_id") or ""),
        observed_at=observed_at,
        fetched_at=fetched_at,
        book_hash=str(raw.get("hash") or ""),
        bids=tuple(sorted(_levels(raw.get("bids")), key=lambda lvl: lvl.price, reverse=True)),
        asks=tuple(sorted(_levels(raw.get("asks")), key=lambda lvl: lvl.price)),
        tick_size=_decimal(raw.get("tick_size"), "tick_size"),
        min_order_size=_decimal(raw.get("min_order_size"), "min_order_size"),
    )


def book_refusal(
    snapshot: BookSnapshot, expected_condition_id: str, expected_token_id: str
) -> str | None:
    # observed_at is the book's last-change time, so an old value only means a quiet
    # book; it is current as of fetched_at. Only a last change in the future is refused.
    if (
        snapshot.condition_id != expected_condition_id
        or snapshot.token_id != expected_token_id
    ):
        return "BOOK_MISMATCH"
    if snapshot.observed_at - snapshot.fetched_at > CLOCK_SKEW_TOLERANCE:
        return "CLOCK_SKEW"
    if not snapshot.bids or not snapshot.asks:
        return "EMPTY_SIDE"
    if snapshot.bids[0].price >= snapshot.asks[0].price:
        return "CROSSED_BOOK"
    return None


def snapshot_hash(snapshot: BookSnapshot) -> str:
    return sha256_json(snapshot.record())


def fetch_book(client: JsonClient, token_id: str) -> Any:
    return client.get(f"{CLOB_URL}/book", {"token_id": token_id})
```

`src/predict_agent/db.py`:

```python
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import canonical_json, isoformat, sha256_json

SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    started_at TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    geoblock_json TEXT
);
CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    question TEXT NOT NULL,
    category TEXT NOT NULL,
    yes_token_id TEXT NOT NULL,
    no_token_id TEXT NOT NULL,
    current_rules_hash TEXT NOT NULL,
    fees_enabled INTEGER NOT NULL,
    fee_schedule_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules_versions (
    rules_hash TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discoveries (
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    PRIMARY KEY (run_id, condition_id)
);
CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('YES', 'NO')),
    observed_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    record_json TEXT NOT NULL,
    fees_enabled INTEGER NOT NULL,
    fee_schedule_json TEXT,
    snapshot_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS resolution_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    was_disputed INTEGER NOT NULL,
    new_version_q INTEGER NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refusals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    condition_id TEXT,
    stage TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    detail TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS rules_no_update BEFORE UPDATE ON rules_versions
BEGIN SELECT RAISE(ABORT, 'rules versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS rules_no_delete BEFORE DELETE ON rules_versions
BEGIN SELECT RAISE(ABORT, 'rules versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON book_snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON book_snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        if not rows:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif [row["version"] for row in rows] != [SCHEMA_VERSION]:
            raise RuntimeError(f"unsupported predict schema version {rows[0]['version']}")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _entry_hash(at: str, kind: str, payload_json: str, prev_hash: str) -> str:
    return sha256_json(
        {"at": at, "kind": kind, "payload": json.loads(payload_json), "prev_hash": prev_hash}
    )


def append_journal(
    conn: sqlite3.Connection, kind: str, payload: dict[str, Any], at: datetime
) -> str:
    row = conn.execute("SELECT entry_hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = row["entry_hash"] if row else GENESIS_HASH
    at_text = isoformat(at)
    payload_json = canonical_json(payload)
    entry_hash = _entry_hash(at_text, kind, payload_json, prev_hash)
    conn.execute(
        "INSERT INTO journal (at, kind, payload_json, prev_hash, entry_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        (at_text, kind, payload_json, prev_hash, entry_hash),
    )
    return entry_hash


def verify_journal(conn: sqlite3.Connection) -> bool:
    prev_hash = GENESIS_HASH
    for row in conn.execute("SELECT * FROM journal ORDER BY seq"):
        if row["prev_hash"] != prev_hash:
            return False
        expected = _entry_hash(row["at"], row["kind"], row["payload_json"], row["prev_hash"])
        if expected != row["entry_hash"]:
            return False
        prev_hash = row["entry_hash"]
    return True


def record_refusal(
    conn: sqlite3.Connection,
    run_id: str,
    condition_id: str | None,
    stage: str,
    reason_code: str,
    detail: str,
    at: datetime,
) -> None:
    conn.execute(
        "INSERT INTO refusals (run_id, condition_id, stage, reason_code, detail, at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, condition_id, stage, reason_code, detail, isoformat(at)),
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_clob tests.predict.test_db -v`
Expected: all OK, no ResourceWarning (every connection is closed in `tearDown` or inline).

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/clob.py src/predict_agent/db.py tests/predict/test_clob.py tests/predict/test_db.py
git commit -m "feat(predict): order-book parsing and SQLite store with hash-chained journal"
```

---

### Task 7: Collection orchestration (discover, snapshot, resolve, geoblock)

**Files:**
- Create: `src/predict_agent/collect.py`, `tests/predict/test_collect.py`

**Interfaces:**
- Consumes: everything above. `gamma.fetch_events`, `gamma.parse_market`, `gamma.eligibility_refusal`, `gamma.category_for`, `gamma.rules_payload`, `gamma.rules_hash`, `resolution.fetch_resolutions`, `resolution.resolution_refusal`, `resolution.parse_resolution`, `resolution.fetch_closed_gamma_markets`, `resolution.gamma_outcome`, `resolution.reconcile_outcome`, `clob.fetch_book`, `clob.parse_book`, `clob.book_refusal`, `clob.snapshot_hash`, `db.*`.
- Produces:
  - `collect.GEOBLOCK_URL = "https://polymarket.com/api/geoblock"`
  - `collect.start_run(conn, command: str, policy_hash: str, now: datetime) -> str` (uuid4 hex run id)
  - `collect.record_geoblock(conn, client, run_id: str) -> dict[str, Any] | None` (never raises on fetch failure; records `{"error": ...}`)
  - `collect.discover(conn, client, config, run_id: str, now: datetime) -> DiscoverySummary`
  - `collect.DiscoverySummary` frozen dataclass: `markets_seen: int`, `eligible: int`, `refusals: dict[str, int]`
  - `collect.snapshot_eligible(conn, client, config, run_id: str, now_fn: Callable[[], datetime]) -> int` (stores both YES and NO books for each market discovered in `run_id`; returns snapshots stored)
  - `collect.poll_resolutions(conn, client, run_id: str, now: datetime) -> int` (for every market in `markets`; returns observations stored)

- [ ] **Step 1: Write the failing tests**

`tests/predict/test_collect.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from predict_agent.collect import (
    discover,
    poll_resolutions,
    record_geoblock,
    snapshot_eligible,
    start_run,
)
from predict_agent.config import load_discovery_config
from predict_agent.db import connect, verify_journal
from predict_agent.http import JsonClient
from tests.predict.fakes import RoutedOpener, http_error
from tests.predict.fixtures import (
    CONDITION_ID,
    NO_TOKEN,
    NOW,
    YES_TOKEN,
    clob_book,
    gamma_event,
    gamma_market,
    resolution_row,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "predict-policy.example.json"
CONFIG, POLICY_HASH = load_discovery_config(EXAMPLE)


def events_routes(
    events_by_tag: dict[str, list[dict[str, Any]]], resolutions: list[dict[str, Any]]
) -> dict[str, list[object]]:
    routes: dict[str, list[object]] = {}
    for slug, _category in CONFIG.tag_categories:
        routes[f"tag_slug={slug}"] = [events_by_tag.get(slug, [])]
    routes["/v2/resolutions"] = [{"data": resolutions}]
    return routes


class CollectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.run_id = start_run(self.conn, "test", POLICY_HASH, NOW)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def client(self, opener: RoutedOpener) -> JsonClient:
        return JsonClient(opener=opener, sleep=lambda _: None, jitter=lambda: 0.0)

    def refusal_codes(self) -> list[str]:
        rows = self.conn.execute("SELECT reason_code FROM refusals ORDER BY id").fetchall()
        return [row["reason_code"] for row in rows]


class DiscoverTests(CollectTestCase):
    def test_eligible_market_is_stored_with_rules_version(self) -> None:
        market = gamma_market()
        opener = RoutedOpener(
            events_routes({"politics": [gamma_event([market])]}, [resolution_row()])
        )
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual((summary.markets_seen, summary.eligible), (1, 1))
        row = self.conn.execute("SELECT * FROM markets").fetchone()
        self.assertEqual(row["category"], "politics")
        self.assertEqual(row["yes_token_id"], YES_TOKEN)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM rules_versions").fetchone()[0], 1)

    def test_closed_child_in_active_event_is_refused(self) -> None:
        closed = gamma_market(conditionId="0x" + "a" * 64, closed=True)
        opener = RoutedOpener(
            events_routes({"politics": [gamma_event([gamma_market(), closed])]}, [resolution_row()])
        )
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual(summary.refusals, {"NOT_OPEN": 1})
        self.assertEqual(summary.eligible, 1)

    def test_discover_refuses_market_with_proposed_resolution(self) -> None:
        opener = RoutedOpener(
            events_routes(
                {"politics": [gamma_event([gamma_market()])]},
                [resolution_row(status="proposed")],
            )
        )
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual(summary.eligible, 0)
        self.assertEqual(self.refusal_codes(), ["RESOLUTION_IN_PROGRESS"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0], 0)

    def test_missing_resolution_row_is_refused(self) -> None:
        opener = RoutedOpener(events_routes({"politics": [gamma_event([gamma_market()])]}, []))
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual(summary.refusals, {"RESOLUTION_STATE_MISSING": 1})

    def test_parse_error_is_refused_not_raised(self) -> None:
        broken = gamma_market(outcomes="not json")
        opener = RoutedOpener(events_routes({"politics": [gamma_event([broken])]}, []))
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual(summary.refusals, {"PARSE_ERROR": 1})

    def test_event_under_two_tags_is_counted_once(self) -> None:
        event = gamma_event([gamma_market()], tags=("politics", "geopolitics"))
        opener = RoutedOpener(
            events_routes({"geopolitics": [event], "politics": [event]}, [resolution_row()])
        )
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual((summary.markets_seen, summary.eligible), (1, 1))
        category = self.conn.execute("SELECT category FROM markets").fetchone()["category"]
        self.assertEqual(category, "geopolitics")

    def test_rules_change_creates_new_version_and_journals(self) -> None:
        first = RoutedOpener(
            events_routes({"politics": [gamma_event([gamma_market()])]}, [resolution_row()])
        )
        discover(self.conn, self.client(first), CONFIG, self.run_id, NOW)
        second_run = start_run(self.conn, "test", POLICY_HASH, NOW)
        changed = gamma_market(description="Clarified: announcements count only if ...")
        second = RoutedOpener(
            events_routes({"politics": [gamma_event([changed])]}, [resolution_row()])
        )
        discover(self.conn, self.client(second), CONFIG, second_run, NOW)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM rules_versions").fetchone()[0], 2)
        kinds = [r["kind"] for r in self.conn.execute("SELECT kind FROM journal ORDER BY seq")]
        self.assertIn("RULES_CHANGED", kinds)
        self.assertTrue(verify_journal(self.conn))


class SnapshotTests(CollectTestCase):
    def discover_one(self) -> None:
        opener = RoutedOpener(
            events_routes({"politics": [gamma_event([gamma_market()])]}, [resolution_row()])
        )
        discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)

    def test_snapshots_both_tokens(self) -> None:
        self.discover_one()
        opener = RoutedOpener(
            {
                f"token_id={YES_TOKEN}": [clob_book()],
                f"token_id={NO_TOKEN}": [clob_book(asset_id=NO_TOKEN)],
            }
        )
        stored = snapshot_eligible(self.conn, self.client(opener), CONFIG, self.run_id, lambda: NOW)
        self.assertEqual(stored, 2)
        outcomes = {r["outcome"] for r in self.conn.execute("SELECT outcome FROM book_snapshots")}
        self.assertEqual(outcomes, {"YES", "NO"})

    def test_bad_book_is_refused_and_other_token_still_stored(self) -> None:
        self.discover_one()
        opener = RoutedOpener(
            {
                f"token_id={YES_TOKEN}": [clob_book(asks=[])],
                f"token_id={NO_TOKEN}": [clob_book(asset_id=NO_TOKEN)],
            }
        )
        stored = snapshot_eligible(self.conn, self.client(opener), CONFIG, self.run_id, lambda: NOW)
        self.assertEqual(stored, 1)
        self.assertEqual(self.refusal_codes(), ["EMPTY_SIDE"])

    def test_fetch_failure_is_refused_not_raised(self) -> None:
        self.discover_one()
        opener = RoutedOpener(
            {
                f"token_id={YES_TOKEN}": [http_error(404)],
                f"token_id={NO_TOKEN}": [clob_book(asset_id=NO_TOKEN)],
            }
        )
        snapshot_eligible(self.conn, self.client(opener), CONFIG, self.run_id, lambda: NOW)
        self.assertEqual(self.refusal_codes(), ["FETCH_ERROR"])


class ResolutionPollTests(CollectTestCase):
    def test_resolved_market_records_cross_checked_outcome(self) -> None:
        opener = RoutedOpener(
            events_routes({"politics": [gamma_event([gamma_market()])]}, [resolution_row()])
        )
        discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        poll = RoutedOpener(
            {
                "/v2/resolutions": [{"data": [resolution_row(status="resolved", price="0")]}],
                "/markets?": [[{"conditionId": CONDITION_ID, "outcomePrices": '["0", "1"]'}]],
            }
        )
        stored = poll_resolutions(self.conn, self.client(poll), self.run_id, NOW)
        self.assertEqual(stored, 1)
        row = self.conn.execute("SELECT * FROM resolution_observations").fetchone()
        self.assertEqual((row["status"], row["outcome"]), ("resolved", "NO"))

    def test_open_market_observation_has_no_outcome_and_no_gamma_call(self) -> None:
        opener = RoutedOpener(
            events_routes({"politics": [gamma_event([gamma_market()])]}, [resolution_row()])
        )
        discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        poll = RoutedOpener({"/v2/resolutions": [{"data": [resolution_row()]}]})
        poll_resolutions(self.conn, self.client(poll), self.run_id, NOW)
        row = self.conn.execute("SELECT * FROM resolution_observations").fetchone()
        self.assertIsNone(row["outcome"])


class GeoblockTests(CollectTestCase):
    def test_geoblock_recorded(self) -> None:
        opener = RoutedOpener({"api/geoblock": [{"blocked": True, "country": "GB"}]})
        result = record_geoblock(self.conn, self.client(opener), self.run_id)
        self.assertEqual(result, {"blocked": True, "country": "GB"})
        stored = self.conn.execute("SELECT geoblock_json FROM runs").fetchone()[0]
        self.assertIn('"country":"GB"', stored)

    def test_geoblock_failure_never_raises(self) -> None:
        opener = RoutedOpener({"api/geoblock": [http_error(403)]})
        self.assertIsNone(record_geoblock(self.conn, self.client(opener), self.run_id))
        self.assertIn("HTTP 403", self.conn.execute("SELECT geoblock_json FROM runs").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
```

Note on `events_routes`: `RoutedOpener` matches the first route key that is a substring of the URL, so `tag_slug=politics` must not also match `tag_slug=geopolitics`. It doesn't, because the parameter string is `tag_slug=geopolitics` vs `tag_slug=politics` — `"tag_slug=politics"` is **not** a substring of `"tag_slug=geopolitics"` (the characters before `politics` differ: `=` vs `o`). Keep that in mind if new tags are added.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_collect -v`
Expected: ERROR `No module named 'predict_agent.collect'`.

- [ ] **Step 3: Implement**

`src/predict_agent/collect.py`:

```python
from __future__ import annotations

import sqlite3
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .clob import book_refusal, fetch_book, parse_book, snapshot_hash
from .config import DiscoveryConfig
from .db import append_journal, record_refusal, transaction
from .gamma import (
    MarketCandidate,
    ParseError,
    category_for,
    eligibility_refusal,
    fetch_events,
    parse_market,
    rules_hash,
    rules_payload,
)
from .http import FetchError, JsonClient
from .resolution import (
    fetch_closed_gamma_markets,
    fetch_resolutions,
    gamma_outcome,
    parse_resolution,
    reconcile_outcome,
    resolution_refusal,
)
from .util import canonical_json, isoformat

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


@dataclass(frozen=True)
class DiscoverySummary:
    markets_seen: int
    eligible: int
    refusals: dict[str, int]


def start_run(conn: sqlite3.Connection, command: str, policy_hash: str, now: datetime) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO runs (run_id, command, started_at, policy_hash) VALUES (?, ?, ?, ?)",
        (run_id, command, isoformat(now), policy_hash),
    )
    return run_id


def record_geoblock(
    conn: sqlite3.Connection, client: JsonClient, run_id: str
) -> dict[str, Any] | None:
    try:
        result = client.get(GEOBLOCK_URL)
        stored: dict[str, Any] = result if isinstance(result, dict) else {"unexpected": result}
        returned: dict[str, Any] | None = stored if isinstance(result, dict) else None
    except FetchError as error:
        stored, returned = {"error": error.reason}, None
    conn.execute(
        "UPDATE runs SET geoblock_json = ? WHERE run_id = ?", (canonical_json(stored), run_id)
    )
    return returned


def _store_market(
    conn: sqlite3.Connection,
    candidate: MarketCandidate,
    category: str,
    run_id: str,
    now: datetime,
) -> None:
    new_hash = rules_hash(candidate)
    now_text = isoformat(now)
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO rules_versions (rules_hash, condition_id, rules_json, "
            "first_seen_at) VALUES (?, ?, ?, ?)",
            (new_hash, candidate.condition_id, canonical_json(rules_payload(candidate)), now_text),
        )
        existing = conn.execute(
            "SELECT current_rules_hash FROM markets WHERE condition_id = ?",
            (candidate.condition_id,),
        ).fetchone()
        schedule = canonical_json(candidate.fee_schedule) if candidate.fee_schedule else None
        if existing is None:
            conn.execute(
                "INSERT INTO markets (condition_id, event_id, question, category, yes_token_id, "
                "no_token_id, current_rules_hash, fees_enabled, fee_schedule_json, "
                "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.condition_id,
                    candidate.event_id,
                    candidate.question,
                    category,
                    candidate.token_ids[0],
                    candidate.token_ids[1],
                    new_hash,
                    int(candidate.fees_enabled),
                    schedule,
                    now_text,
                    now_text,
                ),
            )
            append_journal(
                conn, "MARKET_DISCOVERED", {"condition_id": candidate.condition_id}, now
            )
        else:
            if existing["current_rules_hash"] != new_hash:
                append_journal(
                    conn,
                    "RULES_CHANGED",
                    {
                        "condition_id": candidate.condition_id,
                        "from": existing["current_rules_hash"],
                        "to": new_hash,
                    },
                    now,
                )
            conn.execute(
                "UPDATE markets SET question = ?, category = ?, current_rules_hash = ?, "
                "fees_enabled = ?, fee_schedule_json = ?, last_seen_at = ? "
                "WHERE condition_id = ?",
                (
                    candidate.question,
                    category,
                    new_hash,
                    int(candidate.fees_enabled),
                    schedule,
                    now_text,
                    candidate.condition_id,
                ),
            )
        conn.execute(
            "INSERT OR IGNORE INTO discoveries (run_id, condition_id) VALUES (?, ?)",
            (run_id, candidate.condition_id),
        )


def discover(
    conn: sqlite3.Connection,
    client: JsonClient,
    config: DiscoveryConfig,
    run_id: str,
    now: datetime,
) -> DiscoverySummary:
    refusals: Counter[str] = Counter()
    seen: set[str] = set()
    passing: list[MarketCandidate] = []
    for event in fetch_events(client, config):
        for raw_market in event.get("markets") or []:
            key = str(raw_market.get("conditionId") or id(raw_market))
            if key in seen:
                continue
            seen.add(key)
            try:
                candidate = parse_market(raw_market, event)
            except ParseError as error:
                refusals["PARSE_ERROR"] += 1
                record_refusal(conn, run_id, None, "discover", "PARSE_ERROR", str(error), now)
                continue
            reason = eligibility_refusal(candidate, config, now)
            if reason is not None:
                refusals[reason] += 1
                record_refusal(conn, run_id, candidate.condition_id, "discover", reason, "", now)
                continue
            passing.append(candidate)
    states = fetch_resolutions(client, [c.condition_id for c in passing]) if passing else {}
    eligible = 0
    for candidate in passing:
        reason = resolution_refusal(states.get(candidate.condition_id))
        if reason is not None:
            refusals[reason] += 1
            record_refusal(conn, run_id, candidate.condition_id, "discover", reason, "", now)
            continue
        category = category_for(candidate.tag_slugs, config)
        if category is None:  # unreachable: eligibility already required a category
            raise AssertionError("eligible market without category")
        _store_market(conn, candidate, category, run_id, now)
        eligible += 1
    return DiscoverySummary(markets_seen=len(seen), eligible=eligible, refusals=dict(refusals))


def snapshot_eligible(
    conn: sqlite3.Connection,
    client: JsonClient,
    config: DiscoveryConfig,
    run_id: str,
    now_fn: Callable[[], datetime],
) -> int:
    rows = conn.execute(
        "SELECT m.* FROM markets m JOIN discoveries d ON d.condition_id = m.condition_id "
        "WHERE d.run_id = ? ORDER BY m.condition_id",
        (run_id,),
    ).fetchall()
    stored = 0
    for row in rows:
        for outcome, token_id in (("YES", row["yes_token_id"]), ("NO", row["no_token_id"])):
            condition_id = row["condition_id"]
            try:
                raw = fetch_book(client, token_id)
                fetched_at = now_fn()
                snapshot = parse_book(raw, fetched_at)
            except (FetchError, ParseError) as error:
                code = "FETCH_ERROR" if isinstance(error, FetchError) else "PARSE_ERROR"
                record_refusal(conn, run_id, condition_id, "snapshot", code, str(error), now_fn())
                continue
            reason = book_refusal(snapshot, condition_id, token_id)
            if reason is not None:
                record_refusal(conn, run_id, condition_id, "snapshot", reason, outcome, fetched_at)
                continue
            digest = snapshot_hash(snapshot)
            with transaction(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO book_snapshots (run_id, condition_id, token_id, "
                    "outcome, observed_at, fetched_at, record_json, fees_enabled, "
                    "fee_schedule_json, snapshot_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        condition_id,
                        token_id,
                        outcome,
                        isoformat(snapshot.observed_at),
                        isoformat(snapshot.fetched_at),
                        canonical_json(snapshot.record()),
                        row["fees_enabled"],
                        row["fee_schedule_json"],
                        digest,
                    ),
                )
            stored += 1
    return stored


def poll_resolutions(
    conn: sqlite3.Connection, client: JsonClient, run_id: str, now: datetime
) -> int:
    ids = [r["condition_id"] for r in conn.execute("SELECT condition_id FROM markets ORDER BY 1")]
    if not ids:
        return 0
    rows = fetch_resolutions(client, ids)
    states = {cid: parse_resolution(row) for cid, row in rows.items()}
    resolved = [cid for cid, state in states.items() if state.status == "resolved"]
    gamma = fetch_closed_gamma_markets(client, resolved) if resolved else {}
    stored = 0
    with transaction(conn):
        for condition_id in ids:
            state = states.get(condition_id)
            if state is None:
                record_refusal(
                    conn, run_id, condition_id, "resolve", "RESOLUTION_STATE_MISSING", "", now
                )
                continue
            market = gamma.get(condition_id)
            outcome = reconcile_outcome(
                state, gamma_outcome(market.get("outcomePrices")) if market else None
            )
            conn.execute(
                "INSERT INTO resolution_observations (run_id, condition_id, fetched_at, status, "
                "outcome, was_disputed, new_version_q, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    condition_id,
                    isoformat(now),
                    state.status,
                    outcome,
                    int(state.was_disputed),
                    int(state.new_version_q),
                    canonical_json(state.raw),
                ),
            )
            if outcome == "UNKNOWN":
                append_journal(
                    conn, "RESOLUTION_UNKNOWN", {"condition_id": condition_id}, now
                )
            stored += 1
    return stored
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_collect -v`
Expected: all OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/collect.py tests/predict/test_collect.py
git commit -m "feat(predict): discovery, snapshot and resolution collection"
```

---

### Task 8: Shortlist report

**Files:**
- Create: `src/predict_agent/report.py`, `tests/predict/test_report.py`

**Interfaces:**
- Consumes: `db.connect`, `collect.*` (tests), `gamma.ELIGIBILITY_REASONS`, `gamma.fee_rate`.
- Produces:
  - `report.REASON_ORDER: tuple[str, ...]` = `("PARSE_ERROR",) + ELIGIBILITY_REASONS + ("RESOLUTION_STATE_MISSING", "RESOLUTION_IN_PROGRESS")`
  - `report.shortlist(conn, run_id: str) -> dict[str, Any]` with keys `run_id`, `started_at`, `geoblock`, `markets_seen`, `funnel` (list of `{"reason", "excluded", "remaining"}` in `REASON_ORDER`), `eligible_by_category` (dict), `snapshot_refusals` (dict), `markets` (list of `{"condition_id","question","category","end_date","fee_rate","yes_best_bid","yes_best_ask"}`)
  - `report.render_markdown(data: dict[str, Any]) -> str`

- [ ] **Step 1: Write the failing test**

`tests/predict/test_report.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from predict_agent.collect import discover, snapshot_eligible, start_run
from predict_agent.config import load_discovery_config
from predict_agent.db import connect
from predict_agent.http import JsonClient
from predict_agent.report import render_markdown, shortlist
from tests.predict.fakes import RoutedOpener
from tests.predict.fixtures import (
    NO_TOKEN,
    NOW,
    YES_TOKEN,
    clob_book,
    gamma_event,
    gamma_market,
    resolution_row,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "predict-policy.example.json"
CONFIG, POLICY_HASH = load_discovery_config(EXAMPLE)


class ShortlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.run_id = start_run(self.conn, "test", POLICY_HASH, NOW)
        markets = [
            gamma_market(),
            gamma_market(conditionId="0x" + "b" * 64, closed=True),
            gamma_market(conditionId="0x" + "c" * 64, liquidityNum=1),
        ]
        routes: dict[str, list[object]] = {
            "tag_slug=geopolitics": [[]],
            "tag_slug=economics": [[]],
            "tag_slug=politics": [[gamma_event(markets)]],
            "/v2/resolutions": [{"data": [resolution_row()]}],
            f"token_id={YES_TOKEN}": [clob_book()],
            f"token_id={NO_TOKEN}": [clob_book(asset_id=NO_TOKEN)],
        }
        client = JsonClient(opener=RoutedOpener(routes), sleep=lambda _: None)
        discover(self.conn, client, CONFIG, self.run_id, NOW)
        snapshot_eligible(self.conn, client, CONFIG, self.run_id, lambda: NOW)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_funnel_counts_after_every_exclusion(self) -> None:
        data = shortlist(self.conn, self.run_id)
        self.assertEqual(data["markets_seen"], 3)
        funnel = {step["reason"]: step for step in data["funnel"]}
        self.assertEqual(funnel["NOT_OPEN"]["excluded"], 1)
        self.assertEqual(funnel["NOT_OPEN"]["remaining"], 2)
        self.assertEqual(funnel["LOW_LIQUIDITY"]["remaining"], 1)
        self.assertEqual(data["funnel"][-1]["remaining"], 1)
        self.assertEqual(data["eligible_by_category"], {"politics": 1})

    def test_market_rows_carry_fee_and_yes_quote(self) -> None:
        market = shortlist(self.conn, self.run_id)["markets"][0]
        self.assertEqual(market["fee_rate"], "0.04")
        self.assertEqual(market["yes_best_ask"], "0.37")
        self.assertEqual(market["yes_best_bid"], "0.36")

    def test_markdown_lists_counts_first(self) -> None:
        text = render_markdown(shortlist(self.conn, self.run_id))
        self.assertLess(text.index("Markets seen"), text.index("| Question"))
        self.assertIn("NOT_OPEN", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_report -v`
Expected: ERROR `No module named 'predict_agent.report'`.

- [ ] **Step 3: Implement**

`src/predict_agent/report.py`:

```python
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any

from .gamma import ELIGIBILITY_REASONS, fee_rate

REASON_ORDER: tuple[str, ...] = (
    ("PARSE_ERROR",) + ELIGIBILITY_REASONS + ("RESOLUTION_STATE_MISSING", "RESOLUTION_IN_PROGRESS")
)


def _count_by(conn: sqlite3.Connection, run_id: str, stage: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT reason_code, COUNT(*) AS n FROM refusals WHERE run_id = ? AND stage = ? "
        "GROUP BY reason_code",
        (run_id, stage),
    ).fetchall()
    return {row["reason_code"]: row["n"] for row in rows}


def shortlist(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"unknown run {run_id}")
    discover_refusals = _count_by(conn, run_id, "discover")
    markets = conn.execute(
        "SELECT m.*, r.rules_json FROM markets m "
        "JOIN discoveries d ON d.condition_id = m.condition_id "
        "JOIN rules_versions r ON r.rules_hash = m.current_rules_hash "
        "WHERE d.run_id = ? ORDER BY m.category, m.question",
        (run_id,),
    ).fetchall()
    markets_seen = len(markets) + sum(discover_refusals.values())
    remaining = markets_seen
    funnel = []
    for reason in REASON_ORDER:
        excluded = discover_refusals.get(reason, 0)
        remaining -= excluded
        funnel.append({"reason": reason, "excluded": excluded, "remaining": remaining})
    by_category: dict[str, int] = {}
    rows = []
    for market in markets:
        by_category[market["category"]] = by_category.get(market["category"], 0) + 1
        snap = conn.execute(
            "SELECT record_json FROM book_snapshots WHERE run_id = ? AND condition_id = ? "
            "AND outcome = 'YES' ORDER BY id DESC LIMIT 1",
            (run_id, market["condition_id"]),
        ).fetchone()
        best_bid = best_ask = None
        if snap is not None:
            record = json.loads(snap["record_json"])
            best_bid = record["bids"][0][0] if record["bids"] else None
            best_ask = record["asks"][0][0] if record["asks"] else None
        schedule = json.loads(market["fee_schedule_json"]) if market["fee_schedule_json"] else None
        rate = fee_rate(schedule) if market["fees_enabled"] else Decimal("0")
        rows.append(
            {
                "condition_id": market["condition_id"],
                "question": market["question"],
                "category": market["category"],
                "end_date": json.loads(market["rules_json"])["end_date"],
                "fee_rate": None if rate is None else format(rate, "f"),
                "yes_best_bid": best_bid,
                "yes_best_ask": best_ask,
            }
        )
    return {
        "run_id": run_id,
        "started_at": run["started_at"],
        "geoblock": json.loads(run["geoblock_json"]) if run["geoblock_json"] else None,
        "markets_seen": markets_seen,
        "funnel": funnel,
        "eligible_by_category": by_category,
        "snapshot_refusals": _count_by(conn, run_id, "snapshot"),
        "markets": rows,
    }


def render_markdown(data: dict[str, Any]) -> str:
    lines = [
        f"# Shortlist — run {data['run_id']}",
        "",
        f"Started: {data['started_at']}  ",
        f"Geoblock (audit only): `{json.dumps(data['geoblock'])}`",
        "",
        f"**Markets seen: {data['markets_seen']}** — "
        f"eligible: {data['funnel'][-1]['remaining'] if data['funnel'] else 0}",
        "",
        "| Exclusion | Excluded | Remaining |",
        "|---|---:|---:|",
    ]
    lines += [f"| {s['reason']} | {s['excluded']} | {s['remaining']} |" for s in data["funnel"]]
    lines += ["", "Eligible by category: " + json.dumps(data["eligible_by_category"])]
    lines += ["Snapshot refusals: " + json.dumps(data["snapshot_refusals"]), ""]
    lines += [
        "| Question | Category | Ends | Fee rate | YES bid | YES ask |",
        "|---|---|---|---:|---:|---:|",
    ]
    for m in data["markets"]:
        question = m["question"].replace("|", "\\|")
        lines.append(
            f"| {question} | {m['category']} | {m['end_date']} | {m['fee_rate']} | "
            f"{m['yes_best_bid']} | {m['yes_best_ask']} |"
        )
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_report -v`
Expected: all OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent`

```bash
git add src/predict_agent/report.py tests/predict/test_report.py
git commit -m "feat(predict): shortlist report with exclusion funnel"
```

---

### Task 9: CLI, isolation test, docs, live read-only smoke run

**Files:**
- Create: `src/predict_agent/cli.py`, `tests/predict/test_cli_isolation.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: all modules above.
- Produces: `cli.main(argv: list[str] | None = None, *, client: JsonClient | None = None, root: Path | None = None, now_fn: Callable[[], datetime] = utc_now) -> int`; subcommands `doctor`, `discover`, `snapshot`, `resolve`, `report --run <id> | --latest`, `run-data` (geoblock → discover → snapshot → resolve → report written to `data/reports/shortlist-<run_id>.md` and `.json`).

- [ ] **Step 1: Write the failing tests**

`tests/predict/test_cli_isolation.py`:

```python
from __future__ import annotations

import ast
import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from predict_agent.cli import main
from predict_agent.http import JsonClient
from tests.predict.fakes import RoutedOpener
from tests.predict.fixtures import (
    NO_TOKEN,
    NOW,
    YES_TOKEN,
    clob_book,
    gamma_event,
    gamma_market,
    resolution_row,
)

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "src" / "predict_agent"


class IsolationTests(unittest.TestCase):
    def test_predict_agent_never_imports_japan_agent(self) -> None:
        for path in PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    with self.subTest(file=path.name, module=name):
                        self.assertFalse(name.startswith("japan_agent"))

    def test_no_write_http_methods_or_signing_in_package(self) -> None:
        forbidden = ("method=\"POST\"", "method='POST'", "eth_account", "private_key", "sign_order")
        for path in PACKAGE.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(file=path.name, token=token):
                    self.assertNotIn(token, text)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def install_policy(self) -> None:
        shutil.copy(
            REPO / "config" / "predict-policy.example.json",
            self.root / "config" / "predict-policy.json",
        )

    def run_cli(self, argv: list[str], client: JsonClient | None = None) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(argv, client=client, root=self.root, now_fn=lambda: NOW)
        return code, out.getvalue()

    def test_doctor_fails_closed_without_policy(self) -> None:
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 2)
        self.assertIn("predict-policy.example.json", output)

    def test_doctor_passes_with_policy(self) -> None:
        self.install_policy()
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)

    def test_run_data_writes_report_files(self) -> None:
        self.install_policy()
        routes: dict[str, list[object]] = {
            "api/geoblock": [{"blocked": True, "country": "GB"}],
            "tag_slug=geopolitics": [[]],
            "tag_slug=economics": [[]],
            "tag_slug=politics": [[gamma_event([gamma_market()])]],
            "/v2/resolutions": [{"data": [resolution_row()]}, {"data": [resolution_row()]}],
            f"token_id={YES_TOKEN}": [clob_book()],
            f"token_id={NO_TOKEN}": [clob_book(asset_id=NO_TOKEN)],
        }
        client = JsonClient(opener=RoutedOpener(routes), sleep=lambda _: None)
        code, output = self.run_cli(["run-data"], client=client)
        self.assertEqual(code, 0, output)
        reports = sorted((self.root / "data" / "reports").iterdir())
        self.assertEqual([p.suffix for p in reports], [".json", ".md"])
        self.assertIn("eligible: 1", reports[1].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_cli_isolation -v`
Expected: ERROR `No module named 'predict_agent.cli'`.

- [ ] **Step 3: Implement the CLI**

`src/predict_agent/cli.py`:

```python
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .collect import discover, poll_resolutions, record_geoblock, snapshot_eligible, start_run
from .config import ConfigError, Settings, load_discovery_config
from .db import connect, verify_journal
from .http import FetchError, JsonClient
from .report import render_markdown, shortlist
from .util import utc_now


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="predict-agent", description="Paper-only Polymarket data collector (Phase 1)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check configuration; fails closed")
    sub.add_parser("discover", help="discover eligible markets")
    sub.add_parser("snapshot", help="snapshot books for the latest discovery run")
    sub.add_parser("resolve", help="poll resolution state for known markets")
    report = sub.add_parser("report", help="write the shortlist report")
    which = report.add_mutually_exclusive_group(required=True)
    which.add_argument("--run")
    which.add_argument("--latest", action="store_true")
    sub.add_parser("run-data", help="geoblock, discover, snapshot, resolve, report")
    return parser


def _latest_discovery_run(conn_path: Path) -> str | None:
    conn = connect(conn_path)
    try:
        row = conn.execute(
            "SELECT run_id FROM runs WHERE command IN ('discover', 'run-data') "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row["run_id"] if row else None
    finally:
        conn.close()


def _write_report(settings: Settings, conn_path: Path, run_id: str) -> Path:
    conn = connect(conn_path)
    try:
        data = shortlist(conn, run_id)
    finally:
        conn.close()
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    markdown = settings.reports_dir / f"shortlist-{run_id}.md"
    markdown.write_text(render_markdown(data), encoding="utf-8")
    (settings.reports_dir / f"shortlist-{run_id}.json").write_text(
        json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
    )
    return markdown


def main(
    argv: list[str] | None = None,
    *,
    client: JsonClient | None = None,
    root: Path | None = None,
    now_fn: Callable[[], datetime] = utc_now,
) -> int:
    args = _parser().parse_args(argv)
    settings = Settings.from_root((root or Path.cwd()).resolve())
    try:
        config, policy_hash = load_discovery_config(settings.policy_path)
    except ConfigError as error:
        print(f"predict-agent: {error}", file=sys.stderr)
        return 2
    if args.command == "doctor":
        conn = connect(settings.database_path)
        try:
            ok = verify_journal(conn)
        finally:
            conn.close()
        print(f"policy {policy_hash[:12]} ok; journal chain {'ok' if ok else 'BROKEN'}")
        return 0 if ok else 3
    http = client or JsonClient()
    try:
        if args.command == "report":
            run_id = args.run or _latest_discovery_run(settings.database_path)
            if run_id is None:
                print("predict-agent: no discovery run yet", file=sys.stderr)
                return 2
            print(_write_report(settings, settings.database_path, run_id))
            return 0
        conn = connect(settings.database_path)
        try:
            run_id = start_run(conn, args.command, policy_hash, now_fn())
            geoblock = record_geoblock(conn, http, run_id)
            print(f"run {run_id}; geoblock (audit only): {geoblock}")
            if args.command in ("discover", "run-data"):
                summary = discover(conn, http, config, run_id, now_fn())
                print(f"seen {summary.markets_seen}, eligible {summary.eligible}")
            if args.command == "snapshot":
                latest = _latest_discovery_run(settings.database_path)
                if latest is None:
                    print("predict-agent: no discovery run yet", file=sys.stderr)
                    return 2
                print(f"snapshots {snapshot_eligible(conn, http, config, latest, now_fn)}")
            if args.command == "run-data":
                print(f"snapshots {snapshot_eligible(conn, http, config, run_id, now_fn)}")
            if args.command in ("resolve", "run-data"):
                print(f"resolution observations {poll_resolutions(conn, http, run_id, now_fn())}")
        finally:
            conn.close()
        if args.command == "run-data":
            print(_write_report(settings, settings.database_path, run_id))
    except FetchError as error:
        print(f"predict-agent: fetch failed, run aborted: {error}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests and the full suite**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_cli_isolation -v`
Expected: all OK.

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -v`
Expected: all existing `japan_agent` tests plus all `tests/predict` tests OK.

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`
Expected: clean.

- [ ] **Step 5: Add the CLAUDE.md section**

Append to `CLAUDE.md`:

```markdown
## predict_agent (Polymarket paper forecaster, Phase 1)

- Spec: `docs/superpowers/specs/2026-10-05-polymarket-paper-forecaster-design.md`; plans in `docs/superpowers/plans/`.
- Separate package: `predict_agent` must never import `japan_agent` (enforced by `tests/predict/test_cli_isolation.py`). `japan_agent` is frozen.
- Phase 1 has **no execution code**: no wallet, keys, signing, or non-GET HTTP. Never attempt to circumvent Polymarket's UK geoblock; the geoblock check is audit-only.
- CLI: `PYTHONPATH=src python3 -m predict_agent.cli doctor|discover|snapshot|resolve|report|run-data`. Needs `config/predict-policy.json` (human-owned copy of the `.example`; code never writes it).
- Live API quirks pinned in `tests/predict/fixtures.py`: JSON-string list fields, worst-first books on both sides, resolution rows without `payouts`, closed Gamma markets only returned with `closed=true`.
```

- [ ] **Step 6: Live read-only smoke run (manual, needs network)**

```bash
cp config/predict-policy.example.json config/predict-policy.json
PYTHONPATH=src python3 -m predict_agent.cli run-data
```

Expected: prints a run id, geoblock `{'blocked': True, 'country': 'GB', ...}` (audit only), non-zero `seen`, an `eligible` count (zero is a valid result), and a report path under `data/reports/`. Then pick one shortlisted market from the Markdown report and compare its question, end date and YES best ask against `https://gamma-api.polymarket.com/markets?condition_ids=<id>` fetched by hand; record agreement or differences in the commit message body. If anything differs in shape from the "Contract facts" section, stop and update fixtures and code before committing.

- [ ] **Step 7: Commit**

```bash
git add src/predict_agent/cli.py tests/predict/test_cli_isolation.py CLAUDE.md
git commit -m "feat(predict): CLI, isolation guard, CLAUDE.md section; live smoke run"
```

---

## Self-Review Record

1. **Spec coverage (milestone 1 only):** child-market eligibility incl. resolution-state check (Tasks 4, 5, 7); immutable rules versions and RULES_CHANGED journaling (Tasks 6, 7); book snapshots with last-change (`observed_at`) vs fetched time and fee schedule as served — staleness measured from `fetched_at`, enforced in Plan 3 (Tasks 6, 7); resolution polling for every known market with `was_disputed`/`new_version_q` and unknown-status handling (Tasks 5, 7); shortlist with counts after every exclusion (Task 8); fixtures for every API shape used (Task 2); geoblock audit (Task 7); permissions 0700/0600 and journal inside the DB (Task 6); no execution code guard (Task 9). Deferred by design to Plans 2–5: forecasts, cohorts, artifacts other than rules, cash ledger, policy, paper fills, research layer, full report, cron wrapper, share-precision and min-order-unit enforcement (recorded here, enforced in Plan 3).
2. **Placeholder scan:** none.
3. **Type consistency:** `MarketCandidate`, `BookSnapshot`, `ResolutionState`, `DiscoverySummary` field names match across tasks; `JsonClient.get(url, params)` used uniformly; `record_refusal` signature identical in all callers.
4. **Review Focus:** five items listed at the top, each with its test named in the owning task.

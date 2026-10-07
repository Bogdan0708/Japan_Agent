# predict-agent Plan 3 — Paper Policy and Fills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn an entry forecast with a timely post-forecast baseline into exactly one paper decision per portfolio — a ticket filled from the recorded book, or a coded refusal — using the spec §5 policy for the primary portfolio and the pre-registered p_mid policy for the shadow portfolio, with no network and no Claude calls.

**Architecture:** `policy_params.py` parses the human-owned config's `policy` section and freezes it as one artifact per portfolio variant. `fills.py` (pure) walks a recorded ask book with per-level fees. `policy.py` (pure, no I/O, no clock) maps a forecast, both books and the portfolio's ledger state to a `Trade` or a `Refusal`. `paper.py` reads those inputs and writes the result through the Plan 2 ledger in **one transaction per portfolio decision**. `predict-agent trade` runs it offline; `doctor` re-checks every ticket's fills against its snapshot and policy.

**Tech Stack:** Python ≥ 3.11 stdlib (`decimal`, `sqlite3`, `json`), `unittest`, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-10-05-polymarket-paper-forecaster-design.md` (§3 stages 4–5, §5 paper policy, §8 policy/book-walk tests, §10 build order item 3).

**Builds on:** Plan 1 (data) and Plan 2 (ledger, `docs/superpowers/plans/2026-10-05-predict-agent-02-ledger.md`), merged on `main` at `553cd33`. Deferred items from Plan 2 live in the project memory note `plan2-carry-forward.md`; Task 6 closes the ones that belong here.

**Verification of this plan:** every code block below was extracted from a scratch worktree where the six tasks were committed in order on top of `553cd33`, with the full suite run after each (311 → 319 → 334 → 345 → 351 → 356 tests, all OK); the final tree passed on Python 3.11 and 3.14 with ruff, strict mypy, `bash -n` and `git diff --check` clean. Thirteen mutation checks (dropping a cap, the slippage limit, per-level fees, share rounding, the USDC reading of the minimum order, the book-age gate, the eligibility check, the shadow's p_mid source, the fill-depth and book-age invariants, total journal verification) each made a test fail.

## Global Constraints

- Core code is stdlib-only; `predict_agent` never imports `japan_agent`; no wallet, key, signing or non-GET HTTP code; Phase 1 never places a real order.
- Money is `Decimal`, stored as exact text; never `SUM()` money in SQL; never `float`.
- Timestamps are timezone-aware UTC and compared parsed, never as strings.
- `policy.decide` and `fills.walk_asks` are pure: no I/O, no clock, no database; every input arrives as an argument.
- A decision uses the portfolio's **frozen policy artifact**, never `config/predict-policy.json` directly. The config file is human-owned: code reads it, never writes it.
- Every state change commits with its journal entry in one `db.transaction`; a decision's inputs are read inside the same transaction that writes it.
- Fees are summed per level (`shares × rate × price × (1 − price)`), never at the average price. Fills never exceed recorded depth and never exceed the limit price.
- One entry per market per portfolio, ever; update forecasts never trade (spec §5).
- Fail-closed refusals are correct behavior: a refusal is recorded with a reason code, never retried into a trade.
- Canonical test run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -v`; ruff line length 100; mypy strict on `src/predict_agent`; code stays 3.11-compatible.

## Spec deviations and rulings in this plan

1. **Slippage is a limit price, not a post-hoc refusal.** The walk never buys above `best_ask × (1 + max_slippage)`, so realized slippage cannot exceed the cap; deeper levels are simply not bought. Spec step 5's "refuse if slippage > 2%" can then never fire. Cost if wrong: trades are smaller than the spec intends instead of refused.
2. **Insufficient depth fills partially.** The walk buys up to the recorded depth inside the limit price and is refused (`NO_DEPTH` / `BELOW_MIN_ORDER`) only when that buys nothing or less than the minimum order. "Never fabricate fills beyond recorded depth" holds; spec step 5's bare "depth is insufficient" is read as "cannot fill a minimum order". Cost if wrong: some thin-book trades should have been refused.
3. **Share precision is 0.01 shares** (`fills.SHARE_QUANTUM`), from Plan 1 contract fact 4 (CLOB sizes are served with 2 decimal places). Quantities are rounded down.
4. **The minimum order size must hold in both units** — shares and USDC — because the docs contradict each other (Plan 1 contract fact 5). `"0"` (served live for some markets) always passes.
5. **"Eligibility lost" means absent from the latest COMPLETED discovery run**; **"resolution started" means any resolution observation for the market is no longer `posed`.** The spec names the conditions without defining them offline.
6. **Policy artifacts.** `variant_policies` freezes the config's `policy` section as canonical JSON, adding `probability` (`bounds` for `primary`, `mid` for `shadow_mid`). The starting bankroll is inside the artifact, so a bankroll change opens a new cohort (consistent with Plan 2's bankroll refusal). Plan 4 builds `CohortIdentity.portfolios` from it.
7. **`doctor` now requires the config's `policy` section** (fail closed on incomplete setup). Existing `config/predict-policy.json` files must gain the section from the example before `doctor` passes again.
8. **Kelly uses the best ask without fees** (`(q − a)/(1 − a)`, spec §5 step 3 literally); fees enter the provisional and realized edge and the budget walk.
9. **Equal realized edge on both sides buys YES** (deterministic tie-break). When both sides refuse, the reported code is the side that got further (a side with no edge at all yields).
10. **A closed cohort's undecided forecasts are refused `COHORT_CLOSED`** by `trade`, so resume never loops on them (Plan 2 ruling: refusals do not require an active cohort).
11. **`STALE_BOOK` is final for that forecast**, as every decision is: a decision taken more than `max_book_age_seconds` after the books were fetched refuses rather than trading on old prices (spec §5, §7).

## Review Focus

1. **Sizing before depth** — the spec's worked example (q = 0.80, ask 0.50, equity $1,000: quarter Kelly $150 → capped to $20 → 40 of 50 shares) must hold in the pure policy and end to end (Task 3 `test_spec_worked_example`, Task 4 `test_spec_worked_example_end_to_end_in_both_portfolios`).
2. **Per-level fees** — 20 @ 0.50 + 20 @ 0.51 at rate 0.05 must cost 0.49990, not the average-price 0.49995; a fee-enabled ticket's fee must equal the per-level sum from its snapshot's schedule (Task 2 `test_fees_are_summed_per_level_not_at_the_average_price`, Task 4 `test_fees_are_charged_per_level_from_the_snapshot_schedule`).
3. **Primary and shadow diverge on the same forecast** — conservative bounds refuse `NO_EDGE` while p_mid trades, each with its own cash (Task 4 `test_primary_refuses_where_the_shadow_mid_policy_trades`).
4. **A late run** — deciding ten minutes after the books were fetched must refuse `STALE_BOOK` in every portfolio and close the forecast, never trade on old prices (Task 4 `test_a_late_decision_on_a_stale_book_refuses_and_is_final`).
5. **Fabricated fills are visible** — a ticket re-hashed with fills beyond the recorded depth, a fee not from the snapshot, a fill past the slippage limit, or a decision on a stale book must each be reported by `doctor` (Task 5 `test_paper_invariants`).
6. **Losses shrink the next size** — after a losing settlement, the next ticket's budget is 2% of the reduced equity ($19.60 of $980) (Task 4 `test_losses_shrink_equity_and_the_next_size`).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/predict_agent/policy_params.py` | parse/validate the config `policy` section; freeze per-variant policy artifacts; read them back |
| `src/predict_agent/fills.py` | pure ask-book walk: tick validation, per-level fees, share rounding, limit price |
| `src/predict_agent/policy.py` | pure decision: gates, side probabilities, Kelly/cap budget, walk, realized edge, side choice |
| `src/predict_agent/paper.py` | read policy inputs from the ledger and write ticket or refusal in one transaction; `trade_ready` |
| `src/predict_agent/tickets.py` (modify) | in-transaction variants `open_ticket_locked`, `record_refusal_locked` (refusal `detail` journaled) |
| `src/predict_agent/invariants.py` (modify) | fills re-checked against the snapshot book, fee schedule, slippage limit and book age |
| `src/predict_agent/cli.py` (modify) | `doctor` validates the config `policy` section; new offline `trade` command |
| `src/predict_agent/db.py` (modify) | `verify_journal` reports an unreadable entry as broken instead of raising |
| `config/predict-policy.example.json` (modify) | spec §5 defaults in a `policy` section |
| `tests/predict/ledger_fixtures.py` (modify) | real policy artifacts and real recorded asks for every seeded snapshot |
| `tests/predict/trade_fixtures.py` | policy cohorts, tradeable markets, discovery runs, baselined forecasts |
| `tests/predict/test_policy_params.py`, `test_fills.py`, `test_policy.py`, `test_paper.py`, `test_paper_invariants.py` | one module per new unit |

---

### Task 1: Policy parameters and per-variant artifacts

**Files:**
- Create: `src/predict_agent/policy_params.py`, `tests/predict/test_policy_params.py`
- Modify: `config/predict-policy.example.json`, `src/predict_agent/cli.py`

**Interfaces:**
- Consumes: `config.ConfigError`; `util.canonical_json`; `cli.main(argv, *, client, root, now_fn)`.
- Produces:
  - `policy_params.PROBABILITY_SOURCES = ("bounds", "mid")`, `CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}`, `VARIANT_SOURCES = {"primary": "bounds", "shadow_mid": "mid"}`
  - `policy_params.PolicyParams` frozen dataclass: `probability: str`, `starting_bankroll: Decimal`, `min_edge: Decimal`, `min_confidence: str`, `kelly_fraction: Decimal`, `cap_market: Decimal`, `cap_event: Decimal`, `cap_category: Decimal`, `cap_total_open: Decimal`, `max_slippage: Decimal`, `min_hours_to_close: int`, `max_book_age_seconds: int`; method `record() -> dict[str, Any]`
  - `policy_params.parse_policy(raw: object, probability: str, label: str = "policy") -> PolicyParams` (strict: every key, no unknown keys, decimals as strings, ranges checked; raises `ConfigError`)
  - `policy_params.load_policy_config(path: Path) -> PolicyParams` (the config's `policy` section as `bounds`; refuses a missing file/section or a `probability` key)
  - `policy_params.variant_policies(params: PolicyParams) -> dict[str, str]` (variant → canonical artifact content)
  - `policy_params.policy_from_artifact(content: str) -> PolicyParams`
  - CLI: `doctor` exits 2 with the `ConfigError` text when the `policy` section is missing or invalid


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_policy_params.py` (create):

```python
from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any

from predict_agent.cli import main
from predict_agent.config import ConfigError
from predict_agent.policy_params import (
    load_policy_config,
    parse_policy,
    policy_from_artifact,
    variant_policies,
)

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "config" / "predict-policy.example.json"


def example_section() -> dict[str, Any]:
    section: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))["policy"]
    return section


class ParsePolicyTests(unittest.TestCase):
    def test_example_config_parses_to_the_spec_defaults(self) -> None:
        params = parse_policy(example_section(), "bounds")
        self.assertEqual(params.starting_bankroll, Decimal("1000"))
        self.assertEqual(params.min_edge, Decimal("0.05"))
        self.assertEqual(params.min_confidence, "medium")
        self.assertEqual(params.kelly_fraction, Decimal("0.25"))
        self.assertEqual(
            (params.cap_market, params.cap_event, params.cap_category, params.cap_total_open),
            (Decimal("0.02"), Decimal("0.05"), Decimal("0.15"), Decimal("0.50")),
        )
        self.assertEqual(params.max_slippage, Decimal("0.02"))
        self.assertEqual((params.min_hours_to_close, params.max_book_age_seconds), (48, 120))

    def test_invalid_sections_are_refused(self) -> None:
        def broken(**changes: Any) -> dict[str, Any]:
            section = copy.deepcopy(example_section())
            for key, value in changes.items():
                if value is None:
                    del section[key]
                else:
                    section[key] = value
            return section

        cases = {
            "missing key": broken(min_edge=None),
            "unknown key": broken(leverage="2"),
            "float not string": broken(min_edge=0.05),
            "nan": broken(min_edge="NaN"),
            "edge 1": broken(min_edge="1"),
            "kelly above 1": broken(kelly_fraction="1.5"),
            "zero bankroll": broken(starting_bankroll="0"),
            "bad confidence": broken(min_confidence="certain"),
            "caps missing one": broken(caps={"market": "0.02", "event": "0.05",
                                             "category": "0.15"}),
            "cap zero": broken(caps={"market": "0", "event": "0.05", "category": "0.15",
                                     "total_open": "0.5"}),
            "bool hours": broken(min_hours_to_close=True),
            "negative slippage": broken(max_slippage="-0.01"),
        }
        for label, section in cases.items():
            with self.subTest(label), self.assertRaises(ConfigError):
                parse_policy(section, "bounds")
        with self.assertRaises(ConfigError):
            parse_policy(example_section(), "median")


class ArtifactTests(unittest.TestCase):
    def test_variants_differ_only_in_probability_source_and_round_trip(self) -> None:
        params = parse_policy(example_section(), "bounds")
        policies = variant_policies(params)
        self.assertEqual(set(policies), {"primary", "shadow_mid"})
        primary = policy_from_artifact(policies["primary"])
        shadow = policy_from_artifact(policies["shadow_mid"])
        self.assertEqual((primary.probability, shadow.probability), ("bounds", "mid"))
        self.assertEqual(primary, params)
        primary_raw = json.loads(policies["primary"])
        shadow_raw = json.loads(policies["shadow_mid"])
        del primary_raw["probability"], shadow_raw["probability"]
        self.assertEqual(primary_raw, shadow_raw)

    def test_corrupt_artifact_is_refused(self) -> None:
        for content in ("not json", "[]", '{"probability": "bounds"}'):
            with self.subTest(content=content), self.assertRaises(ConfigError):
                policy_from_artifact(content)


class ConfigFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        self.path = self.root / "config" / "predict-policy.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, raw: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(raw), encoding="utf-8")

    def test_loads_the_policy_section_as_the_primary_policy(self) -> None:
        shutil.copy(EXAMPLE, self.path)
        self.assertEqual(load_policy_config(self.path).probability, "bounds")

    def test_missing_file_section_or_probability_override_is_refused(self) -> None:
        with self.assertRaisesRegex(ConfigError, "is missing"):
            load_policy_config(self.path)
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.write({"discovery": raw["discovery"]})
        with self.assertRaisesRegex(ConfigError, "no 'policy' section"):
            load_policy_config(self.path)
        raw["policy"]["probability"] = "mid"
        self.write(raw)
        with self.assertRaisesRegex(ConfigError, "per portfolio variant"):
            load_policy_config(self.path)

    def test_doctor_fails_closed_without_a_policy_section(self) -> None:
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.write({"discovery": raw["discovery"]})
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(["doctor"], root=self.root)
        self.assertEqual(code, 2)
        self.assertIn("no 'policy' section", err.getvalue())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_policy_params -v`
Expected: ERROR `No module named 'predict_agent.policy_params'`.

- [ ] **Step 3: Implement**

`src/predict_agent/policy_params.py` (create):

```python
"""Paper-policy parameters (spec §5).

The human-owned config file holds one `policy` section. Each cohort freezes it as one
policy artifact per portfolio variant: `primary` prices sides from the conservative bounds
(q_YES = p_low, q_NO = 1 - p_high) and the pre-registered shadow `shadow_mid` from p_mid.
Decisions always read the portfolio's frozen artifact, never the config file."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .config import ConfigError
from .util import canonical_json

PROBABILITY_SOURCES = ("bounds", "mid")
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
VARIANT_SOURCES = {"primary": "bounds", "shadow_mid": "mid"}

_DECIMAL_FIELDS = (
    "starting_bankroll",
    "min_edge",
    "kelly_fraction",
    "max_slippage",
)
_CAP_FIELDS = ("market", "event", "category", "total_open")
_INT_FIELDS = ("min_hours_to_close", "max_book_age_seconds")
_CONFIG_KEYS = frozenset({*_DECIMAL_FIELDS, *_INT_FIELDS, "min_confidence", "caps"})


@dataclass(frozen=True)
class PolicyParams:
    probability: str  # "bounds" | "mid"
    starting_bankroll: Decimal
    min_edge: Decimal
    min_confidence: str
    kelly_fraction: Decimal
    cap_market: Decimal
    cap_event: Decimal
    cap_category: Decimal
    cap_total_open: Decimal
    max_slippage: Decimal
    min_hours_to_close: int
    max_book_age_seconds: int

    def record(self) -> dict[str, Any]:
        return {
            "probability": self.probability,
            "starting_bankroll": format(self.starting_bankroll, "f"),
            "min_edge": format(self.min_edge, "f"),
            "min_confidence": self.min_confidence,
            "kelly_fraction": format(self.kelly_fraction, "f"),
            "caps": {
                "market": format(self.cap_market, "f"),
                "event": format(self.cap_event, "f"),
                "category": format(self.cap_category, "f"),
                "total_open": format(self.cap_total_open, "f"),
            },
            "max_slippage": format(self.max_slippage, "f"),
            "min_hours_to_close": self.min_hours_to_close,
            "max_book_age_seconds": self.max_book_age_seconds,
        }


def _decimal(raw: Mapping[str, Any], key: str, label: str) -> Decimal:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"{label}.{key} must be a decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ConfigError(f"{label}.{key} must be a decimal string") from None
    if not number.is_finite():
        raise ConfigError(f"{label}.{key} must be finite")
    return number


def _fraction(raw: Mapping[str, Any], key: str, label: str) -> Decimal:
    number = _decimal(raw, key, label)
    if not Decimal("0") < number <= Decimal("1"):
        raise ConfigError(f"{label}.{key} must be in (0, 1]")
    return number


def _positive_int(raw: Mapping[str, Any], key: str, label: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{label}.{key} must be a positive integer")
    return value


def parse_policy(raw: object, probability: str, label: str = "policy") -> PolicyParams:
    """Strict: every key required, no unknown keys, every value range-checked."""
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{label} must be an object")
    if probability not in PROBABILITY_SOURCES:
        raise ConfigError(f"{label}.probability must be one of {PROBABILITY_SOURCES}")
    keys = set(raw) - {"probability"}
    if keys != _CONFIG_KEYS:
        missing = sorted(_CONFIG_KEYS - keys)
        unknown = sorted(keys - _CONFIG_KEYS)
        raise ConfigError(f"{label}: missing keys {missing}, unknown keys {unknown}")
    caps = raw["caps"]
    if not isinstance(caps, Mapping) or set(caps) != set(_CAP_FIELDS):
        raise ConfigError(f"{label}.caps must have exactly {list(_CAP_FIELDS)}")
    bankroll = _decimal(raw, "starting_bankroll", label)
    if bankroll <= 0:
        raise ConfigError(f"{label}.starting_bankroll must be positive")
    min_edge = _decimal(raw, "min_edge", label)
    if not Decimal("0") < min_edge < Decimal("1"):
        raise ConfigError(f"{label}.min_edge must be in (0, 1)")
    max_slippage = _decimal(raw, "max_slippage", label)
    if not Decimal("0") <= max_slippage < Decimal("1"):
        raise ConfigError(f"{label}.max_slippage must be in [0, 1)")
    confidence = raw["min_confidence"]
    if confidence not in CONFIDENCE_RANK:
        raise ConfigError(f"{label}.min_confidence must be one of {list(CONFIDENCE_RANK)}")
    return PolicyParams(
        probability=probability,
        starting_bankroll=bankroll,
        min_edge=min_edge,
        min_confidence=confidence,
        kelly_fraction=_fraction(raw, "kelly_fraction", label),
        cap_market=_fraction(caps, "market", f"{label}.caps"),
        cap_event=_fraction(caps, "event", f"{label}.caps"),
        cap_category=_fraction(caps, "category", f"{label}.caps"),
        cap_total_open=_fraction(caps, "total_open", f"{label}.caps"),
        max_slippage=max_slippage,
        min_hours_to_close=_positive_int(raw, "min_hours_to_close", label),
        max_book_age_seconds=_positive_int(raw, "max_book_age_seconds", label),
    )


def load_policy_config(path: Path) -> PolicyParams:
    """The `policy` section of the human-owned config, as the primary (bounds) policy."""
    if not path.exists():
        raise ConfigError(
            f"{path} is missing: copy config/predict-policy.example.json to "
            "config/predict-policy.json and review every value"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path} is not valid JSON") from error
    if not isinstance(raw, dict) or "policy" not in raw:
        raise ConfigError(
            f"{path} has no 'policy' section: copy it from config/predict-policy.example.json "
            "and review every value"
        )
    section = raw["policy"]
    if isinstance(section, Mapping) and "probability" in section:
        raise ConfigError("policy.probability is set per portfolio variant, not in config")
    return parse_policy(section, "bounds")


def variant_policies(params: PolicyParams) -> dict[str, str]:
    """variant -> canonical policy artifact content, for CohortIdentity.portfolios."""
    return {
        variant: canonical_json(replace(params, probability=source).record())
        for variant, source in VARIANT_SOURCES.items()
    }


def policy_from_artifact(content: str) -> PolicyParams:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as error:
        raise ConfigError("policy artifact is not JSON") from error
    if not isinstance(raw, Mapping):
        raise ConfigError("policy artifact must be an object")
    return parse_policy(raw, str(raw.get("probability")), "policy artifact")
```

Apply to `config/predict-policy.example.json` (`git apply` accepts this hunk as written):

```diff
diff --git a/config/predict-policy.example.json b/config/predict-policy.example.json
index da029d7..ae89742 100644
--- a/config/predict-policy.example.json
+++ b/config/predict-policy.example.json
@@ -1,9 +1,18 @@
 {
   "discovery": {
     "tag_categories": [
-      ["geopolitics", "geopolitics"],
-      ["economics", "economics"],
-      ["politics", "politics"]
+      [
+        "geopolitics",
+        "geopolitics"
+      ],
+      [
+        "economics",
+        "economics"
+      ],
+      [
+        "politics",
+        "politics"
+      ]
     ],
     "min_liquidity": "5000",
     "min_days_to_end": 2,
@@ -12,5 +21,20 @@
     "max_book_age_seconds": 120,
     "page_size": 100,
     "max_pages": 60
+  },
+  "policy": {
+    "starting_bankroll": "1000",
+    "min_edge": "0.05",
+    "min_confidence": "medium",
+    "kelly_fraction": "0.25",
+    "caps": {
+      "market": "0.02",
+      "event": "0.05",
+      "category": "0.15",
+      "total_open": "0.50"
+    },
+    "max_slippage": "0.02",
+    "min_hours_to_close": 48,
+    "max_book_age_seconds": 120
   }
 }
```

Apply to `src/predict_agent/cli.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/src/predict_agent/cli.py b/src/predict_agent/cli.py
index d8df9f7..c284688 100644
--- a/src/predict_agent/cli.py
+++ b/src/predict_agent/cli.py
@@ -21,6 +21,7 @@ from .db import connect, record_refusal, verify_journal
 from .gamma import ParseError
 from .http import FetchError, JsonClient
 from .invariants import verify_ledger
+from .policy_params import load_policy_config
 from .report import render_markdown, shortlist
 from .settlement import settle_open_tickets
 from .util import utc_now
@@ -86,6 +87,11 @@ def main(
         print(f"predict-agent: {error}", file=sys.stderr)
         return 2
     if args.command == "doctor":
+        try:
+            load_policy_config(settings.policy_path)
+        except ConfigError as error:
+            print(f"predict-agent: {error}", file=sys.stderr)
+            return 2
         conn = connect(settings.database_path)
         try:
             journal_ok = verify_journal(conn)
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_policy_params -v` → 7 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`

```bash
git add src/predict_agent/policy_params.py src/predict_agent/cli.py config/predict-policy.example.json tests/predict/test_policy_params.py
git commit -m "feat(predict): policy parameters and per-variant policy artifacts"
```

---

### Task 2: Ask-book walk with per-level fees

**Files:**
- Create: `src/predict_agent/fills.py`, `tests/predict/test_fills.py`

**Interfaces:**
- Consumes: `tickets.Fill(price: Decimal, shares: Decimal)` (Plan 2).
- Produces:
  - `fills.SHARE_QUANTUM = Decimal("0.01")`
  - `fills.FillError(ValueError)` with attribute `code: str` (`TICK_MISMATCH`, `BAD_BOOK`, `BAD_BUDGET`, `FEE_UNKNOWN`)
  - `fills.AskLevel(price: Decimal, size: Decimal)` frozen dataclass
  - `fills.Walk(fills: tuple[Fill, ...], shares: Decimal, notional: Decimal, fee: Decimal)` with properties `cost`, `cost_per_share`, `average_price`
  - `fills.fee_per_share(price, rate) -> Decimal`, `fills.level_fee(shares, price, rate) -> Decimal`, `fills.round_shares(quantity) -> Decimal`
  - `fills.sorted_asks(asks, tick_size) -> tuple[AskLevel, ...]` (best first; validates tick grid, range, size)
  - `fills.walk_asks(asks, *, budget, fee_rate, limit_price, tick_size) -> Walk` (spends at most `budget` incl. fees; may return zero shares)


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_fills.py` (create):

```python
from __future__ import annotations

import unittest
from decimal import Decimal

from predict_agent.fills import (
    AskLevel,
    FillError,
    fee_per_share,
    level_fee,
    round_shares,
    walk_asks,
)
from predict_agent.tickets import Fill

D = Decimal
TICK = D("0.01")


def book(*levels: tuple[str, str]) -> list[AskLevel]:
    return [AskLevel(D(price), D(size)) for price, size in levels]


class FeeTests(unittest.TestCase):
    def test_fees_are_summed_per_level_not_at_the_average_price(self) -> None:
        # Spec §8: 20 @ 0.50 + 20 @ 0.51 at rate 0.05 costs 0.49990 in fees, not 0.49995.
        walk = walk_asks(
            book(("0.50", "20"), ("0.51", "20")),
            budget=D("100"),
            fee_rate=D("0.05"),
            limit_price=D("0.99"),
            tick_size=TICK,
        )
        self.assertEqual(walk.shares, D("40"))
        self.assertEqual(walk.fee, D("0.49990"))
        at_average = level_fee(D("40"), walk.average_price, D("0.05"))
        self.assertEqual(at_average, D("0.49995"))
        self.assertEqual(walk.notional, D("20.20"))
        self.assertEqual(walk.cost, D("20.69990"))

    def test_fee_free_book_has_zero_fee(self) -> None:
        self.assertEqual(fee_per_share(D("0.4"), D("0")), D("0"))


class WalkTests(unittest.TestCase):
    def walk(self, levels: list[AskLevel], budget: str, limit: str = "0.99",
             rate: str = "0") -> object:
        return walk_asks(levels, budget=D(budget), fee_rate=D(rate),
                         limit_price=D(limit), tick_size=TICK)

    def test_spec_worked_example_fills_forty_shares_from_a_fifty_share_book(self) -> None:
        # Spec §8: budget capped to $20 at ask 0.50 buys 40 of the 50 recorded shares.
        walk = walk_asks(book(("0.50", "50")), budget=D("20"), fee_rate=D("0"),
                         limit_price=D("0.51"), tick_size=TICK)
        self.assertEqual(walk.fills, (Fill(D("0.50"), D("40")),))
        self.assertEqual(walk.cost, D("20"))

    def test_multi_level_walk_from_worst_first_input(self) -> None:
        # The live API serves asks worst-first; the walk must start at the best price.
        walk = walk_asks(book(("0.43", "100"), ("0.42", "10"), ("0.41", "5")),
                         budget=D("6.2543"), fee_rate=D("0"), limit_price=D("0.43"),
                         tick_size=TICK)
        self.assertEqual(
            walk.fills,
            (Fill(D("0.41"), D("5")), Fill(D("0.42"), D("10")), Fill(D("0.43"), D("0.01"))),
        )
        self.assertEqual(walk.cost, D("6.2543"))

    def test_never_walks_past_the_limit_price_or_the_recorded_depth(self) -> None:
        walk = walk_asks(book(("0.40", "10"), ("0.45", "1000")), budget=D("1000"),
                         fee_rate=D("0"), limit_price=D("0.408"), tick_size=TICK)
        self.assertEqual(walk.fills, (Fill(D("0.40"), D("10")),))
        thin = walk_asks(book(("0.40", "3.5")), budget=D("1000"), fee_rate=D("0"),
                         limit_price=D("0.99"), tick_size=TICK)
        self.assertEqual(thin.shares, D("3.5"))

    def test_shares_round_down_to_the_share_precision(self) -> None:
        self.assertEqual(round_shares(D("2.999")), D("2.99"))
        walk = walk_asks(book(("0.30", "100")), budget=D("1"), fee_rate=D("0"),
                         limit_price=D("0.99"), tick_size=TICK)
        self.assertEqual(walk.shares, D("3.33"))  # 1 / 0.30 = 3.333...

    def test_budget_below_one_share_unit_buys_nothing(self) -> None:
        walk = walk_asks(book(("0.50", "10")), budget=D("0.004"), fee_rate=D("0"),
                         limit_price=D("0.99"), tick_size=TICK)
        self.assertEqual((walk.fills, walk.shares), ((), D("0")))

    def test_bad_books_are_refused_with_a_code(self) -> None:
        cases = {
            "TICK_MISMATCH": (book(("0.505", "10")), TICK),
            "BAD_BOOK": (book(("0.50", "0")), TICK),
        }
        for code, (levels, tick) in cases.items():
            with self.subTest(code), self.assertRaises(FillError) as caught:
                walk_asks(levels, budget=D("1"), fee_rate=D("0"), limit_price=D("0.99"),
                          tick_size=tick)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(FillError) as caught:
            walk_asks(book(("0.5", "1")), budget=D("1"), fee_rate=D("1"),
                      limit_price=D("0.99"), tick_size=TICK)
        self.assertEqual(caught.exception.code, "FEE_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_fills -v`
Expected: ERROR `No module named 'predict_agent.fills'`.

- [ ] **Step 3: Implement**

`src/predict_agent/fills.py` (create):

```python
"""Paper fills: walk a recorded ask book (spec §5 steps 4–5). Pure, no I/O.

Fees are charged per level: shares x rate x price x (1 - price), never at the average
price. Prices must sit on the book's tick grid; share quantities are rounded down to the
venue's share precision. The walk never goes past the limit price or the recorded depth."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .tickets import Fill

# Share precision: CLOB book sizes are served with 2 decimal places (Plan 1 contract fact 4).
SHARE_QUANTUM = Decimal("0.01")


class FillError(ValueError):
    """A book the walk cannot use. `code` is the refusal reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class AskLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class Walk:
    fills: tuple[Fill, ...]
    shares: Decimal
    notional: Decimal
    fee: Decimal

    @property
    def cost(self) -> Decimal:
        return self.notional + self.fee

    @property
    def cost_per_share(self) -> Decimal:
        return self.cost / self.shares

    @property
    def average_price(self) -> Decimal:
        return self.notional / self.shares


def fee_per_share(price: Decimal, rate: Decimal) -> Decimal:
    return rate * price * (Decimal("1") - price)


def level_fee(shares: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    return shares * fee_per_share(price, rate)


def round_shares(quantity: Decimal) -> Decimal:
    return quantity.quantize(SHARE_QUANTUM, rounding=ROUND_DOWN)


def sorted_asks(asks: Sequence[AskLevel], tick_size: Decimal) -> tuple[AskLevel, ...]:
    """Best (lowest) price first, whatever order the input came in. Refuses levels off the
    tick grid or outside (0, 1), and non-positive sizes."""
    if tick_size <= 0:
        raise FillError("TICK_MISMATCH", f"tick size {tick_size}")
    for level in asks:
        if not Decimal("0") < level.price < Decimal("1") or level.size <= 0:
            raise FillError("BAD_BOOK", f"level {level.price} x {level.size}")
        if level.price % tick_size != 0:
            raise FillError("TICK_MISMATCH", f"price {level.price} is off tick {tick_size}")
    return tuple(sorted(asks, key=lambda level: level.price))


def walk_asks(
    asks: Sequence[AskLevel],
    *,
    budget: Decimal,
    fee_rate: Decimal,
    limit_price: Decimal,
    tick_size: Decimal,
) -> Walk:
    """Spend at most `budget` (fees included) buying from the cheapest level up, never above
    `limit_price` and never more than each level's recorded size. May return zero shares."""
    if budget < 0 or not budget.is_finite():
        raise FillError("BAD_BUDGET", f"budget {budget}")
    if not Decimal("0") <= fee_rate < Decimal("1"):
        raise FillError("FEE_UNKNOWN", f"fee rate {fee_rate}")
    remaining = budget
    fills: list[Fill] = []
    notional = Decimal("0")
    fee = Decimal("0")
    for level in sorted_asks(asks, tick_size):
        if level.price > limit_price:
            break
        unit = level.price + fee_per_share(level.price, fee_rate)
        take = min(round_shares(level.size), round_shares(remaining / unit))
        if take <= 0:
            break
        level_cost = take * level.price
        level_fees = level_fee(take, level.price, fee_rate)
        fills.append(Fill(level.price, take))
        notional += level_cost
        fee += level_fees
        remaining -= level_cost + level_fees
    shares = sum((fill.shares for fill in fills), Decimal("0"))
    return Walk(tuple(fills), shares, notional, fee)
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_fills -v` → 8 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`

```bash
git add src/predict_agent/fills.py tests/predict/test_fills.py
git commit -m "feat(predict): recorded ask-book walk with per-level fees"
```

---

### Task 3: Pure paper policy

**Files:**
- Create: `src/predict_agent/policy.py`, `tests/predict/test_policy.py`

**Interfaces:**
- Consumes: `fills.*` (Task 2); `gamma.fee_rate(schedule) -> Decimal | None` (Plan 1); `policy_params.PolicyParams`, `CONFIDENCE_RANK`, `parse_policy` (Task 1).
- Produces:
  - `policy.SideBook(outcome, snapshot_id, fetched_at, asks: tuple[AskLevel, ...], tick_size, min_order_size, fees_enabled, fee_schedule)`
  - `policy.ForecastView(abstained, p_low, p_mid, p_high, confidence, rules_hash, end_date)`
  - `policy.Exposure(market, event, category, total)` — cost basis of the portfolio's OPEN tickets per cap group
  - `policy.PolicyInputs(forecast, current_rules_hash, eligible, resolution_started, already_traded, books: Mapping[str, SideBook], equity, available_cash, exposure, now)`
  - `policy.Trade(outcome, snapshot_id, q, best_ask, budget, walk)` with property `edge = q − walk.cost_per_share`; `policy.Refusal(reason, detail)`
  - `policy.kelly(q, price)`, `policy.side_probabilities(forecast, source) -> dict[str, Decimal]`, `policy.budget_for(q, ask, inputs, params) -> Decimal`
  - `policy.decide(inputs, params) -> Trade | Refusal`. Refusal codes: `ABSTAINED`, `RULES_CHANGED`, `ELIGIBILITY_LOST`, `RESOLUTION_STARTED`, `ALREADY_TRADED`, `LOW_CONFIDENCE`, `CLOSING_SOON`, `NO_BOOK`, `STALE_BOOK`, `FEE_UNKNOWN`, `NO_DEPTH`, `NO_EDGE`, `NO_BUDGET`, `BELOW_MIN_ORDER`, `NO_EDGE_AFTER_FILL`, plus `FillError` codes


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_policy.py` (create):

```python
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from predict_agent.fills import AskLevel
from predict_agent.policy import (
    Exposure,
    ForecastView,
    PolicyInputs,
    Refusal,
    SideBook,
    Trade,
    budget_for,
    decide,
    kelly,
)
from predict_agent.policy_params import PolicyParams, parse_policy
from tests.predict.fixtures import NOW

D = Decimal
REPO = Path(__file__).resolve().parents[2]
SECTION = json.loads((REPO / "config" / "predict-policy.example.json").read_text())["policy"]
BOUNDS = parse_policy(SECTION, "bounds")
MID = parse_policy(SECTION, "mid")
ZERO_EXPOSURE = Exposure(D("0"), D("0"), D("0"), D("0"))
FETCHED = NOW - timedelta(seconds=30)


def side(outcome: str, *levels: tuple[str, str], **overrides: Any) -> SideBook:
    book = SideBook(
        outcome=outcome,
        snapshot_id=1 if outcome == "YES" else 2,
        fetched_at=FETCHED,
        asks=tuple(AskLevel(D(p), D(s)) for p, s in levels),
        tick_size=D("0.01"),
        min_order_size=D("5"),
        fees_enabled=False,
        fee_schedule=None,
    )
    return replace(book, **overrides)


def inputs(
    p: tuple[str, str, str] = ("0.80", "0.85", "0.90"),
    yes: SideBook | None = None,
    no: SideBook | None = None,
    **overrides: Any,
) -> PolicyInputs:
    base = PolicyInputs(
        forecast=ForecastView(
            abstained=False,
            p_low=D(p[0]),
            p_mid=D(p[1]),
            p_high=D(p[2]),
            confidence="medium",
            rules_hash="r1",
            end_date=NOW + timedelta(days=10),
        ),
        current_rules_hash="r1",
        eligible=True,
        resolution_started=False,
        already_traded=False,
        books={
            "YES": yes or side("YES", ("0.50", "50")),
            "NO": no or side("NO", ("0.45", "50")),
        },
        equity=D("1000"),
        available_cash=D("1000"),
        exposure=ZERO_EXPOSURE,
        now=NOW,
    )
    return replace(base, **overrides)


def loose(params: PolicyParams = BOUNDS) -> PolicyParams:
    return replace(params, cap_market=D("1"), cap_event=D("1"), cap_category=D("1"),
                   cap_total_open=D("1"))


class SizingTests(unittest.TestCase):
    def test_spec_worked_example(self) -> None:
        # q=0.80, ask 0.50, equity $1,000: quarter Kelly is $150 (300 shares), the 2% market
        # cap limits the budget to $20, so 40 shares fill from the 50-share book.
        self.assertEqual(kelly(D("0.80"), D("0.50")), D("0.6"))
        self.assertEqual(budget_for(D("0.80"), D("0.50"), inputs(), loose()), D("150.00"))
        trade = decide(inputs(), BOUNDS)
        assert isinstance(trade, Trade), trade
        self.assertEqual((trade.outcome, trade.snapshot_id), ("YES", 1))
        self.assertEqual(trade.budget, D("20.00"))
        self.assertEqual(trade.walk.shares, D("40"))
        self.assertEqual(trade.walk.cost, D("20.00"))
        self.assertEqual(trade.edge, D("0.30"))

    def test_kelly_budget_is_limited_by_recorded_depth(self) -> None:
        trade = decide(inputs(), loose())
        assert isinstance(trade, Trade), trade
        self.assertEqual(trade.walk.shares, D("50"))  # $150 budget, only 50 shares exist

    def test_equity_shrinks_every_size(self) -> None:
        trade = decide(inputs(equity=D("500"), available_cash=D("500")), BOUNDS)
        assert isinstance(trade, Trade), trade
        self.assertEqual(trade.walk.shares, D("20"))  # 2% of $500 = $10

    def test_each_cap_and_cash_can_exhaust_the_budget(self) -> None:
        cases = {
            "market": Exposure(D("20"), D("20"), D("20"), D("20")),
            "event": Exposure(D("0"), D("50"), D("50"), D("50")),
            "category": Exposure(D("0"), D("0"), D("150"), D("150")),
            "total": Exposure(D("0"), D("0"), D("0"), D("500")),
        }
        for label, exposure in cases.items():
            with self.subTest(label):
                result = decide(inputs(exposure=exposure), BOUNDS)
                self.assertIsInstance(result, Refusal)
                self.assertEqual(result.reason, "NO_BUDGET")  # type: ignore[union-attr]
        broke = decide(inputs(available_cash=D("0")), BOUNDS)
        self.assertEqual(broke, Refusal("NO_BUDGET", broke.detail))  # type: ignore[union-attr]

    def test_cap_headroom_is_what_remains(self) -> None:
        trade = decide(inputs(exposure=Exposure(D("0"), D("44"), D("44"), D("44"))), BOUNDS)
        assert isinstance(trade, Trade), trade
        self.assertEqual(trade.budget, D("6.00"))  # event cap $50 - $44 open


class ProbabilityTests(unittest.TestCase):
    def test_primary_uses_conservative_bounds_and_shadow_uses_p_mid(self) -> None:
        narrow_edge = inputs(p=("0.52", "0.60", "0.70"))
        self.assertEqual(decide(narrow_edge, BOUNDS).reason, "NO_EDGE")  # type: ignore[union-attr]
        trade = decide(narrow_edge, MID)
        assert isinstance(trade, Trade), trade
        self.assertEqual((trade.outcome, trade.q), ("YES", D("0.60")))

    def test_no_side_uses_one_minus_p_high(self) -> None:
        result = decide(
            inputs(p=("0.10", "0.15", "0.20"), yes=side("YES", ("0.30", "50")),
                   no=side("NO", ("0.60", "50"))),
            BOUNDS,
        )
        assert isinstance(result, Trade), result
        self.assertEqual((result.outcome, result.q, result.edge), ("NO", D("0.80"), D("0.20")))

    def test_the_side_with_the_larger_realized_edge_wins(self) -> None:
        result = decide(
            inputs(p=("0.50", "0.50", "0.50"), yes=side("YES", ("0.40", "50")),
                   no=side("NO", ("0.30", "50"))),
            MID,
        )
        assert isinstance(result, Trade), result
        self.assertEqual(result.outcome, "NO")

    def test_fees_count_against_the_edge(self) -> None:
        politics = {"exponent": 1, "rate": 0.04, "takerOnly": True, "rebateRate": 0.25}
        yes = side("YES", ("0.50", "50"), fees_enabled=True, fee_schedule=politics)
        # q 0.555 - ask 0.50 - fee 0.04*0.5*0.5 (0.01) = 0.045 < 0.05.
        self.assertEqual(
            decide(inputs(p=("0.555", "0.6", "0.7"), yes=yes), BOUNDS).reason,  # type: ignore[union-attr]
            "NO_EDGE",
        )
        trade = decide(inputs(yes=yes), BOUNDS)
        assert isinstance(trade, Trade), trade
        self.assertEqual(trade.walk.fee, trade.walk.shares * D("0.01"))
        self.assertLessEqual(trade.walk.cost, trade.budget)


class BookTests(unittest.TestCase):
    def test_walk_stops_at_the_slippage_limit(self) -> None:
        yes = side("YES", ("0.52", "100"), ("0.50", "10"))
        trade = decide(inputs(yes=yes), BOUNDS)  # limit 0.50 * 1.02 = 0.51
        assert isinstance(trade, Trade), trade
        self.assertEqual(trade.walk.shares, D("10"))

    def test_minimum_order_size_is_met_in_shares_and_in_dollars(self) -> None:
        # Budget $4 at 0.50 buys 8 shares: >= 5 shares but < $5.
        tight = replace(BOUNDS, cap_market=D("0.004"))
        self.assertEqual(decide(inputs(), tight).reason, "BELOW_MIN_ORDER")  # type: ignore[union-attr]
        free = side("YES", ("0.50", "50"), min_order_size=D("0"))
        self.assertIsInstance(decide(inputs(yes=free), tight), Trade)

    def test_book_problems_refuse(self) -> None:
        cases = {
            "NO_DEPTH": side("YES"),
            "TICK_MISMATCH": side("YES", ("0.505", "50")),
        }
        for reason, yes in cases.items():
            with self.subTest(reason):
                result = decide(inputs(yes=yes), BOUNDS)
                self.assertIsInstance(result, Refusal)
                self.assertIn(reason, result.detail)  # type: ignore[union-attr]

    def test_stale_or_future_books_refuse(self) -> None:
        for fetched in (NOW - timedelta(seconds=121), NOW + timedelta(seconds=1)):
            with self.subTest(fetched=fetched):
                no = side("NO", ("0.45", "50"), fetched_at=fetched)
                self.assertEqual(decide(inputs(no=no), BOUNDS).reason, "STALE_BOOK")  # type: ignore[union-attr]

    def test_fees_enabled_without_a_rate_refuses(self) -> None:
        yes = side("YES", ("0.50", "50"), fees_enabled=True, fee_schedule=None)
        self.assertEqual(decide(inputs(yes=yes), BOUNDS).reason, "FEE_UNKNOWN")  # type: ignore[union-attr]


class GateTests(unittest.TestCase):
    def test_each_gate_refuses_with_its_code(self) -> None:
        forecast = inputs().forecast
        cases = {
            "ABSTAINED": inputs(forecast=replace(
                forecast, abstained=True, p_low=None, p_mid=None, p_high=None, confidence=None
            )),
            "RULES_CHANGED": inputs(current_rules_hash="r2"),
            "ELIGIBILITY_LOST": inputs(eligible=False),
            "RESOLUTION_STARTED": inputs(resolution_started=True),
            "ALREADY_TRADED": inputs(already_traded=True),
            "LOW_CONFIDENCE": inputs(forecast=replace(forecast, confidence="low")),
            "CLOSING_SOON": inputs(forecast=replace(
                forecast, end_date=NOW + timedelta(hours=47)
            )),
            "NO_BOOK": inputs(books={"YES": side("YES", ("0.5", "50"))}),
        }
        for reason, case in cases.items():
            with self.subTest(reason):
                self.assertEqual(decide(case, BOUNDS).reason, reason)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_policy -v`
Expected: ERROR `No module named 'predict_agent.policy'`.

- [ ] **Step 3: Implement**

`src/predict_agent/policy.py` (create):

```python
"""The paper policy (spec §5): forecast + post-forecast books + ledger state -> one trade
or one refusal. Pure: no I/O, no clock; every input arrives in PolicyInputs.

Sizing is decided before depth: budget = min(kelly_fraction x Kelly(q, a) x equity, every
cap's headroom, available cash) at the best ask `a`; the book is then walked up to a limit
price of a x (1 + max_slippage), fees included, and the realized edge q - c is rechecked."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .fills import AskLevel, FillError, Walk, fee_per_share, walk_asks
from .gamma import fee_rate
from .policy_params import CONFIDENCE_RANK, PolicyParams

ONE = Decimal("1")
ZERO = Decimal("0")


@dataclass(frozen=True)
class SideBook:
    """One token's post-forecast snapshot, as stored."""

    outcome: str  # "YES" | "NO"
    snapshot_id: int
    fetched_at: datetime
    asks: tuple[AskLevel, ...]
    tick_size: Decimal
    min_order_size: Decimal
    fees_enabled: bool
    fee_schedule: Mapping[str, object] | None


@dataclass(frozen=True)
class ForecastView:
    abstained: bool
    p_low: Decimal | None
    p_mid: Decimal | None
    p_high: Decimal | None
    confidence: str | None
    rules_hash: str
    end_date: datetime | None  # from the forecast's own rules version


@dataclass(frozen=True)
class Exposure:
    """Cost basis of this portfolio's OPEN tickets, grouped as the caps are."""

    market: Decimal
    event: Decimal
    category: Decimal
    total: Decimal


@dataclass(frozen=True)
class PolicyInputs:
    forecast: ForecastView
    current_rules_hash: str
    eligible: bool  # in the latest completed discovery run
    resolution_started: bool  # latest resolution observation is no longer `posed`
    already_traded: bool  # this portfolio holds or held a ticket on the market
    books: Mapping[str, SideBook]  # "YES" and "NO"
    equity: Decimal
    available_cash: Decimal
    exposure: Exposure
    now: datetime


@dataclass(frozen=True)
class Trade:
    outcome: str
    snapshot_id: int
    q: Decimal
    best_ask: Decimal
    budget: Decimal
    walk: Walk

    @property
    def edge(self) -> Decimal:
        return self.q - self.walk.cost_per_share


@dataclass(frozen=True)
class Refusal:
    reason: str
    detail: str


def kelly(q: Decimal, price: Decimal) -> Decimal:
    return (q - price) / (ONE - price)


def side_probabilities(forecast: ForecastView, source: str) -> dict[str, Decimal]:
    """q per side: conservative bounds for `bounds`, p_mid for `mid`."""
    if forecast.p_low is None or forecast.p_mid is None or forecast.p_high is None:
        raise ValueError("an abstained forecast has no side probabilities")
    if source == "bounds":
        return {"YES": forecast.p_low, "NO": ONE - forecast.p_high}
    return {"YES": forecast.p_mid, "NO": ONE - forecast.p_mid}


def _headroom(cap: Decimal, equity: Decimal, used: Decimal) -> Decimal:
    return max(ZERO, cap * equity - used)


def budget_for(q: Decimal, ask: Decimal, inputs: PolicyInputs, params: PolicyParams) -> Decimal:
    exposure = inputs.exposure
    return max(
        ZERO,
        min(
            params.kelly_fraction * kelly(q, ask) * inputs.equity,
            _headroom(params.cap_market, inputs.equity, exposure.market),
            _headroom(params.cap_event, inputs.equity, exposure.event),
            _headroom(params.cap_category, inputs.equity, exposure.category),
            _headroom(params.cap_total_open, inputs.equity, exposure.total),
            inputs.available_cash,
        ),
    )


def _gate(inputs: PolicyInputs, params: PolicyParams) -> Refusal | None:
    """Refusals that do not depend on the book walk (spec §5 step 1, plus freshness)."""
    forecast = inputs.forecast
    if forecast.abstained:
        return Refusal("ABSTAINED", "forecast abstained")
    if forecast.rules_hash != inputs.current_rules_hash:
        return Refusal("RULES_CHANGED", "rules version changed since the forecast")
    if not inputs.eligible:
        return Refusal("ELIGIBILITY_LOST", "not in the latest completed discovery run")
    if inputs.resolution_started:
        return Refusal("RESOLUTION_STARTED", "resolution is no longer posed")
    if inputs.already_traded:
        return Refusal("ALREADY_TRADED", "one entry per market per portfolio, ever")
    if CONFIDENCE_RANK.get(forecast.confidence or "", -1) < CONFIDENCE_RANK[
        params.min_confidence
    ]:
        return Refusal("LOW_CONFIDENCE", f"confidence {forecast.confidence}")
    if forecast.end_date is None or forecast.end_date - inputs.now < timedelta(
        hours=params.min_hours_to_close
    ):
        return Refusal("CLOSING_SOON", f"end date {forecast.end_date}")
    if set(inputs.books) != {"YES", "NO"}:
        return Refusal("NO_BOOK", "both post-forecast snapshots are required")
    for book in inputs.books.values():
        age = inputs.now - book.fetched_at
        if age > timedelta(seconds=params.max_book_age_seconds) or age < timedelta(0):
            return Refusal("STALE_BOOK", f"{book.outcome} book age {age}")
        if book.fees_enabled and fee_rate(book.fee_schedule) is None:
            return Refusal("FEE_UNKNOWN", f"{book.outcome} book has fees but no fee rate")
    return None


def _rate(book: SideBook) -> Decimal:
    if not book.fees_enabled:
        return ZERO
    rate = fee_rate(book.fee_schedule)
    if rate is None:  # unreachable: _gate refuses it
        raise ValueError("fee rate missing")
    return rate


def _try_side(
    book: SideBook, q: Decimal, inputs: PolicyInputs, params: PolicyParams
) -> Trade | Refusal:
    if not book.asks:
        return Refusal("NO_DEPTH", f"{book.outcome} book has no asks")
    rate = _rate(book)
    best_ask = min(level.price for level in book.asks)
    provisional = q - best_ask - fee_per_share(best_ask, rate)
    if provisional < params.min_edge:
        return Refusal("NO_EDGE", f"{book.outcome} provisional edge {provisional}")
    budget = budget_for(q, best_ask, inputs, params)
    if budget <= 0:
        return Refusal("NO_BUDGET", f"{book.outcome} cap headroom or cash exhausted")
    try:
        walk = walk_asks(
            book.asks,
            budget=budget,
            fee_rate=rate,
            limit_price=best_ask * (ONE + params.max_slippage),
            tick_size=book.tick_size,
        )
    except FillError as error:
        return Refusal(error.code, f"{book.outcome}: {error}")
    if walk.shares <= 0:
        return Refusal("NO_DEPTH", f"{book.outcome} budget {budget} buys no shares")
    # The minimum order size's unit is contradictory in the docs (shares vs USDC):
    # satisfy both readings.
    if walk.shares < book.min_order_size or walk.cost < book.min_order_size:
        return Refusal(
            "BELOW_MIN_ORDER",
            f"{book.outcome} {walk.shares} shares / {walk.cost} below {book.min_order_size}",
        )
    trade = Trade(book.outcome, book.snapshot_id, q, best_ask, budget, walk)
    if trade.edge < params.min_edge:
        return Refusal("NO_EDGE_AFTER_FILL", f"{book.outcome} realized edge {trade.edge}")
    return trade


def decide(inputs: PolicyInputs, params: PolicyParams) -> Trade | Refusal:
    """At most one side per market: the side with the larger realized edge."""
    refusal = _gate(inputs, params)
    if refusal is not None:
        return refusal
    q_by_side = side_probabilities(inputs.forecast, params.probability)
    results = [
        _try_side(inputs.books[outcome], q_by_side[outcome], inputs, params)
        for outcome in ("YES", "NO")
    ]
    trades = [result for result in results if isinstance(result, Trade)]
    if trades:
        return max(trades, key=lambda trade: (trade.edge, trade.outcome == "YES"))
    refusals = [result for result in results if isinstance(result, Refusal)]
    # A side with no edge at all says least; report the side that got further (YES first).
    telling = [r for r in refusals if r.reason != "NO_EDGE"] or refusals
    return Refusal(telling[0].reason, "; ".join(f"{r.reason}: {r.detail}" for r in refusals))
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_policy -v` → 15 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`

```bash
git add src/predict_agent/policy.py tests/predict/test_policy.py
git commit -m "feat(predict): pure paper policy with Kelly sizing, caps and side choice"
```

---

### Task 4: Paper trading through the ledger

**Files:**
- Create: `src/predict_agent/paper.py`, `tests/predict/trade_fixtures.py`, `tests/predict/test_paper.py`
- Modify: `src/predict_agent/tickets.py`, `tests/predict/ledger_fixtures.py`

**Interfaces:**
- Consumes: `policy.*` (Task 3), `policy_params.policy_from_artifact`, `variant_policies`, `parse_policy` (Task 1); Plan 2 `artifacts.load_artifact`, `cash.available_cash`, `forecasts.resume_step`, `undecided_portfolios`, `unfinished_forecasts`, `tickets.equity`, `db.transaction`.
- Produces:
  - `tickets.open_ticket_locked(conn, draft, now) -> int` and `tickets.record_refusal_locked(conn, portfolio_id, forecast_id, reason, now, detail="")` — the existing bodies, for a caller that holds the transaction; `open_ticket` / `record_refusal_decision(..., detail="")` now wrap them. The refusal journal payload gains `detail`.
  - `paper.COHORT_CLOSED`, `paper.DecisionResult(portfolio_id, forecast_id, ticket_id: int | None, reason: str | None, detail: str)`, `paper.TradeSummary(traded: int, refused: dict[str, int], waiting: int)`
  - `paper.latest_discovery_run(conn) -> str | None`, `paper.load_inputs(conn, portfolio_id, forecast_id, now) -> PolicyInputs`
  - `paper.decide_portfolio(conn, portfolio_id, forecast_id, now) -> DecisionResult` (one transaction: inputs read, decision, ticket or refusal written)
  - `paper.trade_ready(conn, now) -> TradeSummary` (every cohort; only `NEEDS_DECISION` forecasts; rerun-safe)
  - Fixtures: `ledger_fixtures.seed_snapshot(conn, outcome, fetched_at, condition_id=..., *, asks=None, min_order_size="0", tick_size="0.01", fee_schedule=None)` now stores real ask levels; `ledger_fixtures.PRIMARY_POLICY`/`SHADOW_POLICY` are the example config's real artifacts. `trade_fixtures.seed_policy_cohort`, `seed_tradeable_market`, `seed_discovery`, `seed_ready_forecast`, `POLICY_SECTION`, `POLITICS_FEES`


- [ ] **Step 1: Write the failing tests**

Apply to `tests/predict/ledger_fixtures.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/tests/predict/ledger_fixtures.py b/tests/predict/ledger_fixtures.py
index 8264f3d..8cedcaf 100644
--- a/tests/predict/ledger_fixtures.py
+++ b/tests/predict/ledger_fixtures.py
@@ -2,11 +2,13 @@

 from __future__ import annotations

+import json
 import sqlite3
 import uuid
 from dataclasses import replace
 from datetime import datetime, timedelta
 from decimal import Decimal
+from pathlib import Path
 from typing import Any

 from predict_agent.artifacts import store_artifact
@@ -17,12 +19,24 @@ from predict_agent.forecasts import (
     record_forecast,
     start_attempt,
 )
+from predict_agent.policy_params import parse_policy, variant_policies
 from predict_agent.tickets import Fill, TicketDraft
 from predict_agent.util import canonical_json, isoformat, sha256_json
 from tests.predict.fixtures import CONDITION_ID, NO_TOKEN, NOW, YES_TOKEN

-PRIMARY_POLICY = '{"min_edge": "0.05", "price": "bounds"}'
-SHADOW_POLICY = '{"min_edge": "0.05", "price": "p_mid"}'
+_POLICIES = variant_policies(
+    parse_policy(
+        json.loads(
+            (Path(__file__).resolve().parents[2] / "config" / "predict-policy.example.json")
+            .read_text(encoding="utf-8")
+        )["policy"],
+        "bounds",
+    )
+)
+PRIMARY_POLICY = _POLICIES["primary"]
+SHADOW_POLICY = _POLICIES["shadow_mid"]
+# Recorded asks deep enough for every draft the ledger tests open.
+BOOK_RECORD_ASKS = [["0.40", "1000"], ["0.41", "1000"]]
 WINDOW_SECONDS = 1800


@@ -61,18 +75,40 @@ def seed_snapshot(
     outcome: str,
     fetched_at: datetime,
     condition_id: str = CONDITION_ID,
+    *,
+    asks: list[tuple[str, str]] | None = None,
+    min_order_size: str = "0",
+    tick_size: str = "0.01",
+    fee_schedule: dict[str, Any] | None = None,
 ) -> int:
+    """A stored snapshot whose record carries real ask levels, best-first as Plan 1 stores
+    them (default: deep enough for every draft the ledger tests open)."""
     token = YES_TOKEN if outcome == "YES" else NO_TOKEN
+    levels = [list(level) for level in asks] if asks is not None else BOOK_RECORD_ASKS
+    record = {
+        "condition_id": condition_id,
+        "token_id": token,
+        "observed_at": isoformat(fetched_at),
+        "fetched_at": isoformat(fetched_at),
+        "book_hash": "h",
+        "bids": [["0.01", "100"]],
+        "asks": sorted(levels, key=lambda level: Decimal(level[0])),
+        "tick_size": tick_size,
+        "min_order_size": min_order_size,
+    }
     cursor = conn.execute(
         "INSERT INTO book_snapshots (run_id, condition_id, token_id, source_run_id, outcome, "
         "observed_at, fetched_at, record_json, fees_enabled, fee_schedule_json, snapshot_hash) "
-        "VALUES ('r', ?, ?, 'r', ?, ?, ?, '{}', 0, NULL, ?)",
+        "VALUES ('r', ?, ?, 'r', ?, ?, ?, ?, ?, ?, ?)",
         (
             condition_id,
             token,
             outcome,
             isoformat(fetched_at),
             isoformat(fetched_at),
+            canonical_json(record),
+            int(fee_schedule is not None),
+            canonical_json(fee_schedule) if fee_schedule is not None else None,
             uuid.uuid4().hex,
         ),
     )
```

`tests/predict/trade_fixtures.py` (create):

```python
"""Seed helpers for paper-trading tests: cohorts with real policy artifacts, tradeable
markets, discovery runs and baselined forecasts. Builds on ledger_fixtures."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from predict_agent.artifacts import store_artifact
from predict_agent.cohorts import CohortIdentity, ensure_cohort
from predict_agent.forecasts import attach_baseline
from predict_agent.policy_params import parse_policy, variant_policies
from predict_agent.util import canonical_json, isoformat, sha256_json
from tests.predict.fixtures import CONDITION_ID, NO_TOKEN, NOW, YES_TOKEN
from tests.predict.ledger_fixtures import WINDOW_SECONDS, seed_entry_forecast, seed_snapshot

REPO = Path(__file__).resolve().parents[2]
POLICY_SECTION: dict[str, Any] = json.loads(
    (REPO / "config" / "predict-policy.example.json").read_text(encoding="utf-8")
)["policy"]
POLITICS_FEES = {"exponent": 1, "rate": 0.04, "takerOnly": True, "rebateRate": 0.25}


def seed_policy_cohort(conn: sqlite3.Connection, *, model_id: str = "m1") -> str:
    """A cohort whose primary and shadow_mid portfolios carry the example config's policy."""
    params = parse_policy(POLICY_SECTION, "bounds")
    portfolios = {
        variant: store_artifact(conn, "policy", content, NOW)
        for variant, content in variant_policies(params).items()
    }
    identity = CohortIdentity(
        portfolios=portfolios,
        prompt_hash=store_artifact(conn, "prompt", "Forecast without market prices.", NOW),
        model_id=model_id,
        research_settings={"tools": ["WebSearch", "WebFetch"]},
        scoring_version="1",
        baseline_window_seconds=WINDOW_SECONDS,
    )
    return ensure_cohort(
        conn,
        identity,
        starting_bankroll=params.starting_bankroll,
        code_version="test",
        now=NOW,
    )


def seed_tradeable_market(
    conn: sqlite3.Connection,
    condition_id: str = CONDITION_ID,
    *,
    event_id: str = "e1",
    category: str = "politics",
    end_date: datetime = NOW + timedelta(days=20),
) -> str:
    """A market with its rules version and markets row; returns the rules hash."""
    payload = {
        "question": f"Will {condition_id[:8]} happen?",
        "rules_text": "Resolves Yes if it happens by the end date.",
        "resolution_source": "",
        "end_date": isoformat(end_date),
    }
    rules_hash = sha256_json(payload)
    conn.execute(
        "INSERT INTO rules_versions (condition_id, rules_hash, rules_json, first_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (condition_id, rules_hash, canonical_json(payload), isoformat(NOW)),
    )
    conn.execute(
        "INSERT INTO markets (condition_id, event_id, question, category, yes_token_id, "
        "no_token_id, current_rules_hash, fees_enabled, fee_schedule_json, first_seen_at, "
        "last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)",
        (
            condition_id,
            event_id,
            payload["question"],
            category,
            YES_TOKEN,
            NO_TOKEN,
            rules_hash,
            isoformat(NOW),
            isoformat(NOW),
        ),
    )
    return rules_hash


def seed_discovery(
    conn: sqlite3.Connection, condition_ids: list[str], *, at: datetime = NOW
) -> str:
    """A COMPLETED discovery run that found these markets eligible."""
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO runs (run_id, command, started_at, policy_hash, status, finished_at) "
        "VALUES (?, 'discover', ?, 'p', 'COMPLETED', ?)",
        (run_id, isoformat(at), isoformat(at)),
    )
    for condition_id in condition_ids:
        market = conn.execute(
            "SELECT * FROM markets WHERE condition_id = ?", (condition_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO discoveries (run_id, condition_id, event_id, question, category, "
            "yes_token_id, no_token_id, rules_hash, fees_enabled, fee_schedule_json, "
            "observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)",
            (
                run_id,
                condition_id,
                market["event_id"],
                market["question"],
                market["category"],
                market["yes_token_id"],
                market["no_token_id"],
                market["current_rules_hash"],
                isoformat(at),
            ),
        )
    return run_id


def seed_ready_forecast(
    conn: sqlite3.Connection,
    cohort_id: str,
    rules_hash: str,
    *,
    condition_id: str = CONDITION_ID,
    at: datetime = NOW,
    yes_asks: list[tuple[str, str]] | None = None,
    no_asks: list[tuple[str, str]] | None = None,
    p: tuple[str, str, str] = ("0.80", "0.85", "0.90"),
    fee_schedule: dict[str, Any] | None = None,
) -> int:
    """An entry forecast with both baseline books attached one second after it."""
    forecast_id = seed_entry_forecast(
        conn,
        cohort_id,
        rules_hash,
        at=at,
        condition_id=condition_id,
        p_low=Decimal(p[0]),
        p_mid=Decimal(p[1]),
        p_high=Decimal(p[2]),
    )
    later = at + timedelta(seconds=1)
    yes = seed_snapshot(conn, "YES", later, condition_id, asks=yes_asks or [("0.50", "50")],
                        min_order_size="5", fee_schedule=fee_schedule)
    no = seed_snapshot(conn, "NO", later, condition_id, asks=no_asks or [("0.45", "50")],
                       min_order_size="5", fee_schedule=fee_schedule)
    attach_baseline(conn, forecast_id, yes, no, later)
    return forecast_id
```

`tests/predict/test_paper.py` (create):

```python
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from predict_agent.cash import available_cash
from predict_agent.cohorts import cohort_portfolios
from predict_agent.db import connect
from predict_agent.forecasts import ResumeStep, resume_step
from predict_agent.invariants import verify_ledger
from predict_agent.paper import decide_portfolio, trade_ready
from predict_agent.settlement import settle_open_tickets
from predict_agent.tickets import equity
from tests.predict.fixtures import CONDITION_ID, NOW
from tests.predict.ledger_fixtures import seed_cohort, seed_entry_forecast, seed_observation
from tests.predict.trade_fixtures import (
    POLITICS_FEES,
    seed_discovery,
    seed_policy_cohort,
    seed_ready_forecast,
    seed_tradeable_market,
)

DECIDE_AT = NOW + timedelta(seconds=30)
D = Decimal


class PaperTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.rules_hash = seed_tradeable_market(self.conn)
        seed_discovery(self.conn, [CONDITION_ID])
        self.cohort = seed_policy_cohort(self.conn)
        self.portfolios = cohort_portfolios(self.conn, self.cohort)
        self.primary = self.portfolios["primary"]
        self.shadow = self.portfolios["shadow_mid"]

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def decisions(self) -> dict[str, tuple[str, str | None]]:
        rows = self.conn.execute("SELECT portfolio_id, kind, reason FROM decisions").fetchall()
        return {row["portfolio_id"]: (row["kind"], row["reason"]) for row in rows}

    def ticket(self, portfolio_id: str) -> dict[str, str]:
        row = self.conn.execute(
            "SELECT outcome, shares, cost_total, fills_json FROM paper_tickets "
            "WHERE portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()
        return dict(row)


class TradeTests(PaperTestCase):
    def test_spec_worked_example_end_to_end_in_both_portfolios(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        summary = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual((summary.traded, summary.refused, summary.waiting), (2, {}, 0))
        ticket = self.ticket(self.primary)
        self.assertEqual(ticket["outcome"], "YES")
        self.assertEqual(D(ticket["shares"]), D("40"))
        self.assertEqual(D(ticket["cost_total"]), D("20"))
        fills = [(D(p), D(s)) for p, s in json.loads(ticket["fills_json"])]
        self.assertEqual(fills, [(D("0.50"), D("40"))])
        self.assertEqual(available_cash(self.conn, self.primary), D("980"))
        self.assertEqual(available_cash(self.conn, self.shadow), D("980"))
        self.assertEqual(verify_ledger(self.conn), [])

    def test_primary_refuses_where_the_shadow_mid_policy_trades(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash, p=("0.52", "0.60", "0.70"))
        summary = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual((summary.traded, summary.refused), (1, {"NO_EDGE": 1}))
        self.assertEqual(
            self.decisions(),
            {self.primary: ("REFUSED", "NO_EDGE"), self.shadow: ("TRADED", None)},
        )
        self.assertEqual(available_cash(self.conn, self.primary), D("1000"))
        self.assertEqual(verify_ledger(self.conn), [])

    def test_fees_are_charged_per_level_from_the_snapshot_schedule(self) -> None:
        seed_ready_forecast(
            self.conn, self.cohort, self.rules_hash, fee_schedule=POLITICS_FEES,
            yes_asks=[("0.50", "20"), ("0.51", "100")],
        )
        trade_ready(self.conn, DECIDE_AT)
        row = self.conn.execute(
            "SELECT fee, cost_total, fills_json FROM paper_tickets WHERE portfolio_id = ?",
            (self.primary,),
        ).fetchone()
        fills = [(D(p), D(s)) for p, s in json.loads(row["fills_json"])]
        expected = sum((s * D("0.04") * p * (1 - p) for p, s in fills), D("0"))
        self.assertEqual(D(row["fee"]), expected)
        self.assertLessEqual(D(row["cost_total"]), D("20"))
        self.assertEqual(verify_ledger(self.conn), [])

    def test_refusals_are_recorded_with_code_and_journal_detail(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        seed_discovery(self.conn, [], at=NOW + timedelta(seconds=5))  # market dropped out
        summary = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual(summary.refused, {"ELIGIBILITY_LOST": 2})
        payload = json.loads(
            self.conn.execute(
                "SELECT payload_json FROM journal WHERE kind = 'DECISION_REFUSED' LIMIT 1"
            ).fetchone()[0]
        )
        self.assertIn("discovery run", payload["detail"])

    def test_resolution_started_and_rules_changed_refuse(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        seed_observation(self.conn, "YES", fetched_at=NOW + timedelta(seconds=10))
        self.assertEqual(trade_ready(self.conn, DECIDE_AT).refused, {"RESOLUTION_STARTED": 2})

    def test_a_late_decision_on_a_stale_book_refuses_and_is_final(self) -> None:
        forecast = seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        late = NOW + timedelta(minutes=10)
        self.assertEqual(trade_ready(self.conn, late).refused, {"STALE_BOOK": 2})
        self.assertEqual(resume_step(self.conn, forecast, late), ResumeStep.DONE)

    def test_closed_cohort_forecasts_are_refused_cohort_closed(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        seed_cohort(self.conn, model_id="successor")
        summary = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual((summary.traded, summary.refused), (0, {"COHORT_CLOSED": 2}))

    def test_rerun_is_idempotent_and_unbaselined_forecasts_wait(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        other = "0x" + "4" * 64
        seed_entry_forecast(
            self.conn, self.cohort, seed_tradeable_market(self.conn, other), condition_id=other
        )
        first = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual((first.traded, first.waiting), (2, 1))
        second = trade_ready(self.conn, DECIDE_AT)
        self.assertEqual((second.traded, second.refused, second.waiting), (0, {}, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_tickets").fetchone()[0], 2)

    def test_failure_while_writing_leaves_no_decision_and_can_be_retried(self) -> None:
        forecast = seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        with (
            mock.patch("predict_agent.tickets.append_journal", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            decide_portfolio(self.conn, self.primary, forecast, DECIDE_AT)
        self.assertEqual(self.decisions(), {})
        self.assertEqual(available_cash(self.conn, self.primary), D("1000"))
        result = decide_portfolio(self.conn, self.primary, forecast, DECIDE_AT)
        self.assertIsNotNone(result.ticket_id)


class PortfolioStateTests(PaperTestCase):
    def test_losses_shrink_equity_and_the_next_size(self) -> None:
        seed_ready_forecast(self.conn, self.cohort, self.rules_hash)
        trade_ready(self.conn, DECIDE_AT)
        seed_observation(self.conn, "NO", fetched_at=NOW + timedelta(days=1))
        settle_open_tickets(self.conn, NOW + timedelta(days=1))
        self.assertEqual(equity(self.conn, self.primary), D("980"))
        later = NOW + timedelta(days=2)
        other = "0x" + "6" * 64
        rules = seed_tradeable_market(self.conn, other, event_id="e2",
                                      end_date=later + timedelta(days=20))
        seed_discovery(self.conn, [other], at=later)
        seed_ready_forecast(self.conn, self.cohort, rules, condition_id=other, at=later)
        trade_ready(self.conn, later + timedelta(seconds=30))
        row = self.conn.execute(
            "SELECT cost_total FROM paper_tickets WHERE portfolio_id = ? AND condition_id = ?",
            (self.primary, other),
        ).fetchone()
        self.assertEqual(D(row["cost_total"]), D("19.60"))  # 2% of $980

    def test_event_cap_counts_open_tickets_in_the_same_event(self) -> None:
        markets = ["0x" + digit * 64 for digit in "abc"]
        for condition_id in markets:
            rules = seed_tradeable_market(self.conn, condition_id, event_id="shared")
            seed_ready_forecast(self.conn, self.cohort, rules, condition_id=condition_id)
        seed_discovery(self.conn, markets)
        trade_ready(self.conn, DECIDE_AT)
        costs = [
            D(row["cost_total"])
            for row in self.conn.execute(
                "SELECT cost_total FROM paper_tickets WHERE portfolio_id = ? ORDER BY ticket_id",
                (self.primary,),
            )
        ]
        # 2% market cap each ($20), 5% event cap ($50): $20 + $20 + $10.
        self.assertEqual(costs, [D("20"), D("20"), D("10")])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_paper -v`
Expected: ERROR `No module named 'predict_agent.paper'`.

- [ ] **Step 3: Implement**

Apply to `src/predict_agent/tickets.py` (`git apply` accepts this hunk as written) — mostly re-indentation: the two bodies move out of their `with transaction(conn):` blocks into `*_locked` functions:

```diff
diff --git a/src/predict_agent/tickets.py b/src/predict_agent/tickets.py
index bbda84a..fc4739a 100644
--- a/src/predict_agent/tickets.py
+++ b/src/predict_agent/tickets.py
@@ -94,133 +94,161 @@ def _decided(conn: sqlite3.Connection, portfolio_id: str, forecast_id: int) -> b
     return row.fetchone() is not None


-def open_ticket(conn: sqlite3.Connection, draft: TicketDraft, now: datetime) -> int:
+def open_ticket_locked(conn: sqlite3.Connection, draft: TicketDraft, now: datetime) -> int:
+    """open_ticket for a caller that already holds the transaction."""
     shares, cost_total = ticket_totals(draft)
-    with transaction(conn):
-        portfolio = _portfolio(conn, draft.portfolio_id)
-        if portfolio["cohort_status"] != "ACTIVE":
-            raise LedgerError(f"portfolio {draft.portfolio_id[:12]}'s cohort is not active")
-        if draft.policy_hash != portfolio["policy_hash"]:
-            raise LedgerError("ticket policy differs from the portfolio's frozen policy")
-        forecast = conn.execute(
-            "SELECT * FROM forecasts WHERE forecast_id = ?", (draft.forecast_id,)
-        ).fetchone()
-        if (
-            forecast is None
-            or forecast["cohort_id"] != portfolio["cohort_id"]
-            or forecast["condition_id"] != draft.condition_id
-            or forecast["kind"] != "entry"
-            or forecast["abstained"]
-        ):
-            raise LedgerError("ticket needs a non-abstained entry forecast of this cohort/market")
-        if forecast["rules_hash"] != draft.rules_hash:
-            raise LedgerError("ticket rules hash differs from the forecast's rules version")
-        if _decided(conn, draft.portfolio_id, draft.forecast_id):
-            raise LedgerError(f"forecast {draft.forecast_id} is already decided here")
-        baseline = conn.execute(
-            "SELECT yes_snapshot_id, no_snapshot_id FROM forecast_baselines "
-            "WHERE forecast_id = ? AND reason IS NULL",
-            (draft.forecast_id,),
-        ).fetchone()
-        side_snapshot = None
-        if baseline is not None:
-            column = "yes_snapshot_id" if draft.outcome == "YES" else "no_snapshot_id"
-            side_snapshot = baseline[column]
-        if side_snapshot != draft.snapshot_id:
-            raise LedgerError(
-                "ticket snapshot must be the forecast's baseline snapshot for the bought side"
-            )
-        values: dict[str, Any] = {
+    portfolio = _portfolio(conn, draft.portfolio_id)
+    if portfolio["cohort_status"] != "ACTIVE":
+        raise LedgerError(f"portfolio {draft.portfolio_id[:12]}'s cohort is not active")
+    if draft.policy_hash != portfolio["policy_hash"]:
+        raise LedgerError("ticket policy differs from the portfolio's frozen policy")
+    forecast = conn.execute(
+        "SELECT * FROM forecasts WHERE forecast_id = ?", (draft.forecast_id,)
+    ).fetchone()
+    if (
+        forecast is None
+        or forecast["cohort_id"] != portfolio["cohort_id"]
+        or forecast["condition_id"] != draft.condition_id
+        or forecast["kind"] != "entry"
+        or forecast["abstained"]
+    ):
+        raise LedgerError("ticket needs a non-abstained entry forecast of this cohort/market")
+    if forecast["rules_hash"] != draft.rules_hash:
+        raise LedgerError("ticket rules hash differs from the forecast's rules version")
+    if _decided(conn, draft.portfolio_id, draft.forecast_id):
+        raise LedgerError(f"forecast {draft.forecast_id} is already decided here")
+    baseline = conn.execute(
+        "SELECT yes_snapshot_id, no_snapshot_id FROM forecast_baselines "
+        "WHERE forecast_id = ? AND reason IS NULL",
+        (draft.forecast_id,),
+    ).fetchone()
+    side_snapshot = None
+    if baseline is not None:
+        column = "yes_snapshot_id" if draft.outcome == "YES" else "no_snapshot_id"
+        side_snapshot = baseline[column]
+    if side_snapshot != draft.snapshot_id:
+        raise LedgerError(
+            "ticket snapshot must be the forecast's baseline snapshot for the bought side"
+        )
+    values: dict[str, Any] = {
+        "portfolio_id": draft.portfolio_id,
+        "forecast_id": draft.forecast_id,
+        "snapshot_id": draft.snapshot_id,
+        "condition_id": draft.condition_id,
+        "outcome": draft.outcome,
+        "direction": "BUY",
+        "shares": money_text(shares),
+        "fills_json": canonical_json(
+            [[money_text(f.price), money_text(f.shares)] for f in draft.fills]
+        ),
+        "fee": money_text(draft.fee),
+        "cost_total": money_text(cost_total),
+        "policy_hash": draft.policy_hash,
+        "rules_hash": draft.rules_hash,
+        "created_at": isoformat(now),
+    }
+    values["ticket_hash"] = ticket_hash(values)
+    values["status"] = "OPEN"
+    columns = ", ".join(values)
+    placeholders = ", ".join("?" for _ in values)
+    try:
+        cursor = conn.execute(
+            f"INSERT INTO paper_tickets ({columns}) VALUES ({placeholders})",
+            tuple(values.values()),
+        )
+    except sqlite3.IntegrityError as error:
+        raise LedgerError(
+            f"ticket insert refused (market already traded in this portfolio?): {error}"
+        ) from None
+    if cursor.lastrowid is None:
+        raise LedgerError("ticket insert returned no row id")
+    ticket_id = cursor.lastrowid
+    append_cash_entry(conn, draft.portfolio_id, "DEBIT", cost_total, ticket_id, now)
+    conn.execute(
+        "INSERT INTO decisions (portfolio_id, forecast_id, condition_id, kind, reason, "
+        "ticket_id, decided_at) VALUES (?, ?, ?, 'TRADED', NULL, ?, ?)",
+        (draft.portfolio_id, draft.forecast_id, draft.condition_id, ticket_id, isoformat(now)),
+    )
+    append_journal(
+        conn,
+        "TICKET_OPENED",
+        {
+            "ticket_id": ticket_id,
             "portfolio_id": draft.portfolio_id,
-            "forecast_id": draft.forecast_id,
-            "snapshot_id": draft.snapshot_id,
             "condition_id": draft.condition_id,
             "outcome": draft.outcome,
-            "direction": "BUY",
-            "shares": money_text(shares),
-            "fills_json": canonical_json(
-                [[money_text(f.price), money_text(f.shares)] for f in draft.fills]
-            ),
-            "fee": money_text(draft.fee),
-            "cost_total": money_text(cost_total),
-            "policy_hash": draft.policy_hash,
-            "rules_hash": draft.rules_hash,
-            "created_at": isoformat(now),
-        }
-        values["ticket_hash"] = ticket_hash(values)
-        values["status"] = "OPEN"
-        columns = ", ".join(values)
-        placeholders = ", ".join("?" for _ in values)
-        try:
-            cursor = conn.execute(
-                f"INSERT INTO paper_tickets ({columns}) VALUES ({placeholders})",
-                tuple(values.values()),
-            )
-        except sqlite3.IntegrityError as error:
-            raise LedgerError(
-                f"ticket insert refused (market already traded in this portfolio?): {error}"
-            ) from None
-        if cursor.lastrowid is None:
-            raise LedgerError("ticket insert returned no row id")
-        ticket_id = cursor.lastrowid
-        append_cash_entry(conn, draft.portfolio_id, "DEBIT", cost_total, ticket_id, now)
-        conn.execute(
-            "INSERT INTO decisions (portfolio_id, forecast_id, condition_id, kind, reason, "
-            "ticket_id, decided_at) VALUES (?, ?, ?, 'TRADED', NULL, ?, ?)",
-            (draft.portfolio_id, draft.forecast_id, draft.condition_id, ticket_id, isoformat(now)),
-        )
-        append_journal(
-            conn,
-            "TICKET_OPENED",
-            {
-                "ticket_id": ticket_id,
-                "portfolio_id": draft.portfolio_id,
-                "condition_id": draft.condition_id,
-                "outcome": draft.outcome,
-                "cost_total": values["cost_total"],
-                "ticket_hash": values["ticket_hash"],
-            },
-            now,
-        )
+            "cost_total": values["cost_total"],
+            "ticket_hash": values["ticket_hash"],
+        },
+        now,
+    )
     return ticket_id


-def record_refusal_decision(
-    conn: sqlite3.Connection, portfolio_id: str, forecast_id: int, reason: str, now: datetime
+def open_ticket(conn: sqlite3.Connection, draft: TicketDraft, now: datetime) -> int:
+    with transaction(conn):
+        return open_ticket_locked(conn, draft, now)
+
+
+def record_refusal_locked(
+    conn: sqlite3.Connection,
+    portfolio_id: str,
+    forecast_id: int,
+    reason: str,
+    now: datetime,
+    detail: str = "",
 ) -> None:
+    """record_refusal_decision for a caller that already holds the transaction. `reason`
+    is the code stored on the decision; `detail` goes to the journal."""
     if not reason:
         raise LedgerError("a refusal decision needs a reason")
-    with transaction(conn):
-        portfolio = _portfolio(conn, portfolio_id)
-        forecast = conn.execute(
-            "SELECT cohort_id, condition_id, kind FROM forecasts WHERE forecast_id = ?",
-            (forecast_id,),
-        ).fetchone()
-        if (
-            forecast is None
-            or forecast["kind"] != "entry"
-            or forecast["cohort_id"] != portfolio["cohort_id"]
-        ):
-            raise LedgerError(f"forecast {forecast_id} is not an entry forecast of this cohort")
-        if _decided(conn, portfolio_id, forecast_id):
-            raise LedgerError(f"forecast {forecast_id} is already decided here")
-        if not conn.execute(
-            "SELECT 1 FROM forecast_baselines WHERE forecast_id = ?", (forecast_id,)
-        ).fetchone():
-            raise LedgerError(
-                f"forecast {forecast_id} has no baseline yet; decisions follow the baseline"
-            )
-        conn.execute(
-            "INSERT INTO decisions (portfolio_id, forecast_id, condition_id, kind, reason, "
-            "ticket_id, decided_at) VALUES (?, ?, ?, 'REFUSED', ?, NULL, ?)",
-            (portfolio_id, forecast_id, forecast["condition_id"], reason, isoformat(now)),
-        )
-        append_journal(
-            conn,
-            "DECISION_REFUSED",
-            {"portfolio_id": portfolio_id, "forecast_id": forecast_id, "reason": reason},
-            now,
+    portfolio = _portfolio(conn, portfolio_id)
+    forecast = conn.execute(
+        "SELECT cohort_id, condition_id, kind FROM forecasts WHERE forecast_id = ?",
+        (forecast_id,),
+    ).fetchone()
+    if (
+        forecast is None
+        or forecast["kind"] != "entry"
+        or forecast["cohort_id"] != portfolio["cohort_id"]
+    ):
+        raise LedgerError(f"forecast {forecast_id} is not an entry forecast of this cohort")
+    if _decided(conn, portfolio_id, forecast_id):
+        raise LedgerError(f"forecast {forecast_id} is already decided here")
+    if not conn.execute(
+        "SELECT 1 FROM forecast_baselines WHERE forecast_id = ?", (forecast_id,)
+    ).fetchone():
+        raise LedgerError(
+            f"forecast {forecast_id} has no baseline yet; decisions follow the baseline"
         )
+    conn.execute(
+        "INSERT INTO decisions (portfolio_id, forecast_id, condition_id, kind, reason, "
+        "ticket_id, decided_at) VALUES (?, ?, ?, 'REFUSED', ?, NULL, ?)",
+        (portfolio_id, forecast_id, forecast["condition_id"], reason, isoformat(now)),
+    )
+    append_journal(
+        conn,
+        "DECISION_REFUSED",
+        {
+            "portfolio_id": portfolio_id,
+            "forecast_id": forecast_id,
+            "reason": reason,
+            "detail": detail,
+        },
+        now,
+    )
+
+
+def record_refusal_decision(
+    conn: sqlite3.Connection,
+    portfolio_id: str,
+    forecast_id: int,
+    reason: str,
+    now: datetime,
+    detail: str = "",
+) -> None:
+    with transaction(conn):
+        record_refusal_locked(conn, portfolio_id, forecast_id, reason, now, detail)


 def open_cost(conn: sqlite3.Connection, portfolio_id: str) -> Decimal:
```

`src/predict_agent/paper.py` (create):

```python
"""Paper trading (spec §3 stages 4–5): for each entry forecast with a timely baseline,
decide in every undecided portfolio of its cohort and record a ticket or a refusal.

Each portfolio's decision runs in one transaction: the policy inputs (forecast, books,
cash, exposure, eligibility) are read inside the same lock that writes the result, and
the policy is the portfolio's frozen artifact, never the config file."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .artifacts import load_artifact
from .cash import LedgerError, available_cash, parse_money
from .config import ConfigError
from .db import transaction
from .fills import AskLevel
from .forecasts import ResumeStep, resume_step, undecided_portfolios, unfinished_forecasts
from .policy import Exposure, ForecastView, PolicyInputs, SideBook, Trade, decide
from .policy_params import PolicyParams, policy_from_artifact
from .tickets import TicketDraft, equity, open_ticket_locked, record_refusal_locked
from .util import parse_datetime

COHORT_CLOSED = "COHORT_CLOSED"


@dataclass(frozen=True)
class DecisionResult:
    portfolio_id: str
    forecast_id: int
    ticket_id: int | None
    reason: str | None  # refusal code, None when traded
    detail: str


@dataclass(frozen=True)
class TradeSummary:
    traded: int
    refused: dict[str, int]
    waiting: int  # entry forecasts not ready for a decision (no baseline yet)


def _optional_decimal(text: str | None) -> Decimal | None:
    return None if text is None else parse_money(text)


def latest_discovery_run(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM runs WHERE command IN ('discover', 'run-data') "
        "AND status = 'COMPLETED' ORDER BY started_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return None if row is None else str(row["run_id"])


def _side_book(conn: sqlite3.Connection, snapshot_id: int, outcome: str) -> SideBook:
    row = conn.execute("SELECT * FROM book_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if row is None:
        raise LedgerError(f"baseline snapshot {snapshot_id} is missing")
    record = json.loads(row["record_json"])
    schedule = row["fee_schedule_json"]
    return SideBook(
        outcome=outcome,
        snapshot_id=snapshot_id,
        fetched_at=parse_datetime(row["fetched_at"]),
        asks=tuple(AskLevel(Decimal(price), Decimal(size)) for price, size in record["asks"]),
        tick_size=Decimal(record["tick_size"]),
        min_order_size=Decimal(record["min_order_size"]),
        fees_enabled=bool(row["fees_enabled"]),
        fee_schedule=None if schedule is None else json.loads(schedule),
    )


def _exposure(
    conn: sqlite3.Connection, portfolio_id: str, condition_id: str, event_id: str, category: str
) -> Exposure:
    totals = {"market": Decimal("0"), "event": Decimal("0"), "category": Decimal("0")}
    total = Decimal("0")
    for row in conn.execute(
        "SELECT t.cost_total, t.condition_id, m.event_id, m.category FROM paper_tickets t "
        "JOIN markets m ON m.condition_id = t.condition_id "
        "WHERE t.portfolio_id = ? AND t.status = 'OPEN'",
        (portfolio_id,),
    ):
        cost = parse_money(row["cost_total"])
        total += cost
        if row["condition_id"] == condition_id:
            totals["market"] += cost
        if row["event_id"] == event_id:
            totals["event"] += cost
        if row["category"] == category:
            totals["category"] += cost
    return Exposure(totals["market"], totals["event"], totals["category"], total)


def load_inputs(
    conn: sqlite3.Connection, portfolio_id: str, forecast_id: int, now: datetime
) -> PolicyInputs:
    forecast = conn.execute(
        "SELECT * FROM forecasts WHERE forecast_id = ?", (forecast_id,)
    ).fetchone()
    if forecast is None:
        raise LedgerError(f"unknown forecast {forecast_id}")
    condition_id = forecast["condition_id"]
    market = conn.execute(
        "SELECT * FROM markets WHERE condition_id = ?", (condition_id,)
    ).fetchone()
    if market is None:
        raise LedgerError(f"forecast {forecast_id} is for an unknown market")
    rules = conn.execute(
        "SELECT rules_json FROM rules_versions WHERE condition_id = ? AND rules_hash = ?",
        (condition_id, forecast["rules_hash"]),
    ).fetchone()
    end_text = json.loads(rules["rules_json"]).get("end_date") if rules else None
    baseline = conn.execute(
        "SELECT yes_snapshot_id, no_snapshot_id FROM forecast_baselines "
        "WHERE forecast_id = ? AND reason IS NULL",
        (forecast_id,),
    ).fetchone()
    books = {}
    if baseline is not None:
        books = {
            "YES": _side_book(conn, baseline["yes_snapshot_id"], "YES"),
            "NO": _side_book(conn, baseline["no_snapshot_id"], "NO"),
        }
    discovery = latest_discovery_run(conn)
    eligible = discovery is not None and conn.execute(
        "SELECT 1 FROM discoveries WHERE run_id = ? AND condition_id = ?",
        (discovery, condition_id),
    ).fetchone() is not None
    started = conn.execute(
        "SELECT 1 FROM resolution_observations WHERE condition_id = ? AND status != 'posed'",
        (condition_id,),
    ).fetchone() is not None
    traded = conn.execute(
        "SELECT 1 FROM paper_tickets WHERE portfolio_id = ? AND condition_id = ?",
        (portfolio_id, condition_id),
    ).fetchone() is not None
    return PolicyInputs(
        forecast=ForecastView(
            abstained=bool(forecast["abstained"]),
            p_low=_optional_decimal(forecast["p_low"]),
            p_mid=_optional_decimal(forecast["p_mid"]),
            p_high=_optional_decimal(forecast["p_high"]),
            confidence=forecast["confidence"],
            rules_hash=forecast["rules_hash"],
            end_date=parse_datetime(end_text) if end_text else None,
        ),
        current_rules_hash=market["current_rules_hash"],
        eligible=eligible,
        resolution_started=started,
        already_traded=traded,
        books=books,
        equity=equity(conn, portfolio_id),
        available_cash=available_cash(conn, portfolio_id),
        exposure=_exposure(
            conn, portfolio_id, condition_id, market["event_id"], market["category"]
        ),
        now=now,
    )


def _policy(conn: sqlite3.Connection, policy_hash: str) -> PolicyParams:
    kind, content = load_artifact(conn, policy_hash)
    if kind != "policy":
        raise LedgerError(f"artifact {policy_hash[:12]} is not a policy")
    try:
        return policy_from_artifact(content)
    except ConfigError as error:
        raise LedgerError(f"policy artifact {policy_hash[:12]} is unreadable: {error}") from None


def decide_portfolio(
    conn: sqlite3.Connection, portfolio_id: str, forecast_id: int, now: datetime
) -> DecisionResult:
    with transaction(conn):
        portfolio = conn.execute(
            "SELECT p.policy_hash, c.status FROM portfolios p "
            "JOIN cohorts c ON c.cohort_id = p.cohort_id WHERE p.portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()
        if portfolio is None:
            raise LedgerError(f"unknown portfolio {portfolio_id[:12]}")
        if portfolio["status"] != "ACTIVE":
            detail = "cohort closed before this forecast was decided"
            record_refusal_locked(conn, portfolio_id, forecast_id, COHORT_CLOSED, now, detail)
            return DecisionResult(portfolio_id, forecast_id, None, COHORT_CLOSED, detail)
        params = _policy(conn, portfolio["policy_hash"])
        inputs = load_inputs(conn, portfolio_id, forecast_id, now)
        result = decide(inputs, params)
        if not isinstance(result, Trade):
            record_refusal_locked(
                conn, portfolio_id, forecast_id, result.reason, now, result.detail
            )
            return DecisionResult(portfolio_id, forecast_id, None, result.reason, result.detail)
        forecast = conn.execute(
            "SELECT condition_id, rules_hash FROM forecasts WHERE forecast_id = ?",
            (forecast_id,),
        ).fetchone()
        draft = TicketDraft(
            portfolio_id=portfolio_id,
            forecast_id=forecast_id,
            snapshot_id=result.snapshot_id,
            condition_id=forecast["condition_id"],
            outcome=result.outcome,
            fills=result.walk.fills,
            fee=result.walk.fee,
            policy_hash=portfolio["policy_hash"],
            rules_hash=forecast["rules_hash"],
        )
        ticket_id = open_ticket_locked(conn, draft, now)
        detail = f"{result.outcome} edge {result.edge} cost {result.walk.cost}"
        return DecisionResult(portfolio_id, forecast_id, ticket_id, None, detail)


def trade_ready(conn: sqlite3.Connection, now: datetime) -> TradeSummary:
    """Decide every entry forecast whose baseline is attached, in every cohort (a closed
    cohort's forecasts are refused COHORT_CLOSED). Safe to rerun: decided portfolios are
    skipped."""
    traded = 0
    refused: Counter[str] = Counter()
    waiting = 0
    cohorts = [row["cohort_id"] for row in conn.execute("SELECT cohort_id FROM cohorts")]
    for cohort_id in cohorts:
        for forecast_id in unfinished_forecasts(conn, cohort_id):
            if resume_step(conn, forecast_id, now) is not ResumeStep.NEEDS_DECISION:
                waiting += 1
                continue
            for portfolio_id in undecided_portfolios(conn, forecast_id):
                result = decide_portfolio(conn, portfolio_id, forecast_id, now)
                if result.ticket_id is not None:
                    traded += 1
                else:
                    refused[result.reason or ""] += 1
    return TradeSummary(traded, dict(refused), waiting)
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_paper -v` → 11 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`

```bash
git add src/predict_agent/tickets.py src/predict_agent/paper.py tests/predict/ledger_fixtures.py tests/predict/trade_fixtures.py tests/predict/test_paper.py
git commit -m "feat(predict): paper trading decides every portfolio in one transaction"
```

---

### Task 5: `trade` command and fill invariants

**Files:**
- Create: `tests/predict/test_paper_invariants.py`
- Modify: `src/predict_agent/invariants.py`, `src/predict_agent/cli.py`, `tests/predict/test_ledger_invariants.py`, `CLAUDE.md`

**Interfaces:**
- Consumes: `paper.trade_ready` (Task 4); `fills.level_fee` (Task 2); `gamma.fee_rate`; `policy_params.policy_from_artifact`; `artifacts.load_artifact`.
- Produces:
  - `verify_ledger` also reports, per ticket: fills not within its snapshot's recorded asks (unknown level, duplicate level, more than the level's size), a fee that is not the per-level fee from the snapshot's schedule, shares/cost not matching the fills, fills above the policy's limit price, and a decision taken outside `[0, max_book_age_seconds]` after the snapshot was fetched
  - CLI: `predict-agent trade` (offline) prints `traded N; refused M (CODE count, ...); waiting W` and exits 0
  - `tests/predict/test_ledger_invariants.py`: the Plan 2 fixture ticket's fee becomes `0` — its snapshot is fee-free, and the new check correctly flags the old arbitrary `0.02`


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_paper_invariants.py` (create):

```python
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

from predict_agent.cli import main
from predict_agent.cohorts import cohort_portfolios
from predict_agent.db import connect
from predict_agent.invariants import verify_ledger
from predict_agent.tickets import ticket_hash
from predict_agent.util import canonical_json, isoformat
from tests.predict.fixtures import CONDITION_ID, NOW
from tests.predict.trade_fixtures import (
    POLITICS_FEES,
    seed_discovery,
    seed_policy_cohort,
    seed_ready_forecast,
    seed_tradeable_market,
)

REPO = Path(__file__).resolve().parents[2]
DECIDE_AT = NOW + timedelta(seconds=30)


class FillInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        shutil.copy(
            REPO / "config" / "predict-policy.example.json",
            self.root / "config" / "predict-policy.json",
        )
        self.conn = connect(self.root / "data" / "predict.sqlite3")
        rules = seed_tradeable_market(self.conn)
        seed_discovery(self.conn, [CONDITION_ID])
        self.cohort = seed_policy_cohort(self.conn)
        seed_ready_forecast(
            self.conn, self.cohort, rules, fee_schedule=POLITICS_FEES,
            yes_asks=[("0.50", "20"), ("0.51", "100"), ("0.60", "100")],
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(argv, root=self.root, now_fn=lambda: DECIDE_AT)
        return code, out.getvalue()

    def rewrite_ticket(self, **changes: Any) -> None:
        """Change a primary ticket's columns and re-hash it, so only the fill check can
        notice (the hash stays valid)."""
        self.conn.execute("DROP TRIGGER tickets_open_to_settled_only")
        primary = cohort_portfolios(self.conn, self.cohort)["primary"]
        row = dict(
            self.conn.execute(
                "SELECT * FROM paper_tickets WHERE portfolio_id = ?", (primary,)
            ).fetchone()
        )
        row.update(changes)
        assignments = ", ".join(f"{column} = ?" for column in changes)
        self.conn.execute(
            f"UPDATE paper_tickets SET {assignments}, ticket_hash = ? WHERE ticket_id = ?",
            (*changes.values(), ticket_hash(row), row["ticket_id"]),
        )

    def assert_problem(self, fragment: str) -> None:
        problems = verify_ledger(self.conn)
        self.assertTrue(any(fragment in p for p in problems), problems)
        self.assertFalse(any("ticket hash" in p for p in problems), problems)

    def test_trade_command_trades_and_doctor_stays_clean(self) -> None:
        code, output = self.run_cli(["trade"])
        self.assertEqual(code, 0, output)
        self.assertIn("traded 2; refused 0; waiting 0", output)
        self.assertEqual(verify_ledger(self.conn), [])
        code, output = self.run_cli(["doctor"])
        self.assertEqual(code, 0, output)

    def test_fills_beyond_recorded_depth_are_detected(self) -> None:
        self.run_cli(["trade"])
        self.rewrite_ticket(fills_json=canonical_json([["0.50", "25"]]))
        self.assert_problem("fills are not within the recorded book")

    def test_fee_not_from_the_snapshot_schedule_is_detected(self) -> None:
        self.run_cli(["trade"])
        self.rewrite_ticket(fee="0")
        self.assert_problem("fee differs from the snapshot's per-level fee")

    def test_fills_past_the_slippage_limit_are_detected(self) -> None:
        self.run_cli(["trade"])
        self.rewrite_ticket(fills_json=canonical_json([["0.60", "10"]]))
        self.assert_problem("slippage limit")

    def test_decision_on_a_stale_book_is_detected(self) -> None:
        self.run_cli(["trade"])
        self.rewrite_ticket(created_at=isoformat(NOW + timedelta(minutes=5)))
        self.assert_problem("decided on a book")

    def test_trade_command_reports_refusals_by_reason(self) -> None:
        seed_discovery(self.conn, [], at=NOW + timedelta(seconds=5))
        code, output = self.run_cli(["trade"])
        self.assertEqual(code, 0, output)
        self.assertIn("refused 2 (ELIGIBILITY_LOST 2)", output)
        decision = self.conn.execute("SELECT reason FROM decisions LIMIT 1").fetchone()[0]
        self.assertEqual(decision, "ELIGIBILITY_LOST")
        self.assertTrue(json.loads(self.conn.execute(
            "SELECT payload_json FROM journal WHERE kind = 'DECISION_REFUSED' LIMIT 1"
        ).fetchone()[0])["detail"])
        tickets = self.conn.execute("SELECT COUNT(*) FROM paper_tickets").fetchone()[0]
        self.assertEqual(tickets, 0)


if __name__ == "__main__":
    unittest.main()
```

Apply to `tests/predict/test_ledger_invariants.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/tests/predict/test_ledger_invariants.py b/tests/predict/test_ledger_invariants.py
index 95c4b6b..0f33a7e 100644
--- a/tests/predict/test_ledger_invariants.py
+++ b/tests/predict/test_ledger_invariants.py
@@ -57,7 +57,7 @@ class InvariantTests(unittest.TestCase):
                 self.forecast,
                 yes,
                 fills=(Fill(Decimal("0.40"), Decimal("10")),),
-                fee=Decimal("0.02"),
+                fee=Decimal("0"),  # the snapshot is fee-free
             ),
             LATER,
         )
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_paper_invariants -v`
Expected: ERROR/FAIL — `trade` is not yet a command (argparse exits with status 2) and the re-hashed tickets report no fill problems.

- [ ] **Step 3: Implement**

Apply to `src/predict_agent/invariants.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/src/predict_agent/invariants.py b/src/predict_agent/invariants.py
index e609fc8..2fa463d 100644
--- a/src/predict_agent/invariants.py
+++ b/src/predict_agent/invariants.py
@@ -12,11 +12,15 @@ from collections.abc import Callable
 from datetime import timedelta
 from decimal import Decimal

+from .artifacts import load_artifact
 from .cash import available_cash, cash_entry_hash, parse_money
 from .cohorts import portfolio_id_for
 from .db import GENESIS_HASH, SCHEMA
+from .fills import level_fee
 from .forecasts import forecast_hash
+from .gamma import fee_rate
 from .ledger_schema import LEDGER_SCHEMA
+from .policy_params import policy_from_artifact
 from .settlement import SETTLEABLE_OUTCOMES, payout_per_share
 from .tickets import ticket_hash
 from .util import parse_datetime, sha256_json, sha256_text
@@ -262,6 +266,56 @@ def _settlement_problems(
     return problems


+def _fill_problems(conn: sqlite3.Connection, ticket: sqlite3.Row) -> list[str]:
+    """A ticket's fills must come from its own recorded book: existing levels, no more than
+    their recorded size, per-level fees from the snapshot's schedule, within the policy's
+    slippage limit, decided while the book was fresh (spec §5: never fabricate fills)."""
+    label = f"ticket {ticket['ticket_id']}"
+    snapshot = conn.execute(
+        "SELECT * FROM book_snapshots WHERE id = ?", (ticket["snapshot_id"],)
+    ).fetchone()
+    if snapshot is None:
+        return [f"{label}: its snapshot is missing"]
+    record = json.loads(snapshot["record_json"])
+    depth = {Decimal(price): Decimal(size) for price, size in record["asks"]}
+    fills = [
+        (Decimal(price), Decimal(shares)) for price, shares in json.loads(ticket["fills_json"])
+    ]
+    problems: list[str] = []
+    prices = [price for price, _ in fills]
+    if (
+        not fills
+        or len(set(prices)) != len(prices)
+        or any(price not in depth or shares > depth[price] for price, shares in fills)
+    ):
+        problems.append(f"{label}: fills are not within the recorded book")
+    rate = Decimal("0")
+    if snapshot["fees_enabled"]:
+        schedule = snapshot["fee_schedule_json"]
+        found = fee_rate(json.loads(schedule) if schedule else None)
+        if found is None:
+            return [*problems, f"{label}: snapshot has fees but no fee rate"]
+        rate = found
+    fee = sum((level_fee(shares, price, rate) for price, shares in fills), Decimal("0"))
+    notional = sum((price * shares for price, shares in fills), Decimal("0"))
+    if parse_money(ticket["fee"]) != fee:
+        problems.append(f"{label}: fee differs from the snapshot's per-level fee")
+    if (
+        parse_money(ticket["cost_total"]) != notional + parse_money(ticket["fee"])
+        or parse_money(ticket["shares"]) != sum((s for _, s in fills), Decimal("0"))
+    ):
+        problems.append(f"{label}: shares or cost do not match its fills")
+    params = policy_from_artifact(load_artifact(conn, ticket["policy_hash"])[1])
+    if depth and fills:
+        limit = min(depth) * (Decimal("1") + params.max_slippage)
+        if max(prices) > limit:
+            problems.append(f"{label}: fills beyond the policy's slippage limit")
+    age = parse_datetime(ticket["created_at"]) - parse_datetime(snapshot["fetched_at"])
+    if not timedelta(0) <= age <= timedelta(seconds=params.max_book_age_seconds):
+        problems.append(f"{label}: decided on a book {age} old")
+    return problems
+
+
 def _decision_problems(conn: sqlite3.Connection) -> list[str]:
     problems: list[str] = []
     for row in conn.execute(
@@ -348,6 +402,13 @@ def _verify(conn: sqlite3.Connection) -> list[str]:
         "ticket_id",
         _ticket_problems,
     )
+    problems += _each(
+        conn,
+        "SELECT * FROM paper_tickets ORDER BY ticket_id",
+        "ticket",
+        "ticket_id",
+        _fill_problems,
+    )
     problems += _guarded("decisions", lambda: _decision_problems(conn))
     return problems

```

Apply to `src/predict_agent/cli.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/src/predict_agent/cli.py b/src/predict_agent/cli.py
index c284688..f6869b6 100644
--- a/src/predict_agent/cli.py
+++ b/src/predict_agent/cli.py
@@ -21,6 +21,7 @@ from .db import connect, record_refusal, verify_journal
 from .gamma import ParseError
 from .http import FetchError, JsonClient
 from .invariants import verify_ledger
+from .paper import trade_ready
 from .policy_params import load_policy_config
 from .report import render_markdown, shortlist
 from .settlement import settle_open_tickets
@@ -37,6 +38,7 @@ def _parser() -> argparse.ArgumentParser:
     sub.add_parser("snapshot", help="snapshot books for the latest discovery run")
     sub.add_parser("resolve", help="poll resolution state for known markets")
     sub.add_parser("settle", help="settle open paper tickets from stored resolutions (offline)")
+    sub.add_parser("trade", help="decide forecasts with timely baselines; paper only (offline)")
     report = sub.add_parser("report", help="write the shortlist report")
     which = report.add_mutually_exclusive_group(required=True)
     which.add_argument("--run")
@@ -103,6 +105,20 @@ def main(
             print(f"ledger: {problem}")
         print(f"ledger: {'ok' if not problems else f'{len(problems)} problem(s)'}")
         return 0 if journal_ok and not problems else 3
+    if args.command == "trade":
+        conn = connect(settings.database_path)
+        try:
+            trades = trade_ready(conn, now_fn())
+        finally:
+            conn.close()
+        reasons = ", ".join(f"{code} {count}" for code, count in sorted(trades.refused.items()))
+        refused = sum(trades.refused.values())
+        print(
+            f"traded {trades.traded}; refused {refused}"
+            + (f" ({reasons})" if reasons else "")
+            + f"; waiting {trades.waiting}"
+        )
+        return 0
     if args.command == "settle":
         now = now_fn()
         conn = connect(settings.database_path)
```

Apply to `CLAUDE.md` (`git apply` accepts this hunk as written):

```diff
diff --git a/CLAUDE.md b/CLAUDE.md
index 954223f..781ea33 100644
--- a/CLAUDE.md
+++ b/CLAUDE.md
@@ -33,6 +33,7 @@ Paper-first, human-approved investing agent. Flow: ingest (fail-closed snapshots
 - Spec: `docs/superpowers/specs/2026-10-05-polymarket-paper-forecaster-design.md`; plans in `docs/superpowers/plans/`.
 - Separate package: `predict_agent` must never import `japan_agent` (enforced by `tests/predict/test_cli_isolation.py`). `japan_agent` is frozen.
 - Phase 1 has **no execution code**: no wallet, keys, signing, or non-GET HTTP. Never attempt to circumvent Polymarket's UK geoblock; the geoblock check is audit-only.
-- CLI: `PYTHONPATH=src python3 -m predict_agent.cli doctor|discover|snapshot|resolve|settle|report|run-data`. Needs `config/predict-policy.json` (human-owned copy of the `.example`; code never writes it).
+- CLI: `PYTHONPATH=src python3 -m predict_agent.cli doctor|discover|snapshot|resolve|trade|settle|report|run-data`. Needs `config/predict-policy.json` (human-owned copy of the `.example`; code never writes it).
 - Live API quirks pinned in `tests/predict/fixtures.py`: JSON-string list fields, worst-first books on both sides, resolution rows with or without `payouts` (micro-USDC when present), Gamma `/events` offset paging is capped (use `/events/keyset` + `after_cursor`), and the CLOB book `timestamp` behaved as a last-change time in live observation (not documented): store both server and fetch times and never refuse a book only because its timestamp is old. Gamma `/markets` needs repeated `condition_ids` params (comma-joined matches nothing) and returns closed markets only with `closed=true`.
 - Ledger (Plan 2): a cohort is one research identity (prompt, model, research settings, scoring version, baseline window, generation) owning the shared forecasts; each cohort has portfolios (`primary` plus pre-registered shadows like `shadow_mid`), each with its own frozen policy, FUNDING, cash, tickets and decisions. Money is Decimal text summed in Python (never SQL `SUM`); compare stored timestamps parsed, not as strings. Every cash entry must be backed (FUNDING = bankroll, DEBIT = its OPEN ticket's cost, CREDIT = its settlement payout). Write ledger state only through `cohorts`/`forecasts`/`tickets`/`settlement` functions — each commits with its journal entry. Settlement picks the governing resolution by observation time inside its transaction and waits on overlapping contradictory polls. `predict-agent settle` is offline and lists pending tickets with reason and age; `doctor` also runs `verify_ledger` (hashes, triggers and accounting relationships).
+- Paper policy (Plan 3): `policy.decide` is pure (no I/O, no clock). Decisions read the portfolio's frozen policy artifact (`policy_params.variant_policies`: `primary` = conservative bounds, `shadow_mid` = p_mid), never `config/predict-policy.json` directly; `doctor` still requires the config's `policy` section. Fills walk only the recorded book (per-level fees, shares rounded down to 0.01, limit price = best ask x (1 + max_slippage)); `paper.decide_portfolio` reads its inputs and writes the ticket/refusal in one transaction; `doctor` re-checks every ticket's fills against its snapshot and policy. `predict-agent trade` is offline.
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_paper_invariants -v` → 6 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`

```bash
git add src/predict_agent/invariants.py src/predict_agent/cli.py tests/predict/test_paper_invariants.py tests/predict/test_ledger_invariants.py CLAUDE.md
git commit -m "feat(predict): trade command; doctor re-checks fills against the recorded book"
```

---

### Task 6: Plan 2 carry-forward hardening

**Files:**
- Modify: `src/predict_agent/db.py`, `tests/predict/test_ledger_settlement.py`, `tests/predict/test_ledger_invariants.py`

**Interfaces:**
- Consumes: Plan 2 settlement and invariants.
- Produces:
  - `db.verify_journal(conn) -> bool` returns `False` for an unreadable entry (e.g. `payload_json` that is not JSON) instead of raising, so `doctor` prints `journal chain BROKEN` and exits 3
  - Settlement regression tests for the Plan 2 test gaps: HALF on the YES side, a resolved YES that Gamma contradicts, a legacy (pre-v3, no request time) row governing and ordering as a point, a legacy contradiction inside a newer poll's interval
- The settlement tests pin existing behavior and pass before the `db.py` change; the journal test is the one that fails first.


- [ ] **Step 1: Write the failing tests**

Apply to `tests/predict/test_ledger_settlement.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/tests/predict/test_ledger_settlement.py b/tests/predict/test_ledger_settlement.py
index e10cd10..60fa192 100644
--- a/tests/predict/test_ledger_settlement.py
+++ b/tests/predict/test_ledger_settlement.py
@@ -16,7 +16,8 @@ from predict_agent.db import connect
 from predict_agent.forecasts import ForecastError
 from predict_agent.settlement import PendingReason, payout_per_share, settle_open_tickets
 from predict_agent.tickets import open_ticket
-from tests.predict.fixtures import NOW
+from predict_agent.util import isoformat
+from tests.predict.fixtures import CONDITION_ID, NOW
 from tests.predict.ledger_fixtures import (
     portfolio,
     seed_baselined_forecast,
@@ -30,6 +31,7 @@ from tests.predict.ledger_fixtures import (
 LATER = NOW + timedelta(seconds=1)
 RESOLVED_AT = NOW + timedelta(days=20)
 SETTLE_AT = NOW + timedelta(days=30)
+D_HALF = Decimal("0.5")


 class SettlementTestCase(unittest.TestCase):
@@ -115,6 +117,44 @@ class SettleTests(SettlementTestCase):
         self.assertEqual(Decimal(self.settlement(ticket)["net_pnl"]), Decimal("1"))
         self.assertEqual(available_cash(self.conn, self.primary), Decimal("1001"))

+    def test_half_on_the_yes_side_pays_half(self) -> None:
+        ticket = self.open_test_ticket("YES")
+        self.observe("HALF")
+        settle_open_tickets(self.conn, SETTLE_AT)
+        row = self.settlement(ticket)
+        self.assertEqual((row["outcome"], Decimal(row["payout_per_share"])), ("HALF", D_HALF))
+        self.assertEqual(Decimal(row["net_pnl"]), Decimal("1"))
+
+    def test_resolved_yes_that_gamma_contradicts_never_settles(self) -> None:
+        self.open_test_ticket("YES")
+        self.observe("YES", cross_check="MISMATCH")
+        summary = settle_open_tickets(self.conn, SETTLE_AT)
+        self.assertEqual((summary.settled, summary.count(PendingReason.UNSETTLEABLE)), (0, 1))
+
+    def legacy_observation(self, outcome: str, cross_check: str, minutes: float) -> None:
+        """A pre-v3 row: no resolution_requested_at, so its evidence is a point in time."""
+        at = isoformat(RESOLVED_AT + timedelta(minutes=minutes))
+        self.conn.execute(
+            "INSERT INTO resolution_observations (run_id, condition_id, "
+            "resolution_fetched_at, gamma_fetched_at, gamma_json, status, outcome, "
+            "cross_check, was_disputed, new_version_q, raw_json) "
+            "VALUES ('r', ?, ?, ?, '{}', 'resolved', ?, ?, 0, 0, '{}')",
+            (CONDITION_ID, at, at, outcome, cross_check),
+        )
+
+    def test_legacy_rows_govern_and_order_as_points(self) -> None:
+        self.open_test_ticket("YES")
+        self.legacy_observation("UNKNOWN", "MISMATCH", minutes=5)
+        self.legacy_observation("YES", "CONFIRMED", minutes=6)  # later point supersedes
+        self.assertEqual(settle_open_tickets(self.conn, SETTLE_AT).settled, 1)
+
+    def test_legacy_contradiction_inside_a_newer_poll_interval_waits(self) -> None:
+        self.open_test_ticket("YES")
+        self.observe("YES", minutes=7, requested_at=RESOLVED_AT + timedelta(minutes=6))
+        self.legacy_observation("UNKNOWN", "MISMATCH", minutes=6.5)
+        summary = settle_open_tickets(self.conn, SETTLE_AT)
+        self.assertEqual(summary.count(PendingReason.AMBIGUOUS_EVIDENCE), 1)
+
     def test_waiting_tickets_report_reason_and_age(self) -> None:
         cases = (
             (dict(outcome=None, status="posed", cross_check="NOT_APPLICABLE"),
```

Apply to `tests/predict/test_ledger_invariants.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/tests/predict/test_ledger_invariants.py b/tests/predict/test_ledger_invariants.py
index 0f33a7e..f064c35 100644
--- a/tests/predict/test_ledger_invariants.py
+++ b/tests/predict/test_ledger_invariants.py
@@ -268,6 +268,13 @@ class InvariantTests(unittest.TestCase):
         self.assertIn("settled 0; pending 2", output)
         self.assertIn("AWAITING_CONFIRMATION (resolved 10 days, 0:00:00 ago)", output)

+    def test_doctor_reports_an_unreadable_journal_as_broken(self) -> None:
+        self.conn.execute("DROP TRIGGER journal_no_update")
+        self.conn.execute("UPDATE journal SET payload_json = 'not json' WHERE seq = 1")
+        code, output = self.run_cli(["doctor"])
+        self.assertEqual(code, 3, output)
+        self.assertIn("journal chain BROKEN", output)
+
     def test_doctor_fails_on_ledger_problem(self) -> None:
         code, output = self.run_cli(["doctor"])
         self.assertEqual(code, 0, output)
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_settlement tests.predict.test_ledger_invariants -v`
Expected: ERROR in `test_doctor_reports_an_unreadable_journal_as_broken` — `json.decoder.JSONDecodeError` escapes `verify_journal`; the new settlement tests already pass.

- [ ] **Step 3: Implement**

Apply to `src/predict_agent/db.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/src/predict_agent/db.py b/src/predict_agent/db.py
index ca34784..83f8de3 100644
--- a/src/predict_agent/db.py
+++ b/src/predict_agent/db.py
@@ -234,11 +234,16 @@ def append_journal(


 def verify_journal(conn: sqlite3.Connection) -> bool:
+    """True when the chain is intact. An unreadable entry (e.g. payload that is not JSON)
+    is a broken chain, never an exception."""
     prev_hash = GENESIS_HASH
     for row in conn.execute("SELECT * FROM journal ORDER BY seq"):
         if row["prev_hash"] != prev_hash:
             return False
-        expected = _entry_hash(row["at"], row["kind"], row["payload_json"], row["prev_hash"])
+        try:
+            expected = _entry_hash(row["at"], row["kind"], row["payload_json"], row["prev_hash"])
+        except (ValueError, TypeError):
+            return False
         if expected != row["entry_hash"]:
             return False
         prev_hash = row["entry_hash"]
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_ledger_settlement tests.predict.test_ledger_invariants -v` → 42 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK.

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`

```bash
git add src/predict_agent/db.py tests/predict/test_ledger_settlement.py tests/predict/test_ledger_invariants.py
git commit -m "fix(predict): unreadable journal entries are a broken chain; settlement edge-case tests"
```

---

## After this plan (human)

`doctor` now requires the `policy` section (spec deviation 7). Copy it from `config/predict-policy.example.json` into your own `config/predict-policy.json` and review every value; code never writes that file.

## Self-Review Record

1. **Spec coverage (§5, build-order item 3):** refusal gates for abstained / rules changed / eligibility lost / already held / fee unknown (Task 3), conservative bounds and p_mid shadow (Tasks 1, 3, 4), size-before-depth budget with quarter Kelly and every cap as % of cost-basis equity (Task 3), per-level fee walk with tick and share precision (Task 2), realized-edge recheck, minimum order, slippage and depth (Tasks 2–3, deviations 1–4), one side per market (Task 3), ticket + debit written atomically through the ledger (Task 4), shadow with its own cash (Task 4), book freshness from `fetched_at` (Task 3, Plan 1 contract fact 4), §8 policy and book-walk tests (Tasks 2–4). Deferred by design: research and cohort creation (Plan 4 builds `CohortIdentity` from `load_policy_config` + `variant_policies`), reporting of edge at entry and shadow results (Plan 5 — recomputable from the forecast, ticket and policy artifact).
2. **Placeholder scan:** none.
3. **Type consistency:** `PolicyParams`, `SideBook`, `ForecastView`, `Exposure`, `PolicyInputs`, `Trade`, `Refusal`, `Walk`, `AskLevel`, `DecisionResult`, `TradeSummary` names match across tasks and fixtures; `Fill` is Plan 2's `tickets.Fill`.
4. **Task order:** each task's tests import only modules from earlier tasks and Plan 1–2; verified by committing the tasks in order and running the full suite after each.
5. **Review Focus:** six items above, each pinned to a named test in its owning task.

## Post-implementation amendments (2026-10-06)

- (a) Ruling 5 amended: "resolution started" means any observation whose status is not in `resolution.OPEN_STATUSES` (Plan 1's open set `initialized`/`posed`/`active`), not "no longer posed". Regression tests: `test_open_statuses_initialized_and_active_do_not_refuse`, `test_rules_changed_since_the_forecast_refuses`.
- (b) Ruling 9 clarified: when both sides refuse, the reported code is the first refusal that is not `NO_EDGE`, YES checked first; both sides' reasons are in the detail.
- (c) The fill invariant also rejects zero- or negative-share fills.
- (d) The RESOLUTION_STARTED detail text is "a resolution observation is no longer open".

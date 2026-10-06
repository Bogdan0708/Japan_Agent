# predict-agent Plan 4 — Research Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Task 6 is a human-approved live run: an agent never executes it without the human's explicit go-ahead in the session.

**Goal:** Let Claude forecast eligible markets from their rules and the open web — never the market price — through a locked-down Claude Agent SDK session, record every paid attempt and its cost, flag detected price exposure, and turn each forecast into an immediate post-forecast baseline and paper decision.

**Architecture:** A new package `predict_agent/research/` holds the research layer: `config.py` (the `research` config section), `prompt.py` (frozen prompt artifact, price-free rendering), `schema.py` (structured output + validation, citations must be fetched URLs), `exposure.py` (best-effort scanner) and `sdk.py` — the only module that touches the Claude Agent SDK, loaded lazily through `importlib`. The package cannot import policy, paper, money or market-data modules (isolation test). `budget.py` enforces the daily/per-forecast USD caps and the entry-forecast volume cap from durable `research_attempts`. `research_run.py` is the pipeline behind `predict-agent research`: open the cohort, recover interrupted attempts, resume baselines, then per market research → forecast → books fetched immediately → baseline → `trade_ready`. The research call is injected, so every test runs with no network and no SDK.

**Tech Stack:** Python ≥ 3.11 stdlib core; optional `claude-agent-sdk>=0.2.163` (new `predict` extra, lazily imported); `unittest`, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-10-05-polymarket-paper-forecaster-design.md` (§3 data flow and "Forecasting without the price", §6 research layer, §7 resume, §8 research tests, §10 build order item 4).

**Builds on:** Plans 1–3. Branch from `feat/predict-policy-paper-fills` at `9218529` (PR #3, under audit); rebase onto `main` once PR #3 merges. Carry-forward items from `plan2-carry-forward.md` resolved here: `trade` now runs immediately after the baseline; failed-attempt errors are fixed codes (no raw payloads in the journal); interrupted attempts have a recovery policy.

**Verified facts this plan relies on (2026-10-06):**
- Installed `claude-agent-sdk` 0.2.163 (bundled CLI 2.1.286): `ClaudeAgentOptions` has `tools` (the available built-in set), `allowed_tools`, `disallowed_tools`, `system_prompt`, `mcp_servers`, `strict_mcp_config`, `setting_sources` (`[]` = load no settings files), `skills`, `permission_mode` (`"dontAsk"` = deny if not pre-approved), `max_turns`, `max_budget_usd` (float), `model`, `output_format` (`{"type": "json_schema", "schema": ...}`), `cwd`, `verbatim_prompts`, `hooks`. `HookMatcher(matcher, hooks)`; hook callbacks are `async (input, tool_use_id, context) -> dict`. `PreToolUseHookInput` has `tool_name`, `tool_input`; a PreToolUse hook returns `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"|"deny", "permissionDecisionReason": ..., "updatedInput": ...}}`. `PostToolUseHookInput` adds `tool_response`. `ResultMessage` has `subtype` (`success`, `error_max_turns`, `error_max_budget_usd`, `error_max_structured_output_retries`, `error_during_execution`), `is_error`, `total_cost_usd`, `structured_output`. A `SystemMessage` with subtype `init` carries `data["tools"]` and `data["mcp_servers"]`.
- The bundled CLI's `WebSearch` tool input has `query`, `allowed_domains`, `blocked_domains` (model-supplied — hence the hook that forces the list). Structured output is implemented as a `StructuredOutput` tool.
- The Messages API alternative (server-side `web_search`/`web_fetch`) was considered and rejected by the human: its search results are returned encrypted, so the spec's scan of uncited search snippets would be impossible.

## Global Constraints

- Core code is stdlib-only; the Agent SDK is an optional extra imported only inside `research/sdk.py` via `importlib`, with a clear error when missing. `predict_agent` never imports `japan_agent`. No wallet, key, signing or non-GET HTTP code.
- The research layer never sees a price: the rendered prompt contains only question, rules, resolution source, end date and today; `predict_agent.research` imports nothing from policy, paper, money or market-data modules.
- Research isolation fails closed: available tools exactly `WebSearch` + `WebFetch` (plus the SDK's structured-output tool); no settings files, MCP servers or skills; empty working directory; verbatim prompts; every tool call decided by the PreToolUse hook; the session's init report checked before any tool runs.
- Domain enforcement on both tools: every WebSearch carries `research.blocked_domains`; WebFetch is denied for blocked hosts (and subdomains) and non-http(s) URLs.
- Citations must be URLs actually fetched (WebFetch completed) in that session. Exposure wording is "no detected price exposure", never "blind".
- Every paid research call is a `research_attempts` row opened before the call and closed SUCCEEDED or FAILED with its cost; an unknown or interrupted cost is charged at `per_forecast_usd`. Budgets: daily and per-forecast USD caps counting failed attempts; at most `max_entry_forecasts_per_day` new entry forecasts per day.
- The forecast is committed before any post-forecast book is fetched (Plan 2 `attach_baseline` enforces it); books are fetched immediately after and the decision follows within `policy.max_book_age_seconds`.
- Money is `Decimal` (the SDK's `max_budget_usd` float is an operational cap, never ledger money); timestamps UTC and compared parsed.
- Never call the real SDK or the network in tests. Canonical test run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -v`; ruff line length 100; mypy strict on `src/predict_agent`; code stays 3.11-compatible.

## Spec deviations and rulings in this plan

1. **Search-domain blocking is enforced by our PreToolUse hook**, which overwrites the model-supplied `blocked_domains` and drops any `allowed_domains` on every WebSearch call. The Agent SDK has no per-tool domain option. Cost if wrong: if the CLI ignored `updatedInput`, searches would run unfiltered — Task 6 checks this live.
2. **`EXPECTED_SESSION_TOOLS` = WebSearch, WebFetch, StructuredOutput**, read from the bundled CLI and pinned by Task 6. A session reporting any other tool or any MCP server is aborted before a tool runs (`TOOLSET_MISMATCH`).
3. **Weekly update forecasts are deferred to Plan 5** (with `run-daily`); Plan 4 makes entry forecasts only. The closed-cohort update question from `plan2-carry-forward.md` moves with them.
4. **The daily budget is global** across cohorts (it is real money); an attempt still STARTED counts at the full cap; an unknown cost is charged at the cap; attempts left STARTED by a crash are closed `INTERRUPTED` at the cap when the next run starts. Once BUDGET or VOLUME is hit, every remaining candidate is refused with that code (refusals table) and no attempt is opened.
5. **Candidates** are the latest completed discovery run's markets this cohort has not forecast, in condition-id order, skipping markets closing within `policy.min_hours_to_close` before any money is spent.
6. **Exposure-flagged forecasts still trade** (spec §6 says they are reported separately; P&L always counts). Plan 5 reports them separately.
7. **A forecast needs at least one cited evidence item; an abstention needs none.** Stricter than Plan 2's type check; Plan 2's ledger rules still apply on record.
8. **Budgets are not part of the cohort identity**; model, `max_turns`, blocked domains, baseline window, scoring version and generation are (via `ResearchConfig.settings_record()` and `CohortIdentity`).
9. **New `predict` extra** (`claude-agent-sdk>=0.2.163`), separate from the frozen japan_agent `research` extra.
10. **No concurrent-run lock yet**: `recover_interrupted_attempts` assumes it is the only research run. Plan 5's cron wrapper adds the lock (spec §3 flock).
11. **Failure details** (tool names, SDK error type, schema problem — never raw payloads) go to the run's `refusals` rows; the attempt itself stores only the code.

## Review Focus

1. **A widened session** — an extra tool, an MCP server, or a missing research tool in the init report aborts the run before any tool executes (Task 3 `test_extra_tool_or_mcp_server_aborts_before_any_tool_runs`).
2. **Claude reaching for a prediction market** — a fetch of `polymarket.com`/`*.kalshi.com`/`ftp://`/`file://` is denied; every search runs with the blocked list even when the model asked for `allowed_domains: ["polymarket.com"]` (Task 3 `HookTests`).
3. **Price exposure in an uncited search snippet** — flagged on the forecast and kept in the stored transcript (Task 5 `test_exposure_in_an_uncited_snippet_is_flagged_on_the_forecast`).
4. **Spend never under-counted** — failed attempt with unknown cost, a crash mid-attempt, the daily cap and the volume cap (Task 4 tests; Task 5 `FailureTests`, `BudgetTests`, `test_interrupted_attempt_is_recovered_at_the_cap`).
5. **No price reaches Claude** — the sent prompt contains no book price or market statistic, and the research package cannot import price/money modules (Task 5 `test_the_prompt_never_carries_a_price`, `test_research_package_cannot_reach_prices_money_or_trading`).
6. **A crash between forecast and baseline** — the next run takes the baseline inside the window and trades, or marks `NO_TIMELY_BASELINE` after it (Task 5 `ResumeTests`).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/predict_agent/research/__init__.py` | package docstring: isolation contract |
| `src/predict_agent/research/config.py` | parse/validate the `research` config section; `blocked_host` |
| `src/predict_agent/research/prompt.py` | system prompt + user template (frozen artifact); price-free rendering |
| `src/predict_agent/research/schema.py` | structured-output JSON schema; validation; fetched-citation rule |
| `src/predict_agent/research/exposure.py` | venue-name and odds-phrase scanner |
| `src/predict_agent/research/sdk.py` | lazy Agent SDK adapter: options, hooks, self-check, outcome |
| `src/predict_agent/budget.py` | day usage, BUDGET/VOLUME refusal, interrupted-attempt recovery |
| `src/predict_agent/research_run.py` | the research pipeline and cohort identity from config |
| `src/predict_agent/collect.py` (modify) | `snapshot_book` extracted from `snapshot_eligible` |
| `src/predict_agent/cli.py` (modify) | `doctor` validates `research` + reports SDK state; `research` command |
| `config/predict-policy.example.json` (modify) | `research` section |
| `pyproject.toml` (modify) | `predict` extra |
| `tests/predict/fake_sdk.py` | stand-in `claude_agent_sdk` replaying scripted sessions through the real hooks |
| `tests/predict/test_research_*.py`, `test_budget.py`, `test_cli_isolation.py` (modify) | one test module per unit; research isolation |

---

### Task 1: Research config section and the price-free prompt

**Files:**
- Create: `src/predict_agent/research/__init__.py`, `src/predict_agent/research/config.py`, `src/predict_agent/research/prompt.py`, `tests/predict/test_research_config.py`
- Modify: `config/predict-policy.example.json`, `src/predict_agent/cli.py`

**Interfaces:**
- Consumes: `config.ConfigError`; `util.canonical_json`, `util.isoformat`; `cli.main`.
- Produces:
  - `research.config.ResearchConfig` frozen dataclass: `model: str`, `max_turns: int`, `per_forecast_usd: Decimal`, `daily_usd: Decimal`, `max_entry_forecasts_per_day: int`, `blocked_domains: tuple[str, ...]` (sorted, unique), `baseline_window_seconds: int`, `scoring_version: str`, `generation: int`; method `settings_record() -> dict` (`tools`, `max_turns`, `blocked_domains` — no budgets)
  - `research.config.parse_research(raw) -> ResearchConfig` (strict keys; decimals as strings; bare-domain check; per-forecast ≤ daily), `load_research_config(path) -> ResearchConfig`, `blocked_host(host, blocked) -> bool` (domain or subdomain, case-insensitive)
  - `research.prompt.SYSTEM_PROMPT`, `USER_TEMPLATE`, `prompt_artifact() -> str` (canonical JSON of both), `render_user_prompt(*, question, rules_text, resolution_source, end_date, today) -> str`, `research_input(user_prompt) -> str`
  - CLI: `doctor` exits 2 when the `research` section is missing/invalid and prints `research: model <id>`


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_research_config.py` (create):

```python
from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from predict_agent.cli import main
from predict_agent.config import ConfigError
from predict_agent.research.config import blocked_host, load_research_config, parse_research
from predict_agent.research.prompt import (
    SYSTEM_PROMPT,
    prompt_artifact,
    render_user_prompt,
    research_input,
)
from tests.predict.fixtures import NOW

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "config" / "predict-policy.example.json"


def example_section() -> dict[str, Any]:
    section: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))["research"]
    return section


class ParseResearchTests(unittest.TestCase):
    def test_example_config_parses(self) -> None:
        config = parse_research(example_section())
        self.assertEqual(config.model, "claude-opus-5-5")
        self.assertEqual(config.per_forecast_usd, Decimal("3.00"))
        self.assertEqual(config.daily_usd, Decimal("30.00"))
        self.assertEqual(config.max_entry_forecasts_per_day, 15)
        self.assertEqual(config.baseline_window_seconds, 1800)
        self.assertIn("polymarket.com", config.blocked_domains)
        self.assertEqual(list(config.blocked_domains), sorted(config.blocked_domains))

    def test_settings_record_freezes_identity_fields_but_not_budgets(self) -> None:
        record = parse_research(example_section()).settings_record()
        self.assertEqual(record["tools"], ["WebSearch", "WebFetch"])
        self.assertNotIn("daily_usd", record)
        self.assertNotIn("per_forecast_usd", record)
        self.assertIn("blocked_domains", record)

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
            "missing key": broken(model=None),
            "unknown key": broken(temperature="0"),
            "empty model": broken(model=" "),
            "float budget": broken(daily_usd=30.0),
            "zero budget": broken(per_forecast_usd="0"),
            "per forecast above daily": broken(per_forecast_usd="31", daily_usd="30"),
            "bool turns": broken(max_turns=True),
            "empty domains": broken(blocked_domains=[]),
            "domain with scheme": broken(blocked_domains=["https://polymarket.com"]),
            "domain with path": broken(blocked_domains=["polymarket.com/x"]),
            "empty scoring version": broken(scoring_version=""),
            "generation zero": broken(generation=0),
        }
        for label, section in cases.items():
            with self.subTest(label), self.assertRaises(ConfigError):
                parse_research(section)

    def test_blocked_host_covers_subdomains_only(self) -> None:
        blocked = ("polymarket.com",)
        self.assertTrue(blocked_host("polymarket.com", blocked))
        self.assertTrue(blocked_host("gamma-api.Polymarket.com.", blocked))
        self.assertFalse(blocked_host("notpolymarket.com", blocked))
        self.assertFalse(blocked_host("polymarket.com.evil.org", blocked))


class PromptTests(unittest.TestCase):
    def render(self) -> str:
        return render_user_prompt(
            question="Will X happen by June?",
            rules_text="Resolves Yes if X happens.",
            resolution_source="",
            end_date=NOW + timedelta(days=30),
            today=NOW,
        )

    def test_rendered_prompt_contains_only_question_rules_and_dates(self) -> None:
        prompt = self.render()
        self.assertIn("Will X happen by June?", prompt)
        self.assertIn("Resolves Yes if X happens.", prompt)
        self.assertIn("(not stated)", prompt)
        self.assertIn("2026-11-04T12:00:00Z", prompt)
        self.assertIn("Today (UTC): 2026-10-05", prompt)
        for word in ("price", "odds", "volume", "liquidity", "bid", "ask"):
            self.assertNotIn(word, prompt.lower())

    def test_artifacts_are_canonical_and_stable(self) -> None:
        self.assertEqual(prompt_artifact(), prompt_artifact())
        self.assertEqual(json.loads(prompt_artifact())["system"], SYSTEM_PROMPT)
        sent = json.loads(research_input(self.render()))
        self.assertEqual(sent, {"system": SYSTEM_PROMPT, "user": self.render()})


class ConfigFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        self.path = self.root / "config" / "predict-policy.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_section_is_refused_by_loader_and_doctor(self) -> None:
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        del raw["research"]
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "no 'research' section"):
            load_research_config(self.path)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(["doctor"], root=self.root)
        self.assertEqual(code, 2)
        self.assertIn("no 'research' section", err.getvalue())

    def test_doctor_reports_the_research_model(self) -> None:
        shutil.copy(EXAMPLE, self.path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["doctor"], root=self.root)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("research: model claude-opus-5-5", out.getvalue())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_research_config -v`
Expected: ERROR `No module named 'predict_agent.research'`.

- [ ] **Step 3: Implement**

`src/predict_agent/research/__init__.py` (create):

```python
"""Research layer (spec §6): Claude forecasts a market from its rules and the open web,
without the market price. This package never imports the policy, paper-trading or money
modules (enforced by tests/predict/test_cli_isolation.py); only `research.sdk` touches the
optional Claude Agent SDK, and only lazily."""
```

`src/predict_agent/research/config.py` (create):

```python
"""The human-owned `research` section of config/predict-policy.json (spec §6, §7).

Everything here except the budgets is part of the cohort identity: changing the model,
prompt-relevant limits or blocked domains opens a new cohort. Budgets are operational
limits and may change between runs without changing what a forecast means."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..config import ConfigError

_KEYS = frozenset(
    {
        "model",
        "max_turns",
        "per_forecast_usd",
        "daily_usd",
        "max_entry_forecasts_per_day",
        "blocked_domains",
        "baseline_window_seconds",
        "scoring_version",
        "generation",
    }
)
_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


@dataclass(frozen=True)
class ResearchConfig:
    model: str
    max_turns: int
    per_forecast_usd: Decimal
    daily_usd: Decimal
    max_entry_forecasts_per_day: int
    blocked_domains: tuple[str, ...]
    baseline_window_seconds: int
    scoring_version: str
    generation: int

    def settings_record(self) -> dict[str, Any]:
        """The research settings frozen into the cohort identity (no budgets)."""
        return {
            "tools": ["WebSearch", "WebFetch"],
            "max_turns": self.max_turns,
            "blocked_domains": list(self.blocked_domains),
        }


def _positive_int(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"research.{key} must be a positive integer")
    return value


def _usd(raw: Mapping[str, Any], key: str) -> Decimal:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"research.{key} must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ConfigError(f"research.{key} must be a decimal string") from None
    if not amount.is_finite() or amount <= 0:
        raise ConfigError(f"research.{key} must be a positive amount")
    return amount


def parse_research(raw: object) -> ResearchConfig:
    if not isinstance(raw, Mapping):
        raise ConfigError("research must be an object")
    if set(raw) != _KEYS:
        missing = sorted(_KEYS - set(raw))
        unknown = sorted(set(raw) - _KEYS)
        raise ConfigError(f"research: missing keys {missing}, unknown keys {unknown}")
    model = raw["model"]
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("research.model must be a model id")
    scoring_version = raw["scoring_version"]
    if not isinstance(scoring_version, str) or not scoring_version:
        raise ConfigError("research.scoring_version must be a non-empty string")
    domains = raw["blocked_domains"]
    if not isinstance(domains, list) or not domains:
        raise ConfigError("research.blocked_domains must be a non-empty list")
    for domain in domains:
        if not isinstance(domain, str) or not _DOMAIN.match(domain):
            raise ConfigError(f"research.blocked_domains: {domain!r} is not a bare domain")
    per_forecast = _usd(raw, "per_forecast_usd")
    daily = _usd(raw, "daily_usd")
    if per_forecast > daily:
        raise ConfigError("research.per_forecast_usd must not exceed research.daily_usd")
    return ResearchConfig(
        model=model,
        max_turns=_positive_int(raw, "max_turns"),
        per_forecast_usd=per_forecast,
        daily_usd=daily,
        max_entry_forecasts_per_day=_positive_int(raw, "max_entry_forecasts_per_day"),
        blocked_domains=tuple(sorted(set(domains))),
        baseline_window_seconds=_positive_int(raw, "baseline_window_seconds"),
        scoring_version=scoring_version,
        generation=_positive_int(raw, "generation"),
    )


def load_research_config(path: Path) -> ResearchConfig:
    if not path.exists():
        raise ConfigError(
            f"{path} is missing: copy config/predict-policy.example.json to "
            "config/predict-policy.json and review every value"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path} is not valid JSON") from error
    if not isinstance(raw, dict) or "research" not in raw:
        raise ConfigError(
            f"{path} has no 'research' section: copy it from "
            "config/predict-policy.example.json and review every value"
        )
    return parse_research(raw["research"])


def blocked_host(host: str, blocked: tuple[str, ...]) -> bool:
    """True when `host` is a blocked domain or a subdomain of one."""
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in blocked)
```

`src/predict_agent/research/prompt.py` (create):

```python
"""The forecasting prompt (spec §3 "Forecasting without the price", §5 "About the range").

The prompt is frozen as one artifact (system text + user template) and its hash is part of
the cohort identity, so any wording change opens a new cohort. A rendered prompt contains
only the question, the pinned rules version, the end date and today's date: never a
price, order book, volume or any market statistic."""

from __future__ import annotations

from datetime import datetime

from ..util import canonical_json, isoformat

SYSTEM_PROMPT = """You are a careful forecaster. You estimate the probability that a \
question resolves YES under its written rules, using only evidence you find on the open \
web with the WebSearch and WebFetch tools.

Rules for your research:
- Never look up prediction markets, betting exchanges, bookmakers or odds aggregators, \
and never use market prices, odds or crowd forecasts as evidence. Your estimate must be \
your own.
- Read the resolution rules literally. If they are ambiguous enough that a careful reader \
could not say what resolves YES, abstain and say why.
- Every evidence item must cite a URL you actually opened with WebFetch in this session.
- Give a base rate: how often comparable situations resolved YES, from reference-class \
evidence, before considering the specifics.

Report three probabilities as decimal strings between "0.01" and "0.99": p_low and \
p_high are the lowest and highest probability you would still find defensible given the \
evidence, and p_mid is your best estimate, with p_low <= p_mid <= p_high."""

USER_TEMPLATE = """Question: {question}

Resolution rules (verbatim):
{rules_text}

Resolution source: {resolution_source}
Market end date (UTC): {end_date}
Today (UTC): {today}

Research the question, then return your forecast in the required structured format."""


def prompt_artifact() -> str:
    """Canonical content of the prompt artifact frozen into the cohort identity."""
    return canonical_json({"system": SYSTEM_PROMPT, "user_template": USER_TEMPLATE})


def render_user_prompt(
    *,
    question: str,
    rules_text: str,
    resolution_source: str,
    end_date: datetime,
    today: datetime,
) -> str:
    return USER_TEMPLATE.format(
        question=question,
        rules_text=rules_text,
        resolution_source=resolution_source or "(not stated)",
        end_date=isoformat(end_date),
        today=isoformat(today)[:10],
    )


def research_input(user_prompt: str) -> str:
    """Canonical content of the research_input artifact: exactly what was sent."""
    return canonical_json({"system": SYSTEM_PROMPT, "user": user_prompt})
```

Apply to `config/predict-policy.example.json` (`git apply` accepts this hunk as written):

```diff
diff --git a/config/predict-policy.example.json b/config/predict-policy.example.json
index ae89742..2d5df95 100644
--- a/config/predict-policy.example.json
+++ b/config/predict-policy.example.json
@@ -36,5 +36,38 @@
     "max_slippage": "0.02",
     "min_hours_to_close": 48,
     "max_book_age_seconds": 120
+  },
+  "research": {
+    "model": "claude-opus-5-5",
+    "max_turns": 40,
+    "per_forecast_usd": "3.00",
+    "daily_usd": "30.00",
+    "max_entry_forecasts_per_day": 15,
+    "blocked_domains": [
+      "bet365.com",
+      "betfair.co.uk",
+      "betfair.com",
+      "draftkings.com",
+      "electionbettingodds.com",
+      "fanduel.com",
+      "gjopen.com",
+      "goodjudgment.com",
+      "kalshi.com",
+      "kalshidata.com",
+      "ladbrokes.com",
+      "manifold.markets",
+      "metaculus.com",
+      "oddschecker.com",
+      "oddsshark.com",
+      "paddypower.com",
+      "polymarket.com",
+      "polymarketanalytics.com",
+      "predictit.org",
+      "smarkets.com",
+      "williamhill.com"
+    ],
+    "baseline_window_seconds": 1800,
+    "scoring_version": "1",
+    "generation": 1
   }
 }
```

Apply to `src/predict_agent/cli.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/src/predict_agent/cli.py b/src/predict_agent/cli.py
index f6869b6..30de25e 100644
--- a/src/predict_agent/cli.py
+++ b/src/predict_agent/cli.py
@@ -24,6 +24,7 @@ from .invariants import verify_ledger
 from .paper import trade_ready
 from .policy_params import load_policy_config
 from .report import render_markdown, shortlist
+from .research.config import load_research_config
 from .settlement import settle_open_tickets
 from .util import utc_now

@@ -91,9 +92,11 @@ def main(
     if args.command == "doctor":
         try:
             load_policy_config(settings.policy_path)
+            research = load_research_config(settings.policy_path)
         except ConfigError as error:
             print(f"predict-agent: {error}", file=sys.stderr)
             return 2
+        print(f"research: model {research.model}")
         conn = connect(settings.database_path)
         try:
             journal_ok = verify_journal(conn)
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_research_config -v` → 8 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK (the real-SDK name test skips unless the `predict` extra is installed).

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`

```bash
git add src/predict_agent/research/__init__.py src/predict_agent/research/config.py src/predict_agent/research/prompt.py src/predict_agent/cli.py config/predict-policy.example.json tests/predict/test_research_config.py
git commit -m "feat(predict): research config section and price-free forecasting prompt"
```

---

### Task 2: Forecast output schema and exposure scanner

**Files:**
- Create: `src/predict_agent/research/schema.py`, `src/predict_agent/research/exposure.py`, `tests/predict/test_research_output.py`

**Interfaces:**
- Consumes: nothing beyond the stdlib.
- Produces:
  - `research.schema.OUTPUT_SCHEMA` (every property required, `additionalProperties: false`, probabilities as nullable strings), `P_MIN`, `P_MAX`, `CONFIDENCE`
  - `research.schema.OutputError(ValueError)` with `code` (`SCHEMA_INVALID` | `UNFETCHED_CITATION`)
  - `research.schema.ParsedForecast(abstained, abstain_reason, p_low, p_mid, p_high, confidence, base_rate, rules_interpretation, evidence: tuple[(claim, url), ...])`
  - `research.schema.parse_output(raw, fetched_urls: frozenset[str]) -> ParsedForecast`
  - `research.exposure.VENUES`, `research.exposure.scan(texts: Iterable[str]) -> tuple[str, ...]` (sorted unique `venue:<name>` / `phrase:<name>` flags)


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_research_output.py` (create):

```python
from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any

from predict_agent.research.exposure import scan
from predict_agent.research.schema import OUTPUT_SCHEMA, OutputError, parse_output

FETCHED = frozenset({"https://www.reuters.com/a", "https://apnews.com/b"})


def forecast(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "abstain": False,
        "abstain_reason": None,
        "p_low": "0.55",
        "p_mid": "0.60",
        "p_high": "0.70",
        "confidence": "medium",
        "base_rate": "0.30",
        "rules_interpretation": "Resolves on the official count.",
        "evidence": [{"claim": "Polls lead by 5", "url": "https://www.reuters.com/a"}],
    }
    raw.update(changes)
    return raw


class SchemaTests(unittest.TestCase):
    def test_schema_requires_every_property(self) -> None:
        self.assertEqual(set(OUTPUT_SCHEMA["required"]), set(OUTPUT_SCHEMA["properties"]))
        self.assertIs(OUTPUT_SCHEMA["additionalProperties"], False)

    def test_valid_forecast_parses_to_exact_decimals(self) -> None:
        parsed = parse_output(forecast(), FETCHED)
        self.assertEqual((parsed.p_low, parsed.p_mid, parsed.p_high),
                         (Decimal("0.55"), Decimal("0.60"), Decimal("0.70")))
        self.assertEqual(parsed.base_rate, Decimal("0.30"))
        self.assertEqual(parsed.evidence, (("Polls lead by 5", "https://www.reuters.com/a"),))

    def test_abstention_parses_without_probabilities(self) -> None:
        parsed = parse_output(forecast(abstain=True, abstain_reason="rules ambiguous",
                                       p_low=None, p_mid=None, p_high=None, confidence=None,
                                       base_rate=None, evidence=[]), FETCHED)
        self.assertTrue(parsed.abstained)
        self.assertIsNone(parsed.p_mid)

    def test_citation_must_be_a_url_fetched_this_session(self) -> None:
        with self.assertRaises(OutputError) as caught:
            parse_output(forecast(evidence=[{"claim": "c", "url": "https://other.org/x"}]),
                         FETCHED)
        self.assertEqual(caught.exception.code, "UNFETCHED_CITATION")

    def test_invalid_outputs_are_schema_invalid(self) -> None:
        cases = {
            "not an object": "text",
            "extra key": {**forecast(), "price": "0.4"},
            "float probability": forecast(p_mid=0.6),
            "probability above 0.99": forecast(p_high="0.995"),
            "nan": forecast(p_low="NaN"),
            "unordered": forecast(p_low="0.75"),
            "bad confidence": forecast(confidence="certain"),
            "missing base rate": forecast(base_rate=None),
            "base rate above 1": forecast(base_rate="1.2"),
            "no evidence": forecast(evidence=[]),
            "empty claim": forecast(evidence=[{"claim": " ", "url": "https://apnews.com/b"}]),
            "empty interpretation": forecast(rules_interpretation=" "),
            "abstain with probabilities": forecast(abstain=True, abstain_reason="x"),
            "abstain without reason": forecast(abstain=True, abstain_reason=None, p_low=None,
                                               p_mid=None, p_high=None, confidence=None),
            "reason without abstain": forecast(abstain_reason="x"),
        }
        for label, raw in cases.items():
            with self.subTest(label), self.assertRaises(OutputError) as caught:
                parse_output(raw, FETCHED)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID", label)


class ExposureTests(unittest.TestCase):
    def test_venue_names_and_odds_phrasing_are_flagged(self) -> None:
        flags = scan([
            "Traders on Polymarket give it 62%.",
            "Kalshi lists the contract at 40 cents a share.",
            "The betting odds shortened overnight; bookmakers' odds now 2/1.",
        ])
        for flag in ("venue:polymarket", "venue:kalshi", "phrase:traders_price",
                     "phrase:betting_odds", "phrase:cents_per_share"):
            self.assertIn(flag, flags)
        self.assertEqual(list(flags), sorted(set(flags)))

    def test_clean_reporting_is_not_flagged(self) -> None:
        self.assertEqual(
            scan(["The senate vote is scheduled for Tuesday; 51 senators support it.",
                  "Manifold of a car engine is unrelated."]),
            (),
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_research_output -v`
Expected: ERROR `No module named 'predict_agent.research.schema'`.

- [ ] **Step 3: Implement**

`src/predict_agent/research/schema.py` (create):

```python
"""The forecast output schema and its validation (spec §6: strict JSON schema, citations
must be URLs actually fetched in that session).

Probabilities travel as decimal strings so they never pass through float. The schema
keeps to features structured outputs support (no numeric or string-length constraints);
ranges and ordering are checked here."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

P_MIN = Decimal("0.01")
P_MAX = Decimal("0.99")
CONFIDENCE = ("low", "medium", "high")

_NULLABLE_STRING = {"type": ["string", "null"]}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "abstain",
        "abstain_reason",
        "p_low",
        "p_mid",
        "p_high",
        "confidence",
        "base_rate",
        "rules_interpretation",
        "evidence",
    ],
    "properties": {
        "abstain": {"type": "boolean"},
        "abstain_reason": _NULLABLE_STRING,
        "p_low": _NULLABLE_STRING,
        "p_mid": _NULLABLE_STRING,
        "p_high": _NULLABLE_STRING,
        "confidence": {"type": ["string", "null"], "enum": ["low", "medium", "high", None]},
        "base_rate": _NULLABLE_STRING,
        "rules_interpretation": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim", "url"],
                "properties": {"claim": {"type": "string"}, "url": {"type": "string"}},
            },
        },
    },
}


class OutputError(ValueError):
    """The model's output cannot become a forecast. `code` is the refusal reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class ParsedForecast:
    abstained: bool
    abstain_reason: str | None
    p_low: Decimal | None
    p_mid: Decimal | None
    p_high: Decimal | None
    confidence: str | None
    base_rate: Decimal | None
    rules_interpretation: str
    evidence: tuple[tuple[str, str], ...]  # (claim, url)


def _probability(raw: Mapping[str, Any], key: str, low: Decimal, high: Decimal) -> Decimal:
    value = raw.get(key)
    if not isinstance(value, str):
        raise OutputError("SCHEMA_INVALID", f"{key} must be a decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise OutputError("SCHEMA_INVALID", f"{key} {value!r} is not a decimal") from None
    if not number.is_finite() or not low <= number <= high:
        raise OutputError("SCHEMA_INVALID", f"{key} {value} outside [{low}, {high}]")
    return number


def parse_output(raw: object, fetched_urls: frozenset[str]) -> ParsedForecast:
    """Validate the structured output. Raises OutputError: SCHEMA_INVALID for anything
    malformed, UNFETCHED_CITATION for evidence citing a URL not fetched this session."""
    if not isinstance(raw, Mapping):
        raise OutputError("SCHEMA_INVALID", "output is not an object")
    if set(raw) != set(OUTPUT_SCHEMA["required"]):
        raise OutputError("SCHEMA_INVALID", f"unexpected keys {sorted(raw)}")
    interpretation = raw["rules_interpretation"]
    if not isinstance(interpretation, str) or not interpretation.strip():
        raise OutputError("SCHEMA_INVALID", "rules_interpretation is required")
    evidence_raw = raw["evidence"]
    if not isinstance(evidence_raw, list):
        raise OutputError("SCHEMA_INVALID", "evidence must be a list")
    evidence: list[tuple[str, str]] = []
    for item in evidence_raw:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"claim", "url"}
            or not isinstance(item["claim"], str)
            or not isinstance(item["url"], str)
            or not item["claim"].strip()
        ):
            raise OutputError("SCHEMA_INVALID", "evidence items need a claim and a url")
        if item["url"] not in fetched_urls:
            raise OutputError("UNFETCHED_CITATION", f"{item['url']} was not fetched")
        evidence.append((item["claim"], item["url"]))
    if raw["abstain"] is True:
        reason = raw["abstain_reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise OutputError("SCHEMA_INVALID", "an abstention needs a reason")
        if any(raw[key] is not None for key in ("p_low", "p_mid", "p_high", "confidence")):
            raise OutputError("SCHEMA_INVALID", "an abstention carries no probabilities")
        return ParsedForecast(True, reason, None, None, None, None, None, interpretation,
                              tuple(evidence))
    if raw["abstain"] is not False or raw["abstain_reason"] is not None:
        raise OutputError("SCHEMA_INVALID", "abstain must be false with no abstain_reason")
    p_low = _probability(raw, "p_low", P_MIN, P_MAX)
    p_mid = _probability(raw, "p_mid", P_MIN, P_MAX)
    p_high = _probability(raw, "p_high", P_MIN, P_MAX)
    if not p_low <= p_mid <= p_high:
        raise OutputError("SCHEMA_INVALID", "probabilities must satisfy p_low <= p_mid <= p_high")
    if raw["confidence"] not in CONFIDENCE:
        raise OutputError("SCHEMA_INVALID", f"confidence must be one of {CONFIDENCE}")
    base_rate = _probability(raw, "base_rate", Decimal("0"), Decimal("1"))
    if not evidence:
        raise OutputError("SCHEMA_INVALID", "a forecast needs at least one cited evidence item")
    return ParsedForecast(
        False,
        None,
        p_low,
        p_mid,
        p_high,
        raw["confidence"],
        base_rate,
        interpretation,
        tuple(evidence),
    )
```

`src/predict_agent/research/exposure.py` (create):

```python
"""Price-exposure detection over everything the model saw (spec §6).

Detection is best-effort: a clean scan means "no detected price exposure", never "proven
blind". The scanner looks for prediction-market and betting venue names and for phrasing
that reports market odds or crowd probabilities. Flags are stored on the forecast; flagged
forecasts are reported separately (Plan 5)."""

from __future__ import annotations

import re
from collections.abc import Iterable

VENUES = (
    "polymarket",
    "kalshi",
    "manifold markets",
    "manifold.markets",
    "metaculus",
    "predictit",
    "betfair",
    "smarkets",
    "oddschecker",
    "good judgment open",
    "insight prediction",
)
_PHRASES: dict[str, re.Pattern[str]] = {
    "prediction_market": re.compile(r"prediction[- ]markets?", re.I),
    "betting_odds": re.compile(r"\b(?:betting|bookmakers?'?|bookies'?)\s+odds\b", re.I),
    "odds_of": re.compile(r"\bodds\s+(?:of|on|for)\b", re.I),
    "implied_probability": re.compile(r"implied\s+(?:probability|odds|chance)", re.I),
    "traders_price": re.compile(
        r"\btraders?\b(?:\s+\w+){0,4}?\s+(?:give|gives|price|prices|see|sees|put|puts|bet|bets)\b",
        re.I,
    ),
    "market_chance": re.compile(r"\bmarkets?\s+(?:give|gives|price|prices|put|puts|see|sees)\b",
                                re.I),
    "percent_chance_market": re.compile(
        r"\b\d{1,3}(?:\.\d+)?\s*%\s+(?:chance|probability)\s+(?:on|at|according to)\b", re.I
    ),
    "cents_per_share": re.compile(r"\b\d{1,2}\s*(?:¢|cents?)\s+(?:a|per)\s+share\b", re.I),
}


def scan(texts: Iterable[str]) -> tuple[str, ...]:
    """Sorted, de-duplicated flags such as 'venue:polymarket' or 'phrase:odds_of'."""
    flags: set[str] = set()
    for text in texts:
        lowered = text.lower()
        for venue in VENUES:
            if venue in lowered:
                flags.add(f"venue:{venue.replace(' ', '_')}")
        for name, pattern in _PHRASES.items():
            if pattern.search(text):
                flags.add(f"phrase:{name}")
    return tuple(sorted(flags))
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_research_output -v` → 7 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK (the real-SDK name test skips unless the `predict` extra is installed).

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`

```bash
git add src/predict_agent/research/schema.py src/predict_agent/research/exposure.py tests/predict/test_research_output.py
git commit -m "feat(predict): forecast output validation and price-exposure scanner"
```

---

### Task 3: Locked-down Agent SDK adapter

**Files:**
- Create: `src/predict_agent/research/sdk.py`, `tests/predict/fake_sdk.py`, `tests/predict/test_research_sdk.py`
- Modify: `pyproject.toml`, `src/predict_agent/cli.py`, `tests/predict/test_research_config.py`

**Interfaces:**
- Consumes: `research.config.blocked_host` (Task 1); `research.schema.OUTPUT_SCHEMA` (tests).
- Produces:
  - `research.sdk.ALLOWED_TOOLS = ("WebSearch", "WebFetch")`, `STRUCTURED_OUTPUT_TOOL = "StructuredOutput"`, `EXPECTED_SESSION_TOOLS`
  - `research.sdk.ResearchUnavailable(RuntimeError)`; `load_sdk() -> ModuleType` (importlib; raises `ResearchUnavailable` with the install hint)
  - `research.sdk.ResearchRequest(system_prompt, user_prompt, model, max_turns, budget_usd: Decimal, blocked_domains, output_schema)`
  - `research.sdk.ResearchOutcome(structured_output, transcript: tuple[dict, ...], fetched_urls: frozenset[str], final_text, cost_usd: Decimal | None, error: str | None, detail: str)` — error codes `TOOLSET_MISMATCH`, `MAX_BUDGET`, `MAX_TURNS`, `SCHEMA_INVALID`, `NO_RESULT`, `SDK_ERROR`; transcript events `call` / `denied` / `result`
  - `research.sdk.build_options(sdk, request, session, cwd)`, `toolset_problem(init_data) -> str | None`, `run_research(request, *, load=load_sdk) -> ResearchOutcome`
  - `pyproject.toml`: `predict = ["claude-agent-sdk>=0.2.163"]`; `doctor` prints `research: model <id>; Claude Agent SDK installed|not installed (research disabled)`
  - `tests/predict/fake_sdk.py`: `FakeSdk(Script)`, `Script`, `ToolCall`, `ResultMessage` — replays scripted tool calls through the adapter's real hooks


- [ ] **Step 1: Write the failing tests**

`tests/predict/fake_sdk.py` (create):

```python
"""A stand-in for the `claude_agent_sdk` module: the same names research.sdk uses, and a
`query` that replays a scripted session through the real hooks. No network, no CLI."""

from __future__ import annotations

import os
import types
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class HookMatcher:
    matcher: str | None = None
    hooks: list[Any] = field(default_factory=list)


@dataclass
class ClaudeAgentOptions:
    kwargs: dict[str, Any]


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class TextBlock:
    text: str


@dataclass
class AssistantMessage:
    content: list[Any]
    model: str = "fake"


@dataclass
class ResultMessage:
    subtype: str
    is_error: bool = False
    total_cost_usd: float | None = None
    structured_output: Any = None


@dataclass
class ToolCall:
    """One scripted tool call: run through PreToolUse, and (when allowed) PostToolUse with
    `response` as the tool result."""

    tool: str
    tool_input: dict[str, Any]
    response: Any = None


@dataclass
class Script:
    tools: list[str] = field(
        default_factory=lambda: ["WebSearch", "WebFetch", "StructuredOutput"]
    )
    mcp_servers: list[Any] = field(default_factory=list)
    calls: list[ToolCall] = field(default_factory=list)
    text: str = "Done."
    result: ResultMessage = field(
        default_factory=lambda: ResultMessage(
            "success", total_cost_usd=0.42, structured_output={"ok": True}
        )
    )
    raise_error: Exception | None = None


class FakeSdk:
    """Builds the fake module and records what the adapter did with it."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.options: dict[str, Any] = {}
        self.prompt: str | None = None
        self.decisions: list[dict[str, Any]] = []  # PreToolUse outputs, in order
        self.executed: list[ToolCall] = []  # calls that passed PreToolUse
        self.cwd_existed = False
        self.cwd_entries: list[str] = []

    def module(self) -> types.ModuleType:
        module = types.ModuleType("claude_agent_sdk")
        module.HookMatcher = HookMatcher  # type: ignore[attr-defined]
        module.ClaudeAgentOptions = lambda **kwargs: ClaudeAgentOptions(kwargs)  # type: ignore[attr-defined]
        module.SystemMessage = SystemMessage  # type: ignore[attr-defined]
        module.AssistantMessage = AssistantMessage  # type: ignore[attr-defined]
        module.TextBlock = TextBlock  # type: ignore[attr-defined]
        module.ResultMessage = ResultMessage  # type: ignore[attr-defined]
        module.query = self.query  # type: ignore[attr-defined]
        return module

    async def query(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.prompt = prompt
        self.options = options.kwargs
        cwd = self.options["cwd"]
        self.cwd_existed = os.path.isdir(cwd)
        self.cwd_entries = os.listdir(cwd)
        if self.script.raise_error is not None:
            raise self.script.raise_error
        yield SystemMessage(
            "init", {"tools": self.script.tools, "mcp_servers": self.script.mcp_servers}
        )
        hooks = self.options["hooks"]
        for call in self.script.calls:
            pre = {"tool_name": call.tool, "tool_input": call.tool_input}
            decision = await hooks["PreToolUse"][0].hooks[0](pre, "toolu_1", {"signal": None})
            self.decisions.append(decision)
            output = decision["hookSpecificOutput"]
            if output["permissionDecision"] != "allow":
                continue
            tool_input = output.get("updatedInput", call.tool_input)
            self.executed.append(ToolCall(call.tool, tool_input, call.response))
            post = {"tool_name": call.tool, "tool_input": tool_input,
                    "tool_response": call.response}
            await hooks["PostToolUse"][0].hooks[0](post, "toolu_1", {"signal": None})
        yield AssistantMessage([TextBlock(self.script.text)])
        yield self.script.result
```

`tests/predict/test_research_sdk.py` (create):

```python
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from importlib.util import find_spec
from unittest import mock

from predict_agent.research.schema import OUTPUT_SCHEMA
from predict_agent.research.sdk import (
    EXPECTED_SESSION_TOOLS,
    ResearchRequest,
    ResearchUnavailable,
    _Session,
    build_options,
    load_sdk,
    run_research,
    toolset_problem,
)
from tests.predict.fake_sdk import FakeSdk, ResultMessage, Script, ToolCall

BLOCKED = ("kalshi.com", "polymarket.com")
REQUEST = ResearchRequest(
    system_prompt="system",
    user_prompt="Question: will it happen?",
    model="claude-test",
    max_turns=7,
    budget_usd=Decimal("3.00"),
    blocked_domains=BLOCKED,
    output_schema=OUTPUT_SCHEMA,
)


def run(script: Script) -> tuple[FakeSdk, object]:
    fake = FakeSdk(script)
    outcome = run_research(REQUEST, load=fake.module)
    return fake, outcome


class OptionsTests(unittest.TestCase):
    def test_session_is_isolated_to_the_research_toolset(self) -> None:
        fake, outcome = run(Script())
        options = fake.options
        self.assertEqual(options["tools"], ["WebSearch", "WebFetch"])
        self.assertEqual(options["allowed_tools"], [])
        self.assertEqual(options["setting_sources"], [])
        self.assertEqual(options["mcp_servers"], {})
        self.assertIs(options["strict_mcp_config"], True)
        self.assertEqual(options["skills"], [])
        self.assertEqual(options["permission_mode"], "dontAsk")
        self.assertIs(options["verbatim_prompts"], True)
        self.assertEqual(options["model"], "claude-test")
        self.assertEqual(options["max_turns"], 7)
        self.assertEqual(options["max_budget_usd"], 3.0)
        self.assertEqual(options["system_prompt"], "system")
        self.assertEqual(
            options["output_format"], {"type": "json_schema", "schema": OUTPUT_SCHEMA}
        )
        self.assertEqual(set(options["hooks"]), {"PreToolUse", "PostToolUse"})
        self.assertTrue(fake.cwd_existed)
        self.assertEqual(fake.cwd_entries, [])  # an empty working directory
        self.assertEqual(fake.prompt, REQUEST.user_prompt)
        self.assertIsNone(outcome.error)  # type: ignore[attr-defined]


class HookTests(unittest.TestCase):
    def test_web_search_always_runs_with_the_blocked_domain_list(self) -> None:
        fake, outcome = run(Script(calls=[
            ToolCall("WebSearch", {"query": "q", "allowed_domains": ["polymarket.com"]},
                     {"results": ["snippet"]}),
            ToolCall("WebSearch", {"query": "q2", "blocked_domains": []}, {"results": []}),
        ]))
        for executed in fake.executed:
            self.assertEqual(executed.tool_input["blocked_domains"], list(BLOCKED))
            self.assertNotIn("allowed_domains", executed.tool_input)

    def test_web_fetch_is_denied_for_blocked_hosts_and_non_http_urls(self) -> None:
        urls = {
            "https://polymarket.com/event/x": False,
            "https://www.kalshi.com/markets": False,
            "file:///etc/passwd": False,
            "ftp://example.org/data.csv": False,
            "https://www.reuters.com/world/a": True,
            "http://example.org/b": True,
        }
        fake, outcome = run(Script(calls=[
            ToolCall("WebFetch", {"url": url, "prompt": "p"}, "page") for url in urls
        ]))
        decisions = [d["hookSpecificOutput"]["permissionDecision"] for d in fake.decisions]
        self.assertEqual(decisions, ["allow" if ok else "deny" for ok in urls.values()])
        self.assertEqual(
            outcome.fetched_urls,  # type: ignore[attr-defined]
            frozenset(url for url, ok in urls.items() if ok),
        )

    def test_any_other_tool_is_denied_and_recorded(self) -> None:
        fake, outcome = run(Script(calls=[ToolCall("Bash", {"command": "ls"}, "x")]))
        self.assertEqual(fake.executed, [])
        denied = [e for e in outcome.transcript if e["event"] == "denied"]  # type: ignore[attr-defined]
        self.assertEqual([e["tool"] for e in denied], ["Bash"])

    def test_transcript_keeps_every_result_including_uncited_snippets(self) -> None:
        snippet = {"results": [{"title": "T", "content": "Polymarket traders give it 62%"}]}
        fake, outcome = run(Script(calls=[ToolCall("WebSearch", {"query": "q"}, snippet)]))
        results = [e for e in outcome.transcript if e["event"] == "result"]  # type: ignore[attr-defined]
        self.assertEqual(results[0]["output"], snippet)

    def test_structured_output_tool_is_allowed_but_not_transcribed(self) -> None:
        fake, outcome = run(Script(calls=[ToolCall("StructuredOutput", {"a": 1}, "ok")]))
        self.assertEqual(len(fake.executed), 1)
        self.assertEqual(outcome.transcript, ())  # type: ignore[attr-defined]

    def test_hook_decision_shape(self) -> None:
        import asyncio

        session = _Session(BLOCKED)
        decision = asyncio.run(session.pre_tool_use(
            {"tool_name": "WebFetch", "tool_input": {"url": "https://polymarket.com"}},
            None, {"signal": None},
        ))
        output = decision["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("blocked", output["permissionDecisionReason"])


class SelfCheckTests(unittest.TestCase):
    def test_extra_tool_or_mcp_server_aborts_before_any_tool_runs(self) -> None:
        for script in (
            Script(tools=["WebSearch", "WebFetch", "Bash"],
                   calls=[ToolCall("WebSearch", {"query": "q"}, {})]),
            Script(mcp_servers=[{"name": "x"}], calls=[ToolCall("WebSearch", {"query": "q"}, {})]),
            Script(tools=["WebFetch"], calls=[ToolCall("WebFetch", {"url": "https://a.org"}, "")]),
        ):
            with self.subTest(tools=script.tools, mcp=script.mcp_servers):
                fake, outcome = run(script)
                self.assertEqual(outcome.error, "TOOLSET_MISMATCH")  # type: ignore[attr-defined]
                self.assertEqual(fake.decisions, [])
                self.assertIsNone(outcome.structured_output)  # type: ignore[attr-defined]

    def test_expected_toolset_passes(self) -> None:
        self.assertIsNone(toolset_problem({"tools": sorted(EXPECTED_SESSION_TOOLS),
                                           "mcp_servers": []}))
        self.assertIsNone(toolset_problem({"tools": ["WebSearch", "WebFetch"],
                                           "mcp_servers": []}))
        self.assertIsNotNone(toolset_problem({}))


class ResultTests(unittest.TestCase):
    def test_success_returns_output_text_and_exact_cost(self) -> None:
        output = {"abstain": True}
        fake, outcome = run(Script(text="final words",
                                   result=ResultMessage("success", total_cost_usd=0.1234,
                                                        structured_output=output)))
        self.assertEqual(outcome.structured_output, output)  # type: ignore[attr-defined]
        self.assertEqual(outcome.cost_usd, Decimal("0.1234"))  # type: ignore[attr-defined]
        self.assertEqual(outcome.final_text, "final words")  # type: ignore[attr-defined]
        self.assertIsNone(outcome.error)  # type: ignore[attr-defined]

    def test_failure_subtypes_map_to_codes(self) -> None:
        cases = {
            "error_max_budget_usd": "MAX_BUDGET",
            "error_max_turns": "MAX_TURNS",
            "error_max_structured_output_retries": "SCHEMA_INVALID",
            "error_during_execution": "SDK_ERROR",
        }
        for subtype, code in cases.items():
            with self.subTest(subtype):
                _, outcome = run(Script(result=ResultMessage(subtype, is_error=True,
                                                             total_cost_usd=1.5)))
                self.assertEqual(outcome.error, code)  # type: ignore[attr-defined]
                self.assertEqual(outcome.cost_usd, Decimal("1.5"))  # type: ignore[attr-defined]

    def test_success_without_output_and_missing_cost(self) -> None:
        _, outcome = run(Script(result=ResultMessage("success", total_cost_usd=None)))
        self.assertEqual(outcome.error, "NO_RESULT")  # type: ignore[attr-defined]
        self.assertIsNone(outcome.cost_usd)  # type: ignore[attr-defined]

    def test_sdk_exception_is_an_error_code_with_the_type_name_only(self) -> None:
        _, outcome = run(Script(raise_error=RuntimeError("secret token sk-123 leaked")))
        self.assertEqual(outcome.error, "SDK_ERROR")  # type: ignore[attr-defined]
        self.assertEqual(outcome.detail, "RuntimeError")  # type: ignore[attr-defined]


class AvailabilityTests(unittest.TestCase):
    def test_missing_sdk_raises_research_unavailable(self) -> None:
        with mock.patch.dict(sys.modules, {"claude_agent_sdk": None}), \
                self.assertRaisesRegex(ResearchUnavailable, "pip install"):
            load_sdk()

    @unittest.skipUnless(find_spec("claude_agent_sdk"), "Claude Agent SDK not installed")
    def test_real_sdk_accepts_every_option_name(self) -> None:
        sdk = load_sdk()
        options = build_options(sdk, REQUEST, _Session(BLOCKED), "/tmp")
        self.assertEqual(options.tools, ["WebSearch", "WebFetch"])
        self.assertEqual(options.permission_mode, "dontAsk")
        self.assertEqual(options.setting_sources, [])
        self.assertTrue(options.strict_mcp_config)
        self.assertTrue(options.verbatim_prompts)


if __name__ == "__main__":
    unittest.main()
```

Apply to `tests/predict/test_research_config.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/tests/predict/test_research_config.py b/tests/predict/test_research_config.py
index 3b7107b..da04c2e 100644
--- a/tests/predict/test_research_config.py
+++ b/tests/predict/test_research_config.py
@@ -135,13 +135,13 @@ class ConfigFileTests(unittest.TestCase):
         self.assertEqual(code, 2)
         self.assertIn("no 'research' section", err.getvalue())

-    def test_doctor_reports_the_research_model(self) -> None:
+    def test_doctor_reports_the_research_model_and_sdk_state(self) -> None:
         shutil.copy(EXAMPLE, self.path)
         out = io.StringIO()
         with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
             code = main(["doctor"], root=self.root)
         self.assertEqual(code, 0, out.getvalue())
-        self.assertIn("research: model claude-opus-5-5", out.getvalue())
+        self.assertIn("research: model claude-opus-5-5; Claude Agent SDK", out.getvalue())


 if __name__ == "__main__":
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_research_sdk tests.predict.test_research_config -v`
Expected: ERROR `No module named 'predict_agent.research.sdk'`.

- [ ] **Step 3: Implement**

`src/predict_agent/research/sdk.py` (create):

```python
"""The only module that touches the Claude Agent SDK (spec §6). Imported lazily so the
core stays stdlib-only; install the `predict` extra to use it.

Isolation, all fail closed:
- `tools=["WebSearch", "WebFetch"]` is the available built-in tool set; no settings files,
  MCP servers, skills or project files are loaded; the session runs in an empty directory
  and prompts are delivered verbatim (no @path expansion).
- A PreToolUse hook decides every tool call: WebSearch runs with the blocked-domain list
  forced onto its input; WebFetch runs only for http(s) URLs off the blocked list; the
  structured-output tool is allowed; everything else is denied. `permission_mode="dontAsk"`
  denies anything the hook does not explicitly allow.
- A PostToolUse hook records every tool input and result into the transcript.
- The session's own init report must list no tool outside EXPECTED_SESSION_TOOLS and no
  MCP server, or the run is aborted before any tool executes."""

from __future__ import annotations

import asyncio
import importlib
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from .config import blocked_host

ALLOWED_TOOLS = ("WebSearch", "WebFetch")
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
# What the session may report as available. Pinned by the Plan 4 live smoke run.
EXPECTED_SESSION_TOOLS = frozenset({*ALLOWED_TOOLS, STRUCTURED_OUTPUT_TOOL})
_RESULT_ERRORS = {
    "error_max_budget_usd": "MAX_BUDGET",
    "error_max_turns": "MAX_TURNS",
    "error_max_structured_output_retries": "SCHEMA_INVALID",
}


class ResearchUnavailable(RuntimeError):
    """The Claude Agent SDK is not installed."""


@dataclass(frozen=True)
class ResearchRequest:
    system_prompt: str
    user_prompt: str
    model: str
    max_turns: int
    budget_usd: Decimal
    blocked_domains: tuple[str, ...]
    output_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ResearchOutcome:
    structured_output: Any
    transcript: tuple[dict[str, Any], ...]
    fetched_urls: frozenset[str]
    final_text: str
    cost_usd: Decimal | None
    error: str | None  # refusal code; None on success
    detail: str


def load_sdk() -> ModuleType:
    try:
        return importlib.import_module("claude_agent_sdk")
    except ModuleNotFoundError:
        raise ResearchUnavailable(
            "the Claude Agent SDK is not installed: pip install -e '.[predict]'"
        ) from None


def _fetchable(url: object, blocked: tuple[str, ...]) -> str | None:
    """None when WebFetch may open `url`, else the reason it may not."""
    if not isinstance(url, str):
        return "url is not a string"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"only http(s) URLs may be fetched: {url!r}"
    if blocked_host(parts.hostname, blocked):
        return f"{parts.hostname} is on the blocked-domain list"
    return None


class _Session:
    """Hook state for one research run."""

    def __init__(self, blocked: tuple[str, ...]) -> None:
        self.blocked = blocked
        self.transcript: list[dict[str, Any]] = []
        self.fetched: set[str] = set()

    @staticmethod
    def _decision(decision: str, reason: str = "", **extra: Any) -> dict[str, Any]:
        output: dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            **extra,
        }
        if reason:
            output["permissionDecisionReason"] = reason
        return {"hookSpecificOutput": output}

    async def pre_tool_use(
        self, input_data: Mapping[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        name = input_data.get("tool_name")
        tool_input = dict(input_data.get("tool_input") or {})
        if name == "WebSearch":
            tool_input.pop("allowed_domains", None)
            tool_input["blocked_domains"] = list(self.blocked)
            self.transcript.append({"event": "call", "tool": name, "input": tool_input})
            return self._decision("allow", updatedInput=tool_input)
        if name == "WebFetch":
            reason = _fetchable(tool_input.get("url"), self.blocked)
            if reason is None:
                self.transcript.append({"event": "call", "tool": name, "input": tool_input})
                return self._decision("allow")
            self.transcript.append(
                {"event": "denied", "tool": name, "input": tool_input, "reason": reason}
            )
            return self._decision("deny", reason)
        if name == STRUCTURED_OUTPUT_TOOL:
            return self._decision("allow")
        reason = f"tool {name!r} is not available for research"
        self.transcript.append(
            {"event": "denied", "tool": str(name), "input": tool_input, "reason": reason}
        )
        return self._decision("deny", reason)

    async def post_tool_use(
        self, input_data: Mapping[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        name = input_data.get("tool_name")
        if name == STRUCTURED_OUTPUT_TOOL:
            return {}
        tool_input = dict(input_data.get("tool_input") or {})
        self.transcript.append(
            {
                "event": "result",
                "tool": str(name),
                "input": tool_input,
                "output": input_data.get("tool_response"),
            }
        )
        if name == "WebFetch" and isinstance(tool_input.get("url"), str):
            self.fetched.add(tool_input["url"])
        return {}


def build_options(sdk: ModuleType, request: ResearchRequest, session: _Session, cwd: str) -> Any:
    hook = sdk.HookMatcher
    return sdk.ClaudeAgentOptions(
        tools=list(ALLOWED_TOOLS),
        allowed_tools=[],
        system_prompt=request.system_prompt,
        mcp_servers={},
        strict_mcp_config=True,
        setting_sources=[],
        skills=[],
        permission_mode="dontAsk",
        max_turns=request.max_turns,
        # The SDK takes this cap as a float; it is an operational stop, not ledger money.
        max_budget_usd=float(request.budget_usd),
        model=request.model,
        output_format={"type": "json_schema", "schema": dict(request.output_schema)},
        cwd=cwd,
        verbatim_prompts=True,
        hooks={
            "PreToolUse": [hook(matcher=None, hooks=[session.pre_tool_use])],
            "PostToolUse": [hook(matcher=None, hooks=[session.post_tool_use])],
        },
    )


def toolset_problem(init_data: Mapping[str, Any]) -> str | None:
    """None when the session's init report shows exactly the research toolset."""
    tools = init_data.get("tools")
    if not isinstance(tools, list):
        return "session reported no tool list"
    extra = sorted(set(map(str, tools)) - EXPECTED_SESSION_TOOLS)
    missing = sorted(set(ALLOWED_TOOLS) - set(map(str, tools)))
    if extra or missing:
        return f"unexpected tools {extra}, missing tools {missing}"
    if init_data.get("mcp_servers"):
        return "session reported MCP servers"
    return None


def _cost(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        cost = Decimal(str(value))
    except InvalidOperation:
        return None
    return cost if cost.is_finite() and cost >= 0 else None


async def _run(sdk: ModuleType, request: ResearchRequest) -> ResearchOutcome:
    session = _Session(request.blocked_domains)
    text: list[str] = []
    structured: Any = None
    cost: Decimal | None = None
    error: str | None = "NO_RESULT"
    detail = "the session ended without a result"
    with tempfile.TemporaryDirectory(prefix="predict-research-") as cwd:
        options = build_options(sdk, request, session, cwd)
        try:
            async for message in sdk.query(prompt=request.user_prompt, options=options):
                if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                    problem = toolset_problem(message.data)
                    if problem is not None:
                        error, detail = "TOOLSET_MISMATCH", problem
                        break
                elif isinstance(message, sdk.AssistantMessage):
                    text.extend(
                        block.text for block in message.content
                        if isinstance(block, sdk.TextBlock)
                    )
                elif isinstance(message, sdk.ResultMessage):
                    cost = _cost(message.total_cost_usd)
                    if message.subtype == "success" and not message.is_error:
                        structured = message.structured_output
                        error, detail = (None, "") if structured is not None else (
                            "NO_RESULT", "the session returned no structured output"
                        )
                    else:
                        error = _RESULT_ERRORS.get(message.subtype, "SDK_ERROR")
                        detail = f"session ended with {message.subtype}"
        except Exception as caught:  # noqa: BLE001 — any SDK failure fails this attempt
            error, detail = "SDK_ERROR", type(caught).__name__
    return ResearchOutcome(
        structured_output=structured,
        transcript=tuple(session.transcript),
        fetched_urls=frozenset(session.fetched),
        final_text="\n".join(text),
        cost_usd=cost,
        error=error,
        detail=detail,
    )


def run_research(
    request: ResearchRequest, *, load: Callable[[], ModuleType] = load_sdk
) -> ResearchOutcome:
    """Run one forecast session. Raises ResearchUnavailable when the SDK is missing;
    every other failure is returned as an outcome with an error code."""
    return asyncio.run(_run(load(), request))
```

Apply to `pyproject.toml` (`git apply` accepts this hunk as written):

```diff
diff --git a/pyproject.toml b/pyproject.toml
index ab01717..7cc4a4d 100644
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -14,6 +14,8 @@ dependencies = []

 [project.optional-dependencies]
 research = ["claude-agent-sdk>=0.1.0"]
+# predict_agent research layer; 0.2.163 is the version whose option names Plan 4 verified
+predict = ["claude-agent-sdk>=0.2.163"]
 data = ["yfinance>=0.2.0"]
 dev = ["pytest>=8.0", "ruff>=0.9", "mypy>=1.14"]

```

Apply to `src/predict_agent/cli.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/src/predict_agent/cli.py b/src/predict_agent/cli.py
index 30de25e..a01628e 100644
--- a/src/predict_agent/cli.py
+++ b/src/predict_agent/cli.py
@@ -25,6 +25,7 @@ from .paper import trade_ready
 from .policy_params import load_policy_config
 from .report import render_markdown, shortlist
 from .research.config import load_research_config
+from .research.sdk import ResearchUnavailable, load_sdk
 from .settlement import settle_open_tickets
 from .util import utc_now

@@ -96,7 +97,12 @@ def main(
         except ConfigError as error:
             print(f"predict-agent: {error}", file=sys.stderr)
             return 2
-        print(f"research: model {research.model}")
+        try:
+            load_sdk()
+            sdk_state = "installed"
+        except ResearchUnavailable:
+            sdk_state = "not installed (research disabled)"
+        print(f"research: model {research.model}; Claude Agent SDK {sdk_state}")
         conn = connect(settings.database_path)
         try:
             journal_ok = verify_journal(conn)
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_research_sdk tests.predict.test_research_config -v` → 23 tests OK, 1 skipped without the SDK (with `pip install -e '.[predict]'` the real-SDK option-name test runs and passes).
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK (the real-SDK name test skips unless the `predict` extra is installed).

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`

```bash
git add src/predict_agent/research/sdk.py src/predict_agent/cli.py pyproject.toml tests/predict/fake_sdk.py tests/predict/test_research_sdk.py tests/predict/test_research_config.py
git commit -m "feat(predict): locked-down Agent SDK research adapter"
```

---

### Task 4: Research budget and interrupted attempts

**Files:**
- Create: `src/predict_agent/budget.py`, `tests/predict/test_budget.py`

**Interfaces:**
- Consumes: Plan 2 `forecasts.fail_attempt`, `start_attempt`, `unfinished_attempts`; `cash.parse_money`; `util.parse_datetime`.
- Produces:
  - `budget.INTERRUPTED = "INTERRUPTED"`, `budget.DayUsage(spent_usd: Decimal, entry_attempts: int)`
  - `budget.day_usage(conn, now, per_forecast_usd) -> DayUsage` (UTC day of `now`, every cohort; STARTED at the cap)
  - `budget.budget_refusal(usage, *, daily_usd, per_forecast_usd, max_entries) -> "BUDGET" | "VOLUME" | None`
  - `budget.recover_interrupted_attempts(conn, per_forecast_usd, now) -> list[int]`


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_budget.py` (create):

```python
from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from predict_agent.budget import (
    INTERRUPTED,
    DayUsage,
    budget_refusal,
    day_usage,
    recover_interrupted_attempts,
)
from predict_agent.db import connect
from predict_agent.forecasts import fail_attempt, start_attempt, unfinished_attempts
from tests.predict.fixtures import CONDITION_ID, NOW
from tests.predict.ledger_fixtures import seed_cohort

CAP = Decimal("3.00")


class BudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.cohort = seed_cohort(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def attempt(self, kind: str = "entry", at_hours: float = 0, cost: str | None = "1.25") -> int:
        at = NOW + timedelta(hours=at_hours)
        attempt_id = start_attempt(self.conn, self.cohort, CONDITION_ID, kind, at)
        if cost is not None:
            fail_attempt(self.conn, attempt_id, Decimal(cost), "SDK_ERROR", at)
        return attempt_id

    def test_failed_attempts_count_and_started_ones_count_at_the_cap(self) -> None:
        self.attempt(cost="1.25")
        self.attempt(kind="update", cost="0.50")
        self.attempt(cost=None)  # in flight or interrupted
        usage = day_usage(self.conn, NOW, CAP)
        self.assertEqual(usage, DayUsage(Decimal("4.75"), 2))

    def test_only_the_utc_day_of_now_counts(self) -> None:
        self.attempt(at_hours=-13, cost="9")  # 2026-10-04T23:00Z
        self.attempt(at_hours=11.5, cost="2")  # 2026-10-05T23:30Z
        self.assertEqual(day_usage(self.conn, NOW, CAP).spent_usd, Decimal("2"))

    def test_refusals(self) -> None:
        def refusal(spent: str, entries: int) -> str | None:
            return budget_refusal(DayUsage(Decimal(spent), entries), daily_usd=Decimal("30"),
                                  per_forecast_usd=CAP, max_entries=15)

        self.assertIsNone(refusal("27.00", 14))
        self.assertEqual(refusal("27.01", 0), "BUDGET")
        self.assertEqual(refusal("0", 15), "VOLUME")

    def test_interrupted_attempts_are_closed_at_the_cap(self) -> None:
        open_id = self.attempt(cost=None)
        done_id = self.attempt(cost="1")
        recovered = recover_interrupted_attempts(self.conn, CAP, NOW + timedelta(hours=1))
        self.assertEqual(recovered, [open_id])
        self.assertEqual(unfinished_attempts(self.conn, self.cohort), [])
        row = self.conn.execute(
            "SELECT status, cost_usd, error FROM research_attempts WHERE attempt_id = ?",
            (open_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("FAILED", "3.00", INTERRUPTED))
        cost = self.conn.execute(
            "SELECT cost_usd FROM research_attempts WHERE attempt_id = ?", (done_id,)
        ).fetchone()[0]
        self.assertEqual(cost, "1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_budget -v`
Expected: ERROR `No module named 'predict_agent.budget'`.

- [ ] **Step 3: Implement**

`src/predict_agent/budget.py` (create):

```python
"""Research spend and volume limits (spec §6: daily and per-forecast USD caps counting
failed attempts; at most N new entry forecasts per day).

Spend is read from the durable `research_attempts` rows of every cohort for the UTC day.
An attempt still STARTED (in flight, or interrupted by a crash) counts at the full
per-forecast cap, because its real cost is unknown. Amounts are summed in Python."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .cash import parse_money
from .forecasts import fail_attempt
from .util import parse_datetime

INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True)
class DayUsage:
    spent_usd: Decimal
    entry_attempts: int


def _day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def day_usage(conn: sqlite3.Connection, now: datetime, per_forecast_usd: Decimal) -> DayUsage:
    start, end = _day_bounds(now.astimezone(UTC))
    spent = Decimal("0")
    entries = 0
    for row in conn.execute("SELECT kind, status, cost_usd, started_at FROM research_attempts"):
        if not start <= parse_datetime(row["started_at"]) < end:
            continue
        spent += per_forecast_usd if row["status"] == "STARTED" else parse_money(row["cost_usd"])
        entries += row["kind"] == "entry"
    return DayUsage(spent, entries)


def budget_refusal(
    usage: DayUsage, *, daily_usd: Decimal, per_forecast_usd: Decimal, max_entries: int
) -> str | None:
    """BUDGET when one more full-cost attempt could exceed the daily cap; VOLUME when the
    day's entry forecasts are used up."""
    if usage.spent_usd + per_forecast_usd > daily_usd:
        return "BUDGET"
    if usage.entry_attempts >= max_entries:
        return "VOLUME"
    return None


def recover_interrupted_attempts(
    conn: sqlite3.Connection, per_forecast_usd: Decimal, now: datetime
) -> list[int]:
    """Close every STARTED attempt left by an earlier run as FAILED, charged at the full
    per-forecast cap (its real cost is unknown). Call before starting new attempts; the
    caller must hold the only research run (Plan 5 adds the cron lock)."""
    rows = conn.execute(
        "SELECT attempt_id FROM research_attempts WHERE status = 'STARTED' ORDER BY attempt_id"
    ).fetchall()
    recovered = []
    for row in rows:
        fail_attempt(conn, row["attempt_id"], per_forecast_usd, INTERRUPTED, now)
        recovered.append(row["attempt_id"])
    return recovered
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_budget -v` → 4 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK (the real-SDK name test skips unless the `predict` extra is installed).

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`

```bash
git add src/predict_agent/budget.py tests/predict/test_budget.py
git commit -m "feat(predict): research spend and volume limits; interrupted-attempt recovery"
```

---

### Task 5: The research run and `predict-agent research`

**Files:**
- Create: `src/predict_agent/research_run.py`, `tests/predict/test_research_run.py`
- Modify: `src/predict_agent/collect.py`, `src/predict_agent/cli.py`, `tests/predict/test_cli_isolation.py`, `CLAUDE.md`

**Interfaces:**
- Consumes: Tasks 1–4; Plan 1 `collect` (`start_run`, `finish_run`, `fetch_book`/`parse_book`/`book_refusal`), `db.record_refusal`, `http.JsonClient`; Plan 2 `cohorts.ensure_cohort`/`CohortIdentity`, `forecasts.*`, `artifacts.store_artifact`; Plan 3 `paper.trade_ready`, `paper.latest_discovery_run`, `policy_params.variant_policies`/`PolicyParams`.
- Produces:
  - `collect.snapshot_book(conn, client, *, run_id, source_run_id, condition_id, outcome, token_id, fees_enabled, fee_schedule_json, now_fn) -> int | None` (`snapshot_eligible` now uses it; behaviour unchanged)
  - `research_run.ResearchRunner = Callable[[ResearchRequest], ResearchOutcome]`; `ResearchSummary` (cohort_id, recovered, forecasts, abstentions, baselines, no_timely_baseline, traded, failed, skipped)
  - `research_run.cohort_identity(conn, policy, research, now)`, `candidates(conn, cohort_id, now, min_hours_to_close)`, `take_baseline(conn, client, forecast_id, run_id, now_fn) -> bool`, `research_market(conn, runner, cohort_id, row, research, run_id, now_fn)`, `resume_forecasts(...)`, `run_research_day(conn, client, runner, *, policy, research, config_hash, code_version, now_fn) -> ResearchSummary`
  - CLI: `predict-agent research` (exit 2 without the SDK or with bad config; prints the summary line); `main(..., runner=None)` lets tests inject a runner
  - Isolation test: `predict_agent.research` imports nothing from policy, policy_params, paper, tickets, cash, settlement, fills, invariants, budget, research_run, collect, clob, http, gamma, report, db


- [ ] **Step 1: Write the failing tests**

`tests/predict/test_research_run.py` (create):

```python
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

from predict_agent.cli import main
from predict_agent.db import connect
from predict_agent.forecasts import start_attempt
from predict_agent.http import JsonClient
from predict_agent.invariants import verify_ledger
from predict_agent.policy_params import parse_policy
from predict_agent.research.config import parse_research
from predict_agent.research.sdk import ResearchOutcome, ResearchRequest
from predict_agent.research_run import ResearchSummary, run_research_day
from tests.predict.fakes import RoutedOpener
from tests.predict.fixtures import CONDITION_ID, NO_TOKEN, NOW, YES_TOKEN, clob_book
from tests.predict.trade_fixtures import seed_discovery, seed_tradeable_market

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = json.loads((REPO / "config" / "predict-policy.example.json").read_text())
POLICY = parse_policy(EXAMPLE["policy"], "bounds")
RESEARCH = parse_research(EXAMPLE["research"])
SOURCE = "https://www.reuters.com/a"
OTHER = ["0x" + digit * 64 for digit in "abc"]


class Clock:
    """Starts at `start` and advances one second per call."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def forecast_output(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "abstain": False,
        "abstain_reason": None,
        "p_low": "0.55",
        "p_mid": "0.60",
        "p_high": "0.70",
        "confidence": "medium",
        "base_rate": "0.30",
        "rules_interpretation": "Resolves on the official announcement.",
        "evidence": [{"claim": "The bill passed committee.", "url": SOURCE}],
    }
    raw.update(changes)
    return raw


def outcome(**changes: Any) -> ResearchOutcome:
    base = ResearchOutcome(
        structured_output=forecast_output(),
        transcript=(
            {"event": "call", "tool": "WebFetch", "input": {"url": SOURCE}},
            {"event": "result", "tool": "WebFetch", "input": {"url": SOURCE},
             "output": "The bill passed committee on Monday."},
        ),
        fetched_urls=frozenset({SOURCE}),
        final_text="Forecast complete.",
        cost_usd=Decimal("0.80"),
        error=None,
        detail="",
    )
    return replace(base, **changes)


class FakeRunner:
    def __init__(self, *outcomes: ResearchOutcome) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[ResearchRequest] = []

    def __call__(self, request: ResearchRequest) -> ResearchOutcome:
        self.requests.append(request)
        return self.outcomes.pop(0) if self.outcomes else outcome()


def books(condition_id: str = CONDITION_ID, *, crossed: bool = False) -> list[dict[str, Any]]:
    """YES then NO book for one market, as the CLOB serves them."""
    yes = clob_book(market=condition_id, asset_id=YES_TOKEN)
    no_bids = [{"price": "0.80" if crossed else "0.55", "size": "10"}]
    no = clob_book(market=condition_id, asset_id=NO_TOKEN, bids=no_bids,
                   asks=[{"price": "0.70", "size": "100"}])
    return [yes, no]


class ResearchRunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "config").mkdir()
        shutil.copy(REPO / "config" / "predict-policy.example.json",
                    self.root / "config" / "predict-policy.json")
        self.conn = connect(self.root / "data" / "predict.sqlite3")
        self.markets = [CONDITION_ID]
        seed_tradeable_market(self.conn, CONDITION_ID, end_date=NOW + timedelta(days=20))
        seed_discovery(self.conn, [CONDITION_ID], at=NOW - timedelta(hours=1))

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def add_markets(self, *condition_ids: str, end_days: float = 20) -> None:
        for condition_id in condition_ids:
            seed_tradeable_market(self.conn, condition_id,
                                  end_date=NOW + timedelta(days=end_days))
        self.markets += list(condition_ids)
        seed_discovery(self.conn, self.markets, at=NOW - timedelta(minutes=30))

    def run_day(
        self,
        runner: FakeRunner,
        book_queue: list[dict[str, Any]] | None = None,
        start: datetime = NOW,
        research: Any = RESEARCH,
    ) -> ResearchSummary:
        opener = RoutedOpener({"/book": book_queue if book_queue is not None else books()})
        client = JsonClient(opener=opener, sleep=lambda _: None, jitter=lambda: 0.0)
        return run_research_day(self.conn, client, runner, policy=POLICY, research=research,
                                config_hash="h", code_version="test", now_fn=Clock(start))

    def scalar(self, sql: str) -> Any:
        return self.conn.execute(sql).fetchone()[0]


class HappyPathTests(ResearchRunTestCase):
    def test_forecast_baseline_and_trades_in_one_run(self) -> None:
        runner = FakeRunner()
        summary = self.run_day(runner)
        self.assertEqual((summary.forecasts, summary.baselines, summary.traded), (1, 1, 2))
        self.assertEqual(dict(summary.failed), {})
        attempt = self.conn.execute("SELECT status, cost_usd FROM research_attempts").fetchone()
        self.assertEqual(tuple(attempt), ("SUCCEEDED", "0.80"))
        body = json.loads(self.scalar("SELECT body_json FROM forecasts"))
        self.assertEqual(body["evidence"], [{"claim": "The bill passed committee.",
                                             "url": SOURCE}])
        self.assertEqual(body["exposure_flags"], [])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM paper_tickets"), 2)
        self.assertEqual(verify_ledger(self.conn), [])

    def test_the_prompt_never_carries_a_price(self) -> None:
        runner = FakeRunner()
        self.run_day(runner)
        sent = runner.requests[0].user_prompt
        for price in ("0.37", "0.38", "0.45", "0.70", "bestAsk", "liquidity"):
            self.assertNotIn(price, sent)
        stored = json.loads(self.scalar(
            "SELECT content FROM artifacts WHERE kind = 'research_input'"))
        self.assertEqual(stored["user"], sent)

    def test_exposure_in_an_uncited_snippet_is_flagged_on_the_forecast(self) -> None:
        snippet = {"event": "result", "tool": "WebSearch", "input": {"query": "q"},
                   "output": {"results": [{"content": "Polymarket traders give it 62%"}]}}
        runner = FakeRunner(outcome(transcript=outcome().transcript + (snippet,)))
        self.run_day(runner)
        flags = json.loads(self.scalar("SELECT body_json FROM forecasts"))["exposure_flags"]
        self.assertIn("venue:polymarket", flags)
        transcript = json.loads(self.scalar(
            "SELECT content FROM artifacts WHERE kind = 'tool_transcript'"))
        self.assertIn(snippet, transcript)

    def test_abstention_is_recorded_baselined_and_refused(self) -> None:
        runner = FakeRunner(outcome(structured_output=forecast_output(
            abstain=True, abstain_reason="rules ambiguous", p_low=None, p_mid=None,
            p_high=None, confidence=None, base_rate=None, evidence=[])))
        summary = self.run_day(runner)
        self.assertEqual((summary.forecasts, summary.abstentions, summary.traded), (1, 1, 0))
        reasons = [r[0] for r in self.conn.execute("SELECT reason FROM decisions")]
        self.assertEqual(reasons, ["ABSTAINED", "ABSTAINED"])

    def test_already_forecast_and_closing_markets_are_not_researched(self) -> None:
        self.run_day(FakeRunner())
        self.add_markets(OTHER[0], end_days=1)  # closes within 48h
        runner = FakeRunner()
        summary = self.run_day(runner, book_queue=[], start=NOW + timedelta(hours=1))
        self.assertEqual((summary.forecasts, len(runner.requests)), (0, 0))


class FailureTests(ResearchRunTestCase):
    def test_runner_error_fails_the_attempt_with_its_cost_and_fetches_no_books(self) -> None:
        runner = FakeRunner(outcome(error="MAX_BUDGET", cost_usd=Decimal("3.00"),
                                    structured_output=None))
        summary = self.run_day(runner, book_queue=[])
        self.assertEqual(dict(summary.failed), {"MAX_BUDGET": 1})
        row = self.conn.execute("SELECT status, cost_usd, error FROM research_attempts").fetchone()
        self.assertEqual(tuple(row), ("FAILED", "3.00", "MAX_BUDGET"))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM forecasts"), 0)

    def test_failure_detail_is_kept_in_the_run_refusals(self) -> None:
        runner = FakeRunner(outcome(error="TOOLSET_MISMATCH", cost_usd=None,
                                    detail="unexpected tools ['Bash'], missing tools []",
                                    structured_output=None))
        self.run_day(runner, book_queue=[])
        refusal = self.conn.execute(
            "SELECT reason_code, detail FROM refusals WHERE stage = 'research'").fetchone()
        self.assertEqual(tuple(refusal),
                         ("TOOLSET_MISMATCH", "unexpected tools ['Bash'], missing tools []"))

    def test_unknown_cost_is_charged_at_the_per_forecast_cap(self) -> None:
        self.run_day(FakeRunner(outcome(error="SDK_ERROR", cost_usd=None)), book_queue=[])
        self.assertEqual(self.scalar("SELECT cost_usd FROM research_attempts"), "3.00")

    def test_citation_not_fetched_in_the_session_fails_the_attempt(self) -> None:
        runner = FakeRunner(outcome(fetched_urls=frozenset()))
        summary = self.run_day(runner, book_queue=[])
        self.assertEqual(dict(summary.failed), {"UNFETCHED_CITATION": 1})
        self.assertEqual(self.scalar("SELECT status FROM research_attempts"), "FAILED")


class BudgetTests(ResearchRunTestCase):
    def test_daily_cap_stops_research_and_records_budget_refusals(self) -> None:
        self.add_markets(*OTHER[:2])
        research = replace(RESEARCH, daily_usd=Decimal("4.00"))
        queue = books(self.markets[0]) + books(self.markets[1])
        summary = self.run_day(FakeRunner(), book_queue=queue, research=research)
        self.assertEqual(summary.forecasts, 2)
        self.assertEqual(dict(summary.skipped), {"BUDGET": 1})
        codes = [r[0] for r in self.conn.execute(
            "SELECT reason_code FROM refusals WHERE stage = 'research'")]
        self.assertEqual(codes, ["BUDGET"])

    def test_volume_cap_limits_new_entry_forecasts(self) -> None:
        self.add_markets(*OTHER[:2])
        research = replace(RESEARCH, max_entry_forecasts_per_day=1)
        summary = self.run_day(FakeRunner(), book_queue=books(self.markets[0]),
                               research=research)
        self.assertEqual((summary.forecasts, dict(summary.skipped)), (1, {"VOLUME": 2}))


class ResumeTests(ResearchRunTestCase):
    def test_interrupted_attempt_is_recovered_at_the_cap(self) -> None:
        cohort = self.run_day(FakeRunner(), book_queue=books()).cohort_id
        start_attempt(self.conn, cohort, OTHER[0], "entry", NOW + timedelta(minutes=5))
        summary = self.run_day(FakeRunner(), book_queue=[], start=NOW + timedelta(minutes=10))
        self.assertEqual(summary.recovered, 1)
        failed = self.conn.execute(
            "SELECT cost_usd, error FROM research_attempts WHERE status = 'FAILED'").fetchone()
        self.assertEqual(tuple(failed), ("3.00", "INTERRUPTED"))

    def test_missing_baseline_is_taken_on_resume_within_the_window(self) -> None:
        first = self.run_day(FakeRunner(), book_queue=books(crossed=True))
        self.assertEqual((first.forecasts, first.baselines, first.traded), (1, 0, 0))
        second = self.run_day(FakeRunner(), book_queue=books(),
                              start=NOW + timedelta(minutes=10))
        self.assertEqual((second.baselines, second.traded), (1, 2))

    def test_expired_baseline_is_marked_no_timely_baseline(self) -> None:
        self.run_day(FakeRunner(), book_queue=books(crossed=True))
        later = self.run_day(FakeRunner(), book_queue=[], start=NOW + timedelta(minutes=45))
        self.assertEqual(later.no_timely_baseline, 1)
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM decisions")]
        self.assertEqual(kinds, ["NO_TIMELY_BASELINE", "NO_TIMELY_BASELINE"])


class CliTests(ResearchRunTestCase):
    def cli(self, runner: FakeRunner | None, book_queue: list[dict[str, Any]]) -> tuple[int, str]:
        opener = RoutedOpener({"/book": book_queue})
        client = JsonClient(opener=opener, sleep=lambda _: None, jitter=lambda: 0.0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["research"], root=self.root, client=client, now_fn=Clock(NOW),
                        runner=runner)
        return code, out.getvalue()

    def test_research_command_prints_the_run_summary(self) -> None:
        code, output = self.cli(FakeRunner(), books())
        self.assertEqual(code, 0, output)
        self.assertIn("forecasts 1 (abstained 0)", output)
        self.assertIn("traded 2", output)

    def test_research_command_without_the_sdk_fails_closed(self) -> None:
        with mock.patch.dict(sys.modules, {"claude_agent_sdk": None}):
            code, output = self.cli(None, [])
        self.assertEqual(code, 2)
        self.assertIn("pip install", output)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM research_attempts"), 0)


if __name__ == "__main__":
    unittest.main()
```

Apply to `tests/predict/test_cli_isolation.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/tests/predict/test_cli_isolation.py b/tests/predict/test_cli_isolation.py
index 4f7f16d..d4f671a 100644
--- a/tests/predict/test_cli_isolation.py
+++ b/tests/predict/test_cli_isolation.py
@@ -40,6 +40,31 @@ class IsolationTests(unittest.TestCase):
                     with self.subTest(file=path.name, module=name):
                         self.assertFalse(name.startswith("japan_agent"))

+    def test_research_package_cannot_reach_prices_money_or_trading(self) -> None:
+        # Spec §6/§8: the research layer imports nothing that touches money, policy,
+        # paper trading or market data, so no code path can hand Claude a price.
+        forbidden = {
+            "policy", "policy_params", "paper", "tickets", "cash", "settlement", "fills",
+            "invariants", "budget", "research_run", "collect", "clob", "http", "gamma",
+            "report", "db",
+        }
+        for path in (PACKAGE / "research").rglob("*.py"):
+            tree = ast.parse(path.read_text(encoding="utf-8"))
+            for node in ast.walk(tree):
+                modules: list[str] = []
+                if isinstance(node, ast.ImportFrom):
+                    module = node.module or ""
+                    if node.level >= 2 or module.startswith("predict_agent."):
+                        modules = [module.removeprefix("predict_agent.").split(".")[0]]
+                        modules += [alias.name for alias in node.names]
+                elif isinstance(node, ast.Import):
+                    modules = [alias.name.removeprefix("predict_agent.").split(".")[0]
+                               for alias in node.names
+                               if alias.name.startswith("predict_agent.")]
+                for module in modules:
+                    with self.subTest(file=path.name, module=module):
+                        self.assertNotIn(module, forbidden)
+
     def test_no_write_http_methods_or_signing_in_package(self) -> None:
         forbidden = ("method=\"POST\"", "method='POST'", "eth_account", "private_key", "sign_order")
         for path in PACKAGE.rglob("*.py"):
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_research_run tests.predict.test_cli_isolation -v`
Expected: ERROR `No module named 'predict_agent.research_run'`.

- [ ] **Step 3: Implement**

Apply to `src/predict_agent/collect.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/src/predict_agent/collect.py b/src/predict_agent/collect.py
index a2961ab..aca3ed4 100644
--- a/src/predict_agent/collect.py
+++ b/src/predict_agent/collect.py
@@ -252,6 +252,60 @@ def discover(
     return DiscoverySummary(markets_seen=len(seen), eligible=eligible, refusals=dict(refusals))


+def snapshot_book(
+    conn: sqlite3.Connection,
+    client: JsonClient,
+    *,
+    run_id: str,
+    source_run_id: str,
+    condition_id: str,
+    outcome: str,
+    token_id: str,
+    fees_enabled: int,
+    fee_schedule_json: str | None,
+    now_fn: Callable[[], datetime],
+) -> int | None:
+    """Fetch, validate and store one token's book. Returns the snapshot id, or None after
+    recording the refusal (fetch/parse error or an unusable book)."""
+    try:
+        raw = fetch_book(client, token_id)
+        fetched_at = now_fn()
+        snapshot = parse_book(raw, fetched_at)
+    except (FetchError, ParseError) as error:
+        code = "FETCH_ERROR" if isinstance(error, FetchError) else "PARSE_ERROR"
+        record_refusal(conn, run_id, condition_id, "snapshot", code, str(error), now_fn())
+        return None
+    reason = book_refusal(snapshot, condition_id, token_id)
+    if reason is not None:
+        record_refusal(conn, run_id, condition_id, "snapshot", reason, outcome, fetched_at)
+        return None
+    digest = snapshot_hash(snapshot)
+    with transaction(conn):
+        conn.execute(
+            "INSERT OR IGNORE INTO book_snapshots (run_id, source_run_id, condition_id, "
+            "token_id, outcome, observed_at, fetched_at, record_json, fees_enabled, "
+            "fee_schedule_json, snapshot_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
+            (
+                run_id,
+                source_run_id,
+                condition_id,
+                token_id,
+                outcome,
+                isoformat(snapshot.observed_at),
+                isoformat(snapshot.fetched_at),
+                canonical_json(snapshot.record()),
+                fees_enabled,
+                fee_schedule_json,
+                digest,
+            ),
+        )
+        row = conn.execute(
+            "SELECT id FROM book_snapshots WHERE snapshot_hash = ?", (digest,)
+        ).fetchone()
+    snapshot_id: int = row["id"]
+    return snapshot_id
+
+
 def snapshot_eligible(
     conn: sqlite3.Connection,
     client: JsonClient,
@@ -268,40 +322,19 @@ def snapshot_eligible(
     stored = 0
     for row in rows:
         for outcome, token_id in (("YES", row["yes_token_id"]), ("NO", row["no_token_id"])):
-            condition_id = row["condition_id"]
-            try:
-                raw = fetch_book(client, token_id)
-                fetched_at = now_fn()
-                snapshot = parse_book(raw, fetched_at)
-            except (FetchError, ParseError) as error:
-                code = "FETCH_ERROR" if isinstance(error, FetchError) else "PARSE_ERROR"
-                record_refusal(conn, run_id, condition_id, "snapshot", code, str(error), now_fn())
-                continue
-            reason = book_refusal(snapshot, condition_id, token_id)
-            if reason is not None:
-                record_refusal(conn, run_id, condition_id, "snapshot", reason, outcome, fetched_at)
-                continue
-            digest = snapshot_hash(snapshot)
-            with transaction(conn):
-                conn.execute(
-                    "INSERT OR IGNORE INTO book_snapshots (run_id, source_run_id, condition_id, "
-                    "token_id, outcome, observed_at, fetched_at, record_json, fees_enabled, "
-                    "fee_schedule_json, snapshot_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
-                    (
-                        run_id,
-                        source_run_id,
-                        condition_id,
-                        token_id,
-                        outcome,
-                        isoformat(snapshot.observed_at),
-                        isoformat(snapshot.fetched_at),
-                        canonical_json(snapshot.record()),
-                        row["fees_enabled"],
-                        row["fee_schedule_json"],
-                        digest,
-                    ),
-                )
-            stored += 1
+            snapshot_id = snapshot_book(
+                conn,
+                client,
+                run_id=run_id,
+                source_run_id=source_run_id,
+                condition_id=row["condition_id"],
+                outcome=outcome,
+                token_id=token_id,
+                fees_enabled=row["fees_enabled"],
+                fee_schedule_json=row["fee_schedule_json"],
+                now_fn=now_fn,
+            )
+            stored += snapshot_id is not None
     return stored


```

`src/predict_agent/research_run.py` (create):

```python
"""The daily research run (spec §3 stages 2–5, §6, §7).

1. Open (or reuse) the cohort for the configured policy, prompt, model and research
   settings.
2. Close attempts an earlier run left STARTED, charging the full per-forecast cap.
3. Resume unfinished forecasts: take missing baselines while the window is open, mark the
   rest NO_TIMELY_BASELINE, and decide whatever has a baseline.
4. For each eligible market of the latest discovery run that this cohort has not forecast,
   in order: check the day's budget and volume, open an attempt, run research (no price),
   validate the output, record the forecast (or fail the attempt with its cost), then
   fetch both books immediately as the post-forecast baseline and decide every portfolio.

The research runner is injected, so tests run with no network and no SDK."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .artifacts import store_artifact
from .budget import budget_refusal, day_usage, recover_interrupted_attempts
from .cash import LedgerError
from .cohorts import CohortIdentity, ensure_cohort
from .collect import finish_run, snapshot_book, start_run
from .db import record_refusal
from .forecasts import (
    ForecastError,
    ForecastRecord,
    ResumeStep,
    attach_baseline,
    fail_attempt,
    mark_no_timely_baseline,
    record_forecast,
    resume_step,
    start_attempt,
    unfinished_forecasts,
)
from .http import JsonClient
from .paper import latest_discovery_run, trade_ready
from .policy_params import PolicyParams, variant_policies
from .research.config import ResearchConfig
from .research.exposure import scan
from .research.prompt import SYSTEM_PROMPT, prompt_artifact, render_user_prompt, research_input
from .research.schema import OUTPUT_SCHEMA, OutputError, parse_output
from .research.sdk import ResearchOutcome, ResearchRequest
from .util import canonical_json, parse_datetime

ResearchRunner = Callable[[ResearchRequest], ResearchOutcome]


@dataclass
class ResearchSummary:
    cohort_id: str
    recovered: int = 0
    forecasts: int = 0
    abstentions: int = 0
    baselines: int = 0
    no_timely_baseline: int = 0
    traded: int = 0
    failed: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)


def cohort_identity(
    conn: sqlite3.Connection, policy: PolicyParams, research: ResearchConfig, now: datetime
) -> CohortIdentity:
    """Store the policy and prompt artifacts and build the cohort identity from config."""
    portfolios = {
        variant: store_artifact(conn, "policy", content, now)
        for variant, content in variant_policies(policy).items()
    }
    return CohortIdentity(
        portfolios=portfolios,
        prompt_hash=store_artifact(conn, "prompt", prompt_artifact(), now),
        model_id=research.model,
        research_settings=research.settings_record(),
        scoring_version=research.scoring_version,
        baseline_window_seconds=research.baseline_window_seconds,
        generation=research.generation,
    )


def candidates(
    conn: sqlite3.Connection, cohort_id: str, now: datetime, min_hours_to_close: int
) -> list[sqlite3.Row]:
    """Markets of the latest completed discovery run that this cohort has not forecast and
    that do not close within the policy's minimum time, in condition-id order."""
    run_id = latest_discovery_run(conn)
    if run_id is None:
        return []
    rows = conn.execute(
        "SELECT d.condition_id, d.rules_hash, r.rules_json FROM discoveries d "
        "JOIN rules_versions r ON r.condition_id = d.condition_id "
        "AND r.rules_hash = d.rules_hash "
        "WHERE d.run_id = ? AND NOT EXISTS (SELECT 1 FROM forecasts f "
        "WHERE f.cohort_id = ? AND f.condition_id = d.condition_id AND f.kind = 'entry') "
        "ORDER BY d.condition_id",
        (run_id, cohort_id),
    ).fetchall()
    horizon = now + timedelta(hours=min_hours_to_close)
    selected = []
    for row in rows:
        end_date = json.loads(row["rules_json"]).get("end_date")
        if end_date and parse_datetime(end_date) >= horizon:
            selected.append(row)
    return selected


def take_baseline(
    conn: sqlite3.Connection,
    client: JsonClient,
    forecast_id: int,
    run_id: str,
    now_fn: Callable[[], datetime],
) -> bool:
    """Fetch both books now and attach them as the forecast's baseline. False when a book
    is refused or arrives after the window (the forecast then waits for resume)."""
    market = conn.execute(
        "SELECT m.* FROM forecasts f JOIN markets m ON m.condition_id = f.condition_id "
        "WHERE f.forecast_id = ?",
        (forecast_id,),
    ).fetchone()
    snapshots = {}
    for outcome, token_column in (("YES", "yes_token_id"), ("NO", "no_token_id")):
        snapshot_id = snapshot_book(
            conn,
            client,
            run_id=run_id,
            source_run_id=run_id,
            condition_id=market["condition_id"],
            outcome=outcome,
            token_id=market[token_column],
            fees_enabled=market["fees_enabled"],
            fee_schedule_json=market["fee_schedule_json"],
            now_fn=now_fn,
        )
        if snapshot_id is None:
            return False
        snapshots[outcome] = snapshot_id
    try:
        attach_baseline(conn, forecast_id, snapshots["YES"], snapshots["NO"], now_fn())
    except ForecastError as error:
        record_refusal(
            conn, run_id, market["condition_id"], "baseline", "LATE_BASELINE", str(error),
            now_fn(),
        )
        return False
    return True


def _exposure_texts(outcome: ResearchOutcome) -> list[str]:
    texts = [outcome.final_text, canonical_json(outcome.structured_output)]
    texts += [canonical_json(event.get("output")) for event in outcome.transcript]
    return texts


def research_market(
    conn: sqlite3.Connection,
    runner: ResearchRunner,
    cohort_id: str,
    row: sqlite3.Row,
    research: ResearchConfig,
    run_id: str,
    now_fn: Callable[[], datetime],
) -> tuple[int | None, str | None]:
    """(forecast_id, None) on success, (None, failure code) otherwise. The attempt is
    always closed: SUCCEEDED with the forecast, or FAILED with what it cost; a failure's
    detail (tool names, SDK error type, schema problem) is kept in the run's refusals."""
    rules = json.loads(row["rules_json"])
    started = now_fn()
    attempt_id = start_attempt(conn, cohort_id, row["condition_id"], "entry", started)
    user_prompt = render_user_prompt(
        question=rules["question"],
        rules_text=rules["rules_text"],
        resolution_source=rules["resolution_source"],
        end_date=parse_datetime(rules["end_date"]),
        today=started,
    )
    outcome = runner(
        ResearchRequest(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=research.model,
            max_turns=research.max_turns,
            budget_usd=research.per_forecast_usd,
            blocked_domains=research.blocked_domains,
            output_schema=OUTPUT_SCHEMA,
        )
    )
    # An unknown cost is charged at the full cap, so the budget never under-counts.
    cost = outcome.cost_usd if outcome.cost_usd is not None else research.per_forecast_usd
    condition_id = row["condition_id"]
    if outcome.error is not None:
        fail_attempt(conn, attempt_id, cost, outcome.error, now_fn())
        record_refusal(conn, run_id, condition_id, "research", outcome.error, outcome.detail,
                       now_fn())
        return None, outcome.error
    try:
        parsed = parse_output(outcome.structured_output, outcome.fetched_urls)
    except OutputError as error:
        fail_attempt(conn, attempt_id, cost, error.code, now_fn())
        record_refusal(conn, run_id, condition_id, "research", error.code, str(error), now_fn())
        return None, error.code
    now = now_fn()
    record = ForecastRecord(
        attempt_id=attempt_id,
        cohort_id=cohort_id,
        condition_id=row["condition_id"],
        rules_hash=row["rules_hash"],
        kind="entry",
        abstained=parsed.abstained,
        abstain_reason=parsed.abstain_reason,
        p_low=parsed.p_low,
        p_mid=parsed.p_mid,
        p_high=parsed.p_high,
        confidence=parsed.confidence,
        base_rate=parsed.base_rate,
        body={
            "evidence": [{"claim": claim, "url": url} for claim, url in parsed.evidence],
            "rules_interpretation": parsed.rules_interpretation,
            "exposure_flags": list(scan(_exposure_texts(outcome))),
        },
        research_input_hash=store_artifact(conn, "research_input", research_input(user_prompt),
                                           now),
        transcript_hash=store_artifact(
            conn, "tool_transcript", canonical_json(list(outcome.transcript)), now
        ),
        cost_usd=cost,
    )
    try:
        return record_forecast(conn, record, now), None
    except LedgerError:
        fail_attempt(conn, attempt_id, cost, "RECORD_FAILED", now_fn())
        return None, "RECORD_FAILED"


def resume_forecasts(
    conn: sqlite3.Connection,
    client: JsonClient,
    run_id: str,
    summary: ResearchSummary,
    now_fn: Callable[[], datetime],
) -> None:
    """Every cohort's forecasts still waiting for a baseline: take it while the window is
    open, otherwise record NO_TIMELY_BASELINE."""
    cohorts = [row["cohort_id"] for row in conn.execute("SELECT cohort_id FROM cohorts")]
    for cohort_id in cohorts:
        for forecast_id in unfinished_forecasts(conn, cohort_id):
            step = resume_step(conn, forecast_id, now_fn())
            if step is ResumeStep.NEEDS_BASELINE:
                summary.baselines += take_baseline(conn, client, forecast_id, run_id, now_fn)
            elif step is ResumeStep.BASELINE_EXPIRED:
                mark_no_timely_baseline(conn, forecast_id, now_fn())
                summary.no_timely_baseline += 1


def run_research_day(
    conn: sqlite3.Connection,
    client: JsonClient,
    runner: ResearchRunner,
    *,
    policy: PolicyParams,
    research: ResearchConfig,
    config_hash: str,
    code_version: str,
    now_fn: Callable[[], datetime],
) -> ResearchSummary:
    run_id = start_run(conn, "research", config_hash, now_fn())
    try:
        cohort_id = ensure_cohort(
            conn,
            cohort_identity(conn, policy, research, now_fn()),
            starting_bankroll=policy.starting_bankroll,
            code_version=code_version,
            now=now_fn(),
        )
        summary = ResearchSummary(cohort_id)
        summary.recovered = len(
            recover_interrupted_attempts(conn, research.per_forecast_usd, now_fn())
        )
        resume_forecasts(conn, client, run_id, summary, now_fn)
        summary.traded += trade_ready(conn, now_fn()).traded
        stop: str | None = None
        for row in candidates(conn, cohort_id, now_fn(), policy.min_hours_to_close):
            if stop is None:
                stop = budget_refusal(
                    day_usage(conn, now_fn(), research.per_forecast_usd),
                    daily_usd=research.daily_usd,
                    per_forecast_usd=research.per_forecast_usd,
                    max_entries=research.max_entry_forecasts_per_day,
                )
            if stop is not None:
                record_refusal(conn, run_id, row["condition_id"], "research", stop, "",
                               now_fn())
                summary.skipped[stop] += 1
                continue
            forecast_id, failure = research_market(conn, runner, cohort_id, row, research,
                                                   run_id, now_fn)
            if forecast_id is None:
                summary.failed[failure or "UNKNOWN"] += 1
                continue
            summary.forecasts += 1
            abstained = conn.execute(
                "SELECT abstained FROM forecasts WHERE forecast_id = ?", (forecast_id,)
            ).fetchone()[0]
            summary.abstentions += bool(abstained)
            if take_baseline(conn, client, forecast_id, run_id, now_fn):
                summary.baselines += 1
                summary.traded += trade_ready(conn, now_fn()).traded
    except BaseException:
        finish_run(conn, run_id, "FAILED", now_fn())
        raise
    finish_run(conn, run_id, "COMPLETED", now_fn())
    return summary
```

Apply to `src/predict_agent/cli.py` (`git apply` accepts this hunk as written):

```diff
diff --git a/src/predict_agent/cli.py b/src/predict_agent/cli.py
index a01628e..6431787 100644
--- a/src/predict_agent/cli.py
+++ b/src/predict_agent/cli.py
@@ -8,6 +8,7 @@ from collections.abc import Callable
 from datetime import datetime
 from pathlib import Path

+from .cohorts import code_version
 from .collect import (
     discover,
     finish_run,
@@ -25,7 +26,8 @@ from .paper import trade_ready
 from .policy_params import load_policy_config
 from .report import render_markdown, shortlist
 from .research.config import load_research_config
-from .research.sdk import ResearchUnavailable, load_sdk
+from .research.sdk import ResearchUnavailable, load_sdk, run_research
+from .research_run import ResearchRunner, ResearchSummary, run_research_day
 from .settlement import settle_open_tickets
 from .util import utc_now

@@ -41,6 +43,7 @@ def _parser() -> argparse.ArgumentParser:
     sub.add_parser("resolve", help="poll resolution state for known markets")
     sub.add_parser("settle", help="settle open paper tickets from stored resolutions (offline)")
     sub.add_parser("trade", help="decide forecasts with timely baselines; paper only (offline)")
+    sub.add_parser("research", help="forecast eligible markets with Claude (no prices), then trade")
     report = sub.add_parser("report", help="write the shortlist report")
     which = report.add_mutually_exclusive_group(required=True)
     which.add_argument("--run")
@@ -82,6 +85,7 @@ def main(
     client: JsonClient | None = None,
     root: Path | None = None,
     now_fn: Callable[[], datetime] = utc_now,
+    runner: ResearchRunner | None = None,
 ) -> int:
     args = _parser().parse_args(argv)
     settings = Settings.from_root((root or Path.cwd()).resolve())
@@ -142,6 +146,8 @@ def main(
             print(f"pending ticket {item.ticket_id} {item.condition_id}: {item.reason} ({since})")
         return 0
     http = client or JsonClient()
+    if args.command == "research":
+        return _research(settings, http, policy_hash, runner, now_fn)
     if args.command == "report":
         run_id = args.run or _latest_discovery_run(settings.database_path)
         if run_id is None:
@@ -172,6 +178,57 @@ def main(
     return code


+def _research(
+    settings: Settings,
+    http: JsonClient,
+    config_hash: str,
+    runner: ResearchRunner | None,
+    now_fn: Callable[[], datetime],
+) -> int:
+    try:
+        policy = load_policy_config(settings.policy_path)
+        research = load_research_config(settings.policy_path)
+    except ConfigError as error:
+        print(f"predict-agent: {error}", file=sys.stderr)
+        return 2
+    if runner is None:
+        try:
+            load_sdk()
+        except ResearchUnavailable as error:
+            print(f"predict-agent: {error}", file=sys.stderr)
+            return 2
+        runner = run_research
+    conn = connect(settings.database_path)
+    try:
+        summary = run_research_day(
+            conn,
+            http,
+            runner,
+            policy=policy,
+            research=research,
+            config_hash=config_hash,
+            code_version=code_version(settings.root),
+            now_fn=now_fn,
+        )
+    finally:
+        conn.close()
+    print(_research_line(summary))
+    return 0
+
+
+def _research_line(summary: ResearchSummary) -> str:
+    def counts(counter: dict[str, int]) -> str:
+        return ", ".join(f"{code} {n}" for code, n in sorted(counter.items())) or "none"
+
+    return (
+        f"cohort {summary.cohort_id[:12]}; recovered attempts {summary.recovered}; "
+        f"forecasts {summary.forecasts} (abstained {summary.abstentions}); "
+        f"failed: {counts(summary.failed)}; skipped: {counts(summary.skipped)}; "
+        f"baselines {summary.baselines}; no timely baseline {summary.no_timely_baseline}; "
+        f"traded {summary.traded}"
+    )
+
+
 def _run_steps(
     command: str,
     conn: sqlite3.Connection,
```

Apply to `CLAUDE.md` (`git apply` accepts this hunk as written):

```diff
diff --git a/CLAUDE.md b/CLAUDE.md
index 74430a6..108886e 100644
--- a/CLAUDE.md
+++ b/CLAUDE.md
@@ -33,7 +33,8 @@ Paper-first, human-approved investing agent. Flow: ingest (fail-closed snapshots
 - Spec: `docs/superpowers/specs/2026-10-05-polymarket-paper-forecaster-design.md`; plans in `docs/superpowers/plans/`.
 - Separate package: `predict_agent` must never import `japan_agent` (enforced by `tests/predict/test_cli_isolation.py`). `japan_agent` is frozen.
 - Phase 1 has **no execution code**: no wallet, keys, signing, or non-GET HTTP. Never attempt to circumvent Polymarket's UK geoblock; the geoblock check is audit-only.
-- CLI: `PYTHONPATH=src python3 -m predict_agent.cli doctor|discover|snapshot|resolve|trade|settle|report|run-data`. Needs `config/predict-policy.json` (human-owned copy of the `.example`; code never writes it).
+- CLI: `PYTHONPATH=src python3 -m predict_agent.cli doctor|discover|snapshot|resolve|research|trade|settle|report|run-data`. Needs `config/predict-policy.json` (human-owned copy of the `.example`; code never writes it).
 - Live API quirks pinned in `tests/predict/fixtures.py`: JSON-string list fields, worst-first books on both sides, resolution rows with or without `payouts` (micro-USDC when present), Gamma `/events` offset paging is capped (use `/events/keyset` + `after_cursor`), and the CLOB book `timestamp` behaved as a last-change time in live observation (not documented): store both server and fetch times and never refuse a book only because its timestamp is old. Gamma `/markets` needs repeated `condition_ids` params (comma-joined matches nothing) and returns closed markets only with `closed=true`.
 - Ledger (Plan 2): a cohort is one research identity (prompt, model, research settings, scoring version, baseline window, generation) owning the shared forecasts; each cohort has portfolios (`primary` plus pre-registered shadows like `shadow_mid`), each with its own frozen policy, FUNDING, cash, tickets and decisions. Money is Decimal text summed in Python (never SQL `SUM`); compare stored timestamps parsed, not as strings. Every cash entry must be backed (FUNDING = bankroll, DEBIT = its OPEN ticket's cost, CREDIT = its settlement payout). Write ledger state only through `cohorts`/`forecasts`/`tickets`/`settlement` functions — each commits with its journal entry. Settlement picks the governing resolution by observation time inside its transaction and waits on overlapping contradictory polls. `predict-agent settle` is offline and lists pending tickets with reason and age; `doctor` also runs `verify_ledger` (hashes, triggers and accounting relationships).
 - Paper policy (Plan 3): `policy.decide` is pure (no I/O, no clock). Decisions read the portfolio's frozen policy artifact (`policy_params.variant_policies`: `primary` = conservative bounds, `shadow_mid` = p_mid), never `config/predict-policy.json` directly; `doctor` still requires the config's `policy` section. Fills walk only the recorded book (per-level fees, shares rounded down to 0.01, limit price = best ask x (1 + max_slippage)); `paper.decide_portfolio` reads its inputs and writes the ticket/refusal in one transaction; `doctor` re-checks every ticket's fills against its snapshot and policy. `predict-agent trade` is offline. `discovery.max_book_age_seconds` and `policy.max_book_age_seconds` are separate settings; only the policy one governs trading decisions (it is frozen into the policy artifact).
+- Research (Plan 4): only `research/sdk.py` touches the Claude Agent SDK (lazy `importlib`; install the `predict` extra). The package `predict_agent.research` must never import policy, paper, money or market-data modules (isolation test) — the prompt never carries a price. Isolation fails closed: `tools=["WebSearch","WebFetch"]`, no settings/MCP/skills, empty cwd, verbatim prompts, a PreToolUse hook that forces `research.blocked_domains` onto every search, denies blocked/non-http fetches and every other tool, plus an init-report self-check (`EXPECTED_SESSION_TOOLS`). Citations must be URLs fetched in the session; exposure flags are best-effort ("no detected price exposure"). Every paid call is a `research_attempts` row; unknown or interrupted costs are charged at `per_forecast_usd`. `predict-agent research` forecasts, takes the post-forecast books immediately and trades; tests inject a fake runner (`tests/predict/fake_sdk.py` for the adapter) — never call the real SDK in tests.
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest tests.predict.test_research_run tests.predict.test_cli_isolation -v` → 27 tests OK.
Run: `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover` → all OK (the real-SDK name test skips unless the `predict` extra is installed).

- [ ] **Step 5: Lint, type-check, commit**

Run: `.venv/bin/ruff check src/predict_agent tests/predict && .venv/bin/mypy src/predict_agent && git diff --check`

```bash
git add src/predict_agent/collect.py src/predict_agent/research_run.py src/predict_agent/cli.py tests/predict/test_research_run.py tests/predict/test_cli_isolation.py CLAUDE.md
git commit -m "feat(predict): research run — forecast, immediate baseline, paper decision"
```

---

### Task 6: Live smoke run (human-approved; spends real money)

**Never run this task without the human's explicit go-ahead in the session.** It makes one real Claude research call (bounded at $1.00) and live read-only GETs to Polymarket's public APIs from the human's own connection. Agents executing this plan stop after Task 5 and ask.

**Files:** none committed, unless the run disproves `EXPECTED_SESSION_TOOLS` (then a one-line fix in `src/predict_agent/research/sdk.py` plus its test, as a separate reviewed change).

- [ ] **Step 1: Prepare an isolated root** (the human's real `data/` is never touched)

```bash
SMOKE=$(mktemp -d) && mkdir -p "$SMOKE/config"
python3 - "$SMOKE" <<'PY'
import json, sys, pathlib
root = pathlib.Path(sys.argv[1])
raw = json.loads(pathlib.Path("config/predict-policy.example.json").read_text())  # run from the repo root
raw["research"].update({"per_forecast_usd": "1.00", "daily_usd": "1.00", "max_entry_forecasts_per_day": 1})
(root / "config" / "predict-policy.json").write_text(json.dumps(raw, indent=2))
PY
pip install -e '.[predict]'
```

Credentials: `ANTHROPIC_API_KEY` in the environment, or a logged-in Claude Code profile (the bundled CLI uses the same resolution). Run `PYTHONPATH=src python3 -m unittest tests.predict.test_research_sdk -v` and confirm the real-SDK option-name test now runs and passes.

- [ ] **Step 2: Fresh discovery, then one research call**

The CLI uses its working directory as the root, so run from `$SMOKE` with the repo's `src` on the path (`<repo>` = your checkout):

```bash
cd "$SMOKE" && PYTHONPATH=<repo>/src python3 -m predict_agent.cli run-data
cd "$SMOKE" && PYTHONPATH=<repo>/src python3 -m predict_agent.cli research
```

- [ ] **Step 3: Check what happened** (read-only SQL on `$SMOKE/data/predict.sqlite3`)

```sql
SELECT status, cost_usd, error FROM research_attempts;                 -- exactly one row
SELECT reason_code, detail FROM refusals WHERE stage = 'research';     -- empty, or the failure detail
SELECT kind, COUNT(*) FROM artifacts GROUP BY kind;                    -- policy 2, prompt 1, research_input 1, tool_transcript 1
SELECT abstained, p_low, p_mid, p_high, json_extract(body_json, '$.exposure_flags') FROM forecasts;
```

Then confirm from the stored transcript (`artifacts` kind `tool_transcript`): every `WebSearch` `call` event's input carries the full `blocked_domains` list and no `allowed_domains`; no `result` event comes from a blocked domain; every evidence URL appears as a fetched `WebFetch` result. Run `PYTHONPATH=<repo>/src python3 -m predict_agent.cli doctor` from `$SMOKE` → exit 0.

- [ ] **Step 4: Record the outcome**

- If the attempt failed `TOOLSET_MISMATCH`, the `refusals.detail` names the tools the session reported. Update `EXPECTED_SESSION_TOOLS` only if the extra tool is the SDK's own structured-output mechanism under another name; any other tool is a real isolation failure — stop and report it.
- Record in the PR description: SDK and CLI versions, attempt status and cost, whether search results honored the blocked list, and the exposure flags. Delete `$SMOKE` afterwards (its database holds no secrets but is not needed).

---

## After this plan (human)

- `doctor` now requires a `research` section: copy it from `config/predict-policy.example.json` into `config/predict-policy.json` and review the model, budgets and blocked domains (code never writes that file).
- Install the extra to enable research: `pip install -e '.[predict]'`.
- Run order for a day (until Plan 5's `run-daily` wraps it with a lock): `run-data`, then `research`, later `resolve` and `settle`.

## Self-Review Record

1. **Spec coverage (§6, build-order item 4):** tool set exactly WebSearch + WebFetch with settings/MCP disabled and a deny-by-default hook + startup self-check (Task 3); domain enforcement on both tools (Task 3); exposure detection over every tool result incl. uncited snippets, stored per forecast, "no detected price exposure" wording (Tasks 2, 3, 5); strict JSON schema, invalid output → failed attempt with no retry, citations must be fetched URLs (Tasks 2, 5); forward-only (no backtest code); monetary budget daily + per-forecast from reported cost counting failed attempts, volume cap 15/day, `BUDGET` refusals (Tasks 4, 5); spec §3 ordering — forecast committed before the post-forecast book (Task 5 with Plan 2's `attach_baseline`); §7 resume (Task 5). Deferred: weekly updates and `run-daily` (Plan 5), report sections for flagged forecasts and research cost (Plan 5).
2. **Placeholder scan:** Task 6's `<repo>` is the human's checkout path, deliberately not hard-coded; nothing else.
3. **Type consistency:** `ResearchConfig`, `ResearchRequest`, `ResearchOutcome`, `ParsedForecast`, `DayUsage`, `ResearchSummary` match across tasks and fixtures; `ForecastRecord` and `TradeSummary` are Plan 2/3's.
4. **Task order:** each task's tests import only earlier tasks; verified by committing Tasks 1–5 in order on `9218529` with the full suite after each (367 → 374 → 389 → 393 → 410 tests, all OK; 1 real-SDK test skipped without the extra and passing with it).
5. **Mutation checks:** 16 safety mutations each made a test fail — search blocklist not forced, `allowed_domains` kept, fetch blocklist ignored, non-http fetch allowed, other tools allowed, self-check skipped, settings files loaded, `permission_mode` default, SDK error message leaked, citation check removed, unknown cost free, STARTED attempts free, budget not checked, transcript not scanned, research importing `policy`, baseline not taken after the forecast.

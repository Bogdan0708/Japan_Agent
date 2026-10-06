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
from .budget import INTERRUPTED, budget_refusal, day_usage, recover_interrupted_attempts
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

MAX_FAILED_ENTRY_ATTEMPTS = 2
MAX_DISCOVERY_AGE = timedelta(hours=24)
# Failures of the setup rather than of one market: no point burning an attempt on every
# remaining candidate (SDK_ERROR only when it repeats).
SYSTEMIC_FAILURES = frozenset({"TOOLSET_MISMATCH", "HOOK_ERROR"})
SDK_ERROR_STREAK = 2


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
    that do not close within the policy's minimum time, in condition-id order. A market
    with MAX_FAILED_ENTRY_ATTEMPTS or more FAILED entry attempts for this cohort is left
    out; attempts closed as INTERRUPTED (a crash, not a verdict on the market) do not count."""
    run_id = latest_discovery_run(conn)
    if run_id is None:
        return []
    rows = conn.execute(
        "SELECT d.condition_id, d.rules_hash, r.rules_json FROM discoveries d "
        "JOIN rules_versions r ON r.condition_id = d.condition_id "
        "AND r.rules_hash = d.rules_hash "
        "WHERE d.run_id = ? AND NOT EXISTS (SELECT 1 FROM forecasts f "
        "WHERE f.cohort_id = ? AND f.condition_id = d.condition_id AND f.kind = 'entry') "
        "AND (SELECT COUNT(*) FROM research_attempts a WHERE a.cohort_id = ? "
        "AND a.condition_id = d.condition_id AND a.kind = 'entry' AND a.status = 'FAILED' "
        "AND COALESCE(a.error, '') != ?) < ? "
        "ORDER BY d.condition_id",
        (run_id, cohort_id, cohort_id, INTERRUPTED, MAX_FAILED_ENTRY_ATTEMPTS),
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
            conn, run_id, market["condition_id"], "baseline", "BASELINE_REFUSED", str(error),
            now_fn(),
        )
        return False
    return True


def _string_leaves(value: object) -> list[str]:
    """Every string inside `value` as raw text (dict keys excluded). JSON-encoding the
    value first would escape non-ASCII characters and newlines and hide phrases."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _string_leaves(item)]
    if isinstance(value, list | tuple):
        return [leaf for item in value for leaf in _string_leaves(item)]
    return []


def _exposure_texts(outcome: ResearchOutcome) -> list[str]:
    """Everything the model saw: its own text, the output, every tool result and every
    tool error (the model reads a failed call's error text too). Denial reasons and tool
    inputs are the model's or the hook's own words, not something it saw."""
    texts = [outcome.final_text, *_string_leaves(outcome.structured_output)]
    for event in outcome.transcript:
        texts += _string_leaves(event.get("output")) + _string_leaves(event.get("error"))
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
    user_prompt = render_user_prompt(
        question=rules["question"],
        rules_text=rules["rules_text"],
        resolution_source=rules["resolution_source"],
        end_date=parse_datetime(rules["end_date"]),
        today=started,
    )
    attempt_id = start_attempt(conn, cohort_id, row["condition_id"], "entry", started)
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
    except LedgerError as error:
        try:
            fail_attempt(conn, attempt_id, cost, "RECORD_FAILED", now_fn())
        except ForecastError:
            # Another run recovered (closed) the attempt meanwhile: it is already charged.
            status = conn.execute(
                "SELECT status FROM research_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if status is None or status["status"] == "STARTED":
                raise
        record_refusal(conn, run_id, condition_id, "research", "RECORD_FAILED", str(error),
                       now_fn())
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
                if take_baseline(conn, client, forecast_id, run_id, now_fn):
                    summary.baselines += 1
                    summary.traded += trade_ready(conn, now_fn()).traded
            elif step is ResumeStep.BASELINE_EXPIRED:
                mark_no_timely_baseline(conn, forecast_id, now_fn())
                summary.no_timely_baseline += 1


def _discovery_is_stale(conn: sqlite3.Connection, now: datetime) -> bool:
    run_id = latest_discovery_run(conn)
    if run_id is None:
        return False
    started = conn.execute(
        "SELECT started_at FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()["started_at"]
    return now - parse_datetime(started) > MAX_DISCOVERY_AGE


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
        pending = candidates(conn, cohort_id, now_fn(), policy.min_hours_to_close)
        if _discovery_is_stale(conn, now_fn()):
            record_refusal(conn, run_id, None, "research", "STALE_DISCOVERY",
                           "the latest discovery run is older than 24 hours", now_fn())
            summary.skipped["STALE_DISCOVERY"] = 1
            pending = []
        sdk_errors = 0
        for index, row in enumerate(pending):
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
                code = failure or "UNKNOWN"
                summary.failed[code] += 1
                sdk_errors = sdk_errors + 1 if code == "SDK_ERROR" else 0
                if code in SYSTEMIC_FAILURES or sdk_errors >= SDK_ERROR_STREAK:
                    # Like BUDGET and VOLUME, every market left unresearched gets a
                    # durable refusal, not just a line in the summary.
                    for left in pending[index + 1:]:
                        record_refusal(conn, run_id, left["condition_id"], "research",
                                       "ABORTED_" + code, f"run aborted after {code}",
                                       now_fn())
                        summary.skipped["ABORTED_" + code] += 1
                    break
                continue
            sdk_errors = 0
            summary.forecasts += 1
            abstained = conn.execute(
                "SELECT abstained FROM forecasts WHERE forecast_id = ?", (forecast_id,)
            ).fetchone()[0]
            summary.abstentions += bool(abstained)
            if take_baseline(conn, client, forecast_id, run_id, now_fn):
                summary.baselines += 1
                summary.traded += trade_ready(conn, now_fn()).traded
        resume_forecasts(conn, client, run_id, summary, now_fn)
    except BaseException:
        finish_run(conn, run_id, "FAILED", now_fn())
        raise
    finish_run(conn, run_id, "COMPLETED", now_fn())
    return summary

"""The daily research run (spec §3 stages 2–5, §6, §7).

1. Open (or reuse) the cohort for the configured policy, prompt, model and research
   settings.
2. Close attempts an earlier run left STARTED, charging the full per-forecast cap.
3. Resume unfinished forecasts: take missing baselines while the window is open; once it
   has closed, attach the earliest pair of books already stored inside the window (a crash
   between storing the books and attaching them loses nothing), and mark the rest
   NO_TIMELY_BASELINE; decide whatever has a baseline.
4. Weekly update forecasts (spec §5), first, so new markets can never starve them: markets
   where the cohort holds an open ticket, once its newest attempt there is a week old;
   same prompt and checks, same daily budget (not the entry volume), a baseline for
   scoring, never a trade.
5. For each eligible market of the latest discovery run that this cohort has not forecast
   and whose resolution has not started, in order: check the day's budget and volume, open
   an attempt, run research (no price), validate the output, recheck that resolution has
   not started meanwhile, record the forecast (or fail the attempt with its cost), then
   fetch both books immediately as the post-forecast baseline and decide every portfolio.
   One failure streak spans steps 4 and 5: a systemic failure, or repeated SDK errors or
   timeouts, stops both, and every market left gets an ABORTED_<code> refusal.

The research runner is injected, so tests run with no network and no SDK."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from .artifacts import store_artifact
from .budget import INTERRUPTED, budget_refusal, day_usage, recover_interrupted_attempts
from .cash import LedgerError
from .cohorts import CohortIdentity, ensure_cohort
from .collect import finish_run, snapshot_book, start_run
from .db import record_refusal, transaction
from .forecasts import (
    ForecastError,
    ForecastRecord,
    ResumeStep,
    attach_baseline,
    baseline_deadline,
    fail_attempt,
    fail_attempt_locked,
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
from .resolution import OPEN_STATUSES
from .util import canonical_json, parse_datetime

ResearchRunner = Callable[[ResearchRequest], ResearchOutcome]

# A book fetch/parse failure right after a forecast: it cannot be scored or traded, and the
# next ones would fail the same way, so the run stops spending (see `baseline_outage`).
BASELINE_UNAVAILABLE = "BASELINE_UNAVAILABLE"
MAX_FAILED_ENTRY_ATTEMPTS = 2
MAX_DISCOVERY_AGE = timedelta(hours=24)
# Failures of the setup rather than of one market: no point burning an attempt on every
# remaining candidate (SDK_ERROR and TIMEOUT only when they repeat).
SYSTEMIC_FAILURES = frozenset({"TOOLSET_MISMATCH", "HOOK_ERROR"})
REPEATED_FAILURES = frozenset({"SDK_ERROR", "TIMEOUT"})
# Failures of the machinery rather than verdicts on one market: the run exits non-zero
# (run-daily reports the research step as failed). Schema problems, budget/turn limits,
# unfetched citations and markets that resolved meanwhile are expected per-market
# outcomes, like BUDGET and VOLUME refusals.
OPERATIONAL_FAILURES = SYSTEMIC_FAILURES | REPEATED_FAILURES | {
    "NO_RESULT", "RECORD_FAILED", BASELINE_UNAVAILABLE}
SDK_ERROR_STREAK = 2
# Consecutive failed sessions of any code (no success in between) that stop the run: a
# systemic problem disguised as per-market codes (e.g. a schema the model cannot meet).
ANY_FAILURE_STREAK = 3
# Failed sessions, with no forecast and no update in the run, that make it operational.
ALL_FAILED_THRESHOLD = 3
MARKET_RESOLVED = "MARKET_RESOLVED"
# Weekly update forecasts (spec §5): a market is researched again once the cohort's newest
# attempt there is this old.
UPDATE_INTERVAL = timedelta(days=7)
# The init report's model field is recorded, never enforced: its exact name and value are
# unverified until the Plan 4 live smoke run.
REPORTED_MODEL_NOTE = "model named by the session init report; unverified, not enforced"


@dataclass
class ResearchSummary:
    cohort_id: str
    recovered: int = 0
    forecasts: int = 0
    abstentions: int = 0
    baselines: int = 0
    no_timely_baseline: int = 0
    traded: int = 0
    updates: int = 0
    failed: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)

    def operational_failures(self) -> dict[str, int]:
        """Failures that mean the research machinery is broken (see OPERATIONAL_FAILURES);
        anything unrecognised counts too, failing toward attention."""
        known = OPERATIONAL_FAILURES | {"SCHEMA_INVALID", "UNFETCHED_CITATION", "MAX_BUDGET",
                                        "MAX_TURNS", MARKET_RESOLVED}
        broken = {code: n for code, n in sorted(self.failed.items())
                  if code in OPERATIONAL_FAILURES or code not in known}
        total = sum(self.failed.values())
        if total >= ALL_FAILED_THRESHOLD and self.forecasts == 0 and self.updates == 0:
            broken["ALL_SESSIONS_FAILED"] = total
        return broken


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


def resolution_started(
    conn: sqlite3.Connection, condition_id: str, as_of: datetime | None = None
) -> bool:
    """True when a stored resolution observation reports the market outside the open
    statuses (proposed, disputed, resolved...): the notion paper trading uses to refuse a
    trade. With `as_of`, only observations fetched at or before it count."""
    for row in conn.execute(
        "SELECT status, resolution_fetched_at FROM resolution_observations "
        "WHERE condition_id = ?",
        (condition_id,),
    ):
        if row["status"] in OPEN_STATUSES:
            continue
        if as_of is None or parse_datetime(row["resolution_fetched_at"]) <= as_of:
            return True
    return False


def candidates(
    conn: sqlite3.Connection, cohort_id: str, now: datetime, min_hours_to_close: int
) -> list[sqlite3.Row]:
    """Markets of the latest completed discovery run that this cohort has not forecast,
    whose resolution has not started (any stored observation, see `resolution_started`)
    and that do not close within the policy's minimum time, in condition-id order. A market
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
        if resolution_started(conn, row["condition_id"]):
            continue
        end_date = json.loads(row["rules_json"]).get("end_date")
        if end_date and parse_datetime(end_date) >= horizon:
            selected.append(row)
    return selected


def due_updates(
    conn: sqlite3.Connection, cohort_id: str, now: datetime, min_hours_to_close: int
) -> list[sqlite3.Row]:
    """Markets due a weekly update forecast for this cohort: it holds an OPEN ticket there
    in any portfolio, the market's resolution has not started, it is still more than
    `min_hours_to_close` from its end date (the horizon entries use: after it a session
    could look up the outcome; a current rules version with no end date fails closed and
    is skipped), and the cohort's newest research attempt on it (entry or update, whatever
    its outcome) started at least UPDATE_INTERVAL ago. Oldest first. Rows carry the
    market's current rules version."""
    rows = conn.execute(
        "SELECT m.condition_id, m.current_rules_hash AS rules_hash, r.rules_json "
        "FROM markets m JOIN rules_versions r ON r.condition_id = m.condition_id "
        "AND r.rules_hash = m.current_rules_hash "
        "WHERE EXISTS (SELECT 1 FROM paper_tickets t JOIN portfolios p "
        "ON p.portfolio_id = t.portfolio_id WHERE p.cohort_id = ? "
        "AND t.condition_id = m.condition_id AND t.status = 'OPEN') "
        "ORDER BY m.condition_id",
        (cohort_id,),
    ).fetchall()
    due: list[tuple[datetime, sqlite3.Row]] = []
    horizon = timedelta(hours=min_hours_to_close)
    for row in rows:
        if resolution_started(conn, row["condition_id"]):
            continue
        end_date = json.loads(row["rules_json"]).get("end_date")
        if not end_date or now >= parse_datetime(end_date) - horizon:
            continue
        started = [
            parse_datetime(a["started_at"])
            for a in conn.execute(
                "SELECT started_at FROM research_attempts WHERE cohort_id = ? "
                "AND condition_id = ?",
                (cohort_id, row["condition_id"]),
            )
        ]
        last = max(started) if started else None
        if last is None or now - last >= UPDATE_INTERVAL:
            due.append((last or now, row))
    due.sort(key=lambda item: (item[0], item[1]["condition_id"]))
    return [row for _, row in due]


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


def baseline_or_outage(
    conn: sqlite3.Connection,
    client: JsonClient,
    forecast_id: int,
    run_id: str,
    now_fn: Callable[[], datetime],
) -> tuple[bool, bool]:
    """(baseline taken, book outage). The outage flag is set when `take_baseline` failed
    and this call recorded a FETCH_ERROR or PARSE_ERROR snapshot refusal for the market
    (thin or crossed books and refused baselines are market outcomes, not outages)."""
    last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM refusals").fetchone()[0]
    if take_baseline(conn, client, forecast_id, run_id, now_fn):
        return True, False
    outage = conn.execute(
        "SELECT 1 FROM refusals r JOIN forecasts f ON f.condition_id = r.condition_id "
        "WHERE f.forecast_id = ? AND r.id > ? AND r.run_id = ? AND r.stage = 'snapshot' "
        "AND r.reason_code IN ('FETCH_ERROR', 'PARSE_ERROR') LIMIT 1",
        (forecast_id, last, run_id),
    ).fetchone()
    return False, outage is not None


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


def _exposure_texts(outcome: ResearchOutcome, user_prompt: str) -> list[str]:
    """Everything the model saw: the rendered user prompt (market descriptions can quote
    prices), its own text, the output, every tool result and every tool error (the model
    reads a failed call's error text too). The fixed SYSTEM_PROMPT is left out: it names
    prediction markets and odds only to forbid them. Denial reasons and tool inputs are the
    model's or the hook's own words, not something it saw."""
    texts = [user_prompt, outcome.final_text, *_string_leaves(outcome.structured_output)]
    for event in outcome.transcript:
        texts += _string_leaves(event.get("output")) + _string_leaves(event.get("error"))
    return texts


def _transcript_content(research: ResearchConfig, outcome: ResearchOutcome) -> str:
    return canonical_json([
        {
            "event": "session",
            "requested_model": research.model,
            "reported_model": outcome.reported_model,
            "note": REPORTED_MODEL_NOTE,
        },
        *outcome.transcript,
    ])


def _refuse_with_transcript(
    conn: sqlite3.Connection,
    run_id: str,
    condition_id: str,
    attempt_id: int,
    cost: Decimal,
    failed_at: datetime,
    code: str,
    detail: str,
    research: ResearchConfig,
    outcome: ResearchOutcome,
    now_fn: Callable[[], datetime],
) -> None:
    """Close the attempt as failed, record the research refusal and keep the session's
    transcript so the failure can be audited. All three commit together (if any write
    fails, the attempt stays STARTED for recovery); the detail ends with the hash."""
    with transaction(conn):
        fail_attempt_locked(conn, attempt_id, cost, code, failed_at)
        digest = store_artifact(conn, "tool_transcript", _transcript_content(research, outcome),
                                now_fn())
        record_refusal(conn, run_id, condition_id, "research", code,
                       f"{detail} [transcript {digest}]", now_fn())


def research_market(
    conn: sqlite3.Connection,
    runner: ResearchRunner,
    cohort_id: str,
    row: sqlite3.Row,
    research: ResearchConfig,
    run_id: str,
    now_fn: Callable[[], datetime],
    kind: str = "entry",
) -> tuple[int | None, str | None]:
    """(forecast_id, None) on success, (None, failure code) otherwise. `kind` is `entry`
    or `update` (same prompt and checks; only entries trade). The attempt is
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
    attempt_id = start_attempt(conn, cohort_id, row["condition_id"], kind, started)
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
        _refuse_with_transcript(conn, run_id, condition_id, attempt_id, cost, now_fn(),
                                outcome.error, outcome.detail, research, outcome, now_fn)
        return None, outcome.error
    checked = now_fn()
    if resolution_started(conn, condition_id, as_of=checked):
        # Resolution started while the session ran: a forecast now could see the answer.
        _refuse_with_transcript(conn, run_id, condition_id, attempt_id, cost, checked,
                                MARKET_RESOLVED,
                                "a resolution observation arrived during research", research,
                                outcome, now_fn)
        return None, MARKET_RESOLVED
    try:
        parsed = parse_output(outcome.structured_output, outcome.fetched_urls)
    except OutputError as error:
        _refuse_with_transcript(conn, run_id, condition_id, attempt_id, cost, now_fn(),
                                error.code, str(error), research, outcome, now_fn)
        return None, error.code
    now = now_fn()
    record = ForecastRecord(
        attempt_id=attempt_id,
        cohort_id=cohort_id,
        condition_id=row["condition_id"],
        rules_hash=row["rules_hash"],
        kind=kind,
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
            "exposure_flags": list(scan(_exposure_texts(outcome, user_prompt))),
        },
        research_input_hash=store_artifact(conn, "research_input", research_input(user_prompt),
                                           now),
        transcript_hash=store_artifact(conn, "tool_transcript",
                                       _transcript_content(research, outcome), now),
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


def stored_baseline(conn: sqlite3.Connection, forecast_id: int) -> tuple[int, int] | None:
    """The earliest stored YES and NO snapshots of the forecast's market fetched inside its
    baseline window (from the forecast commit to the deadline), whichever run stored them:
    `attach_baseline` ties a baseline to the market, outcome and fetch time only."""
    forecast = conn.execute(
        "SELECT f.condition_id, f.created_at, c.baseline_window_seconds FROM forecasts f "
        "JOIN cohorts c ON c.cohort_id = f.cohort_id WHERE f.forecast_id = ?",
        (forecast_id,),
    ).fetchone()
    opened = parse_datetime(forecast["created_at"])
    deadline = baseline_deadline(forecast)
    earliest: dict[str, tuple[datetime, int]] = {}
    for row in conn.execute(
        "SELECT id, outcome, fetched_at FROM book_snapshots WHERE condition_id = ?",
        (forecast["condition_id"],),
    ):
        fetched = parse_datetime(row["fetched_at"])
        if not opened <= fetched <= deadline:
            continue
        key = (fetched, row["id"])
        if row["outcome"] not in earliest or key < earliest[row["outcome"]]:
            earliest[row["outcome"]] = key
    if "YES" not in earliest or "NO" not in earliest:
        return None
    return earliest["YES"][1], earliest["NO"][1]


def resume_forecasts(
    conn: sqlite3.Connection,
    client: JsonClient,
    run_id: str,
    summary: ResearchSummary,
    now_fn: Callable[[], datetime],
) -> None:
    """Every cohort's forecasts still waiting for a baseline: take it while the window is
    open; once it has closed, attach books already stored inside the window, otherwise
    record NO_TIMELY_BASELINE."""
    cohorts = [row["cohort_id"] for row in conn.execute("SELECT cohort_id FROM cohorts")]
    for cohort_id in cohorts:
        for forecast_id in unfinished_forecasts(conn, cohort_id):
            step = resume_step(conn, forecast_id, now_fn())
            if step is ResumeStep.NEEDS_BASELINE:
                if take_baseline(conn, client, forecast_id, run_id, now_fn):
                    summary.baselines += 1
                    summary.traded += trade_ready(conn, now_fn).traded
            elif step is ResumeStep.BASELINE_EXPIRED:
                pair = stored_baseline(conn, forecast_id)
                if pair is not None:
                    try:
                        attach_baseline(conn, forecast_id, pair[0], pair[1], now_fn())
                    except ForecastError as error:
                        condition_id = conn.execute(
                            "SELECT condition_id FROM forecasts WHERE forecast_id = ?",
                            (forecast_id,),
                        ).fetchone()["condition_id"]
                        record_refusal(conn, run_id, condition_id, "baseline",
                                       "BASELINE_REFUSED", str(error), now_fn())
                    else:
                        summary.baselines += 1
                        summary.traded += trade_ready(conn, now_fn).traded
                        continue
                mark_no_timely_baseline(conn, forecast_id, now_fn())
                summary.no_timely_baseline += 1


@dataclass
class FailureStreak:
    """Consecutive failures across the whole run (updates and entries alike): SDK
    errors/timeouts, and failures of any code."""

    count: int = 0
    any_count: int = 0

    def failed(self, code: str) -> bool:
        """Count one failure; True when it should stop the run (a systemic failure, the
        SDK_ERROR_STREAK-th repeated SDK error/timeout in a row, or the
        ANY_FAILURE_STREAK-th failed session of any code in a row)."""
        self.count = self.count + 1 if code in REPEATED_FAILURES else 0
        self.any_count += 1
        return (code in SYSTEMIC_FAILURES or self.count >= SDK_ERROR_STREAK
                or self.any_count >= ANY_FAILURE_STREAK)

    def succeeded(self) -> None:
        self.count = 0
        self.any_count = 0


def _abort(
    conn: sqlite3.Connection,
    run_id: str,
    condition_id: str,
    stage: str,
    code: str,
    summary: ResearchSummary,
    now_fn: Callable[[], datetime],
) -> None:
    """Like BUDGET and VOLUME, every market left unresearched after an abort gets a durable
    refusal, not just a line in the summary."""
    record_refusal(conn, run_id, condition_id, stage, "ABORTED_" + code,
                   f"run aborted after {code}", now_fn())
    summary.skipped["ABORTED_" + code] += 1


def run_updates(
    conn: sqlite3.Connection,
    client: JsonClient,
    runner: ResearchRunner,
    cohort_id: str,
    research: ResearchConfig,
    run_id: str,
    summary: ResearchSummary,
    streak: FailureStreak,
    now_fn: Callable[[], datetime],
    min_hours_to_close: int,
) -> str | None:
    """Research every due update forecast, before the day's new entries so a backlog of
    new markets can never starve them (updates are few: one per open-ticket market per
    week; none once a market is within `min_hours_to_close` of its end date). Updates
    spend the same daily budget (a refused one is recorded as BUDGET at
    stage `update`) but not the entry volume; each takes a baseline for scoring and never
    trades. Returns the failure code that stopped the run, or None."""
    due = due_updates(conn, cohort_id, now_fn(), min_hours_to_close)
    for index, row in enumerate(due):
        usage = day_usage(conn, now_fn(), research.per_forecast_usd)
        if usage.spent_usd + research.per_forecast_usd > research.daily_usd:
            record_refusal(conn, run_id, row["condition_id"], "update", "BUDGET", "", now_fn())
            summary.skipped["UPDATE_BUDGET"] += 1
            continue
        forecast_id, failure = research_market(conn, runner, cohort_id, row, research,
                                               run_id, now_fn, kind="update")
        if forecast_id is None:
            code = failure or "UNKNOWN"
            summary.failed[code] += 1
            if streak.failed(code):
                for left in due[index + 1:]:
                    _abort(conn, run_id, left["condition_id"], "update", code, summary,
                           now_fn)
                return code
            continue
        streak.succeeded()
        summary.updates += 1
        _, outage = baseline_or_outage(conn, client, forecast_id, run_id, now_fn)
        if outage:
            summary.failed[BASELINE_UNAVAILABLE] += 1
            for left in due[index + 1:]:
                _abort(conn, run_id, left["condition_id"], "update", BASELINE_UNAVAILABLE,
                       summary, now_fn)
            return BASELINE_UNAVAILABLE
    return None


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
        summary.traded += trade_ready(conn, now_fn).traded
        stop: str | None = None
        pending = candidates(conn, cohort_id, now_fn(), policy.min_hours_to_close)
        aborted: str | None = None
        streak = FailureStreak()
        if _discovery_is_stale(conn, now_fn()):
            record_refusal(conn, run_id, None, "research", "STALE_DISCOVERY",
                           "the latest discovery run is older than 24 hours", now_fn())
            summary.skipped["STALE_DISCOVERY"] = 1
            pending = []
        else:
            aborted = run_updates(conn, client, runner, cohort_id, research, run_id, summary,
                                  streak, now_fn, policy.min_hours_to_close)
        for row in pending:
            if aborted is not None:
                _abort(conn, run_id, row["condition_id"], "research", aborted, summary,
                       now_fn)
                continue
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
                if streak.failed(code):
                    aborted = code
                continue
            streak.succeeded()
            summary.forecasts += 1
            abstained = conn.execute(
                "SELECT abstained FROM forecasts WHERE forecast_id = ?", (forecast_id,)
            ).fetchone()[0]
            summary.abstentions += bool(abstained)
            taken, outage = baseline_or_outage(conn, client, forecast_id, run_id, now_fn)
            if taken:
                summary.baselines += 1
                summary.traded += trade_ready(conn, now_fn).traded
            elif outage:
                summary.failed[BASELINE_UNAVAILABLE] += 1
                aborted = BASELINE_UNAVAILABLE
        resume_forecasts(conn, client, run_id, summary, now_fn)
    except BaseException:
        finish_run(conn, run_id, "FAILED", now_fn())
        raise
    finish_run(conn, run_id, "COMPLETED", now_fn())
    return summary

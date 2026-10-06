from __future__ import annotations

import contextlib
import fcntl
import os
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

from predict_agent.cash import LedgerError
from predict_agent.cli import main
from predict_agent.cohorts import cohort_id_for
from predict_agent.db import connect
from predict_agent.budget import INTERRUPTED
from predict_agent.forecasts import fail_attempt, start_attempt
from predict_agent.http import JsonClient
from predict_agent.invariants import verify_ledger
from predict_agent.policy_params import parse_policy
from predict_agent.research.config import parse_research
from predict_agent.research.sdk import ResearchOutcome, ResearchRequest
from predict_agent.research_run import (
    MAX_FAILED_ENTRY_ATTEMPTS,
    ResearchSummary,
    candidates,
    cohort_identity,
    run_research_day,
)
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

    def test_exposure_scan_reads_raw_text_not_json_escaped_text(self) -> None:
        snippet = {"event": "result", "tool": "WebSearch", "input": {"query": "q"},
                   "output": {"content": "Shares trade at 35\u00a2 a share; bookmakers\u2019 odds\n"
                              "of a win"}}
        runner = FakeRunner(outcome(transcript=outcome().transcript + (snippet,)))
        self.run_day(runner)
        flags = json.loads(self.scalar("SELECT body_json FROM forecasts"))["exposure_flags"]
        self.assertIn("phrase:cents_per_share", flags)
        self.assertIn("phrase:odds_of", flags)

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


class SystemicFailureTests(ResearchRunTestCase):
    def test_toolset_mismatch_stops_the_run_after_one_attempt(self) -> None:
        self.add_markets(*OTHER[:2])
        bad = outcome(error="TOOLSET_MISMATCH", cost_usd=None, structured_output=None)
        runner = FakeRunner(bad, bad, bad)
        summary = self.run_day(runner, book_queue=[])
        self.assertEqual(len(runner.requests), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM research_attempts"), 1)
        self.assertEqual(dict(summary.skipped), {"ABORTED_TOOLSET_MISMATCH": 2})
        self.assertEqual(dict(summary.failed), {"TOOLSET_MISMATCH": 1})

    def test_hook_error_stops_the_run(self) -> None:
        self.add_markets(*OTHER[:2])
        bad = outcome(error="HOOK_ERROR", cost_usd=None, structured_output=None)
        runner = FakeRunner(bad, bad, bad)
        summary = self.run_day(runner, book_queue=[])
        self.assertEqual(len(runner.requests), 1)
        self.assertEqual(dict(summary.skipped), {"ABORTED_HOOK_ERROR": 2})

    def test_two_consecutive_sdk_errors_stop_the_run(self) -> None:
        self.add_markets(*OTHER[:3])
        bad = outcome(error="SDK_ERROR", cost_usd=None, structured_output=None)
        runner = FakeRunner(bad, bad, bad, bad)
        summary = self.run_day(runner, book_queue=[])
        self.assertEqual(len(runner.requests), 2)
        self.assertEqual(dict(summary.skipped), {"ABORTED_SDK_ERROR": 2})
        self.assertEqual(dict(summary.failed), {"SDK_ERROR": 2})

    def test_a_single_sdk_error_between_successes_does_not_stop_the_run(self) -> None:
        self.add_markets(*OTHER[:2])
        bad = outcome(error="SDK_ERROR", cost_usd=None, structured_output=None)
        runner = FakeRunner(outcome(), bad, outcome())
        summary = self.run_day(runner, book_queue=books(self.markets[0]) + books(self.markets[2]))
        self.assertEqual(len(runner.requests), 3)
        self.assertEqual(dict(summary.skipped), {})


class RetryLimitTests(ResearchRunTestCase):
    def failed_attempts(self, cohort: str, condition_id: str, *errors: str) -> None:
        for index, error in enumerate(errors):
            at = NOW + timedelta(minutes=index + 1)
            attempt = start_attempt(self.conn, cohort, condition_id, "entry", at)
            fail_attempt(self.conn, attempt, Decimal("1.00"), error, at)

    def test_markets_with_repeated_failures_are_not_candidates(self) -> None:
        self.assertEqual(MAX_FAILED_ENTRY_ATTEMPTS, 2)
        cohort = self.run_day(FakeRunner(), book_queue=books()).cohort_id
        self.add_markets(*OTHER[:3])
        self.failed_attempts(cohort, OTHER[0], "SDK_ERROR", "NO_RESULT")
        self.failed_attempts(cohort, OTHER[1], "SDK_ERROR", INTERRUPTED)
        self.failed_attempts(cohort, OTHER[2], INTERRUPTED, INTERRUPTED, "SDK_ERROR")
        rows = candidates(self.conn, cohort, NOW + timedelta(hours=2), 48)
        self.assertEqual([r["condition_id"] for r in rows], [OTHER[1], OTHER[2]])


class StaleDiscoveryTests(ResearchRunTestCase):
    def test_discovery_older_than_24_hours_researches_nothing(self) -> None:
        runner = FakeRunner()
        summary = self.run_day(runner, book_queue=[], start=NOW + timedelta(hours=24))
        self.assertEqual(len(runner.requests), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM research_attempts"), 0)
        self.assertEqual(dict(summary.skipped), {"STALE_DISCOVERY": 1})
        refusal = self.conn.execute(
            "SELECT condition_id, stage FROM refusals WHERE reason_code = 'STALE_DISCOVERY'"
        ).fetchall()
        self.assertEqual([tuple(r) for r in refusal], [(None, "research")])

    def test_discovery_within_24_hours_is_researched(self) -> None:
        runner = FakeRunner()
        summary = self.run_day(runner, book_queue=books(), start=NOW + timedelta(hours=22))
        self.assertEqual((len(runner.requests), summary.forecasts), (1, 1))
        self.assertEqual(dict(summary.skipped), {})

    def test_stale_discovery_still_resumes_baselines(self) -> None:
        self.run_day(FakeRunner(), book_queue=books(crossed=True) + books(crossed=True))
        later = self.run_day(FakeRunner(), book_queue=[], start=NOW + timedelta(hours=30))
        self.assertEqual(later.no_timely_baseline, 1)
        self.assertEqual(dict(later.skipped), {"STALE_DISCOVERY": 1})


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
        # Crossed pair (refused) + good pair (for retry)
        queue = books(crossed=True) + books()
        first = self.run_day(FakeRunner(), book_queue=queue)
        # Baseline taken in retry, 2 trades (primary + shadow_mid portfolio)
        self.assertEqual((first.forecasts, first.baselines, first.traded), (1, 1, 2))
        second = self.run_day(FakeRunner(), book_queue=books(),
                              start=NOW + timedelta(minutes=10))
        self.assertEqual((second.baselines, second.traded), (0, 0))

    def test_expired_baseline_is_marked_no_timely_baseline(self) -> None:
        # Crossed pair (refused in immediate baseline) + crossed pair (refused in retry)
        self.run_day(FakeRunner(), book_queue=books(crossed=True) + books(crossed=True))
        later = self.run_day(FakeRunner(), book_queue=[], start=NOW + timedelta(minutes=45))
        self.assertEqual(later.no_timely_baseline, 1)
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM decisions")]
        self.assertEqual(kinds, ["NO_TIMELY_BASELINE", "NO_TIMELY_BASELINE"])


class CrashPathTests(ResearchRunTestCase):
    def test_runner_crash_leaves_the_attempt_for_recovery(self) -> None:
        def crashing_runner(_: Any) -> Any:
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.run_day(crashing_runner)  # type: ignore
        # Get the LATEST run (not the seed run from setup)
        run_status = self.scalar("SELECT status FROM runs WHERE command = 'research'")
        self.assertEqual(run_status, "FAILED")
        attempt_status = self.scalar("SELECT status FROM research_attempts")
        self.assertEqual(attempt_status, "STARTED")
        # Second run recovers it
        summary = self.run_day(FakeRunner(), book_queue=books(), start=NOW + timedelta(hours=1))
        self.assertEqual(summary.recovered, 1)

    def test_failed_immediate_baseline_is_retried_in_the_same_run(self) -> None:
        # First pair is crossed (refused), second pair is good
        crossed_pair = books(crossed=True)
        good_pair = books()
        queue = crossed_pair + good_pair
        summary = self.run_day(FakeRunner(), book_queue=queue)
        self.assertEqual((summary.forecasts, summary.baselines, summary.traded), (1, 1, 2))

    def test_record_failure_closes_the_attempt_with_detail(self) -> None:
        runner = FakeRunner()
        with mock.patch("predict_agent.research_run.record_forecast",
                       side_effect=LedgerError("cohort closed")):
            summary = self.run_day(runner, book_queue=[])
        self.assertEqual(dict(summary.failed), {"RECORD_FAILED": 1})
        attempt = self.conn.execute("SELECT status FROM research_attempts").fetchone()
        self.assertEqual(attempt[0], "FAILED")
        refusal = self.conn.execute(
            "SELECT reason_code, detail FROM refusals WHERE stage = 'research'").fetchone()
        self.assertEqual(refusal[0], "RECORD_FAILED")
        self.assertIn("cohort closed", refusal[1])


class CohortIdentityTests(ResearchRunTestCase):
    def cohort(self, research: Any) -> str:
        return cohort_id_for(cohort_identity(self.conn, POLICY, research, NOW))

    def test_per_forecast_budget_is_identity_but_daily_budget_is_not(self) -> None:
        base = self.cohort(RESEARCH)
        self.assertNotEqual(
            self.cohort(replace(RESEARCH, per_forecast_usd=Decimal("2.00"))), base)
        self.assertEqual(self.cohort(replace(RESEARCH, daily_usd=Decimal("50.00"))), base)


class OverlapTests(ResearchRunTestCase):
    def test_record_failure_survives_an_attempt_recovered_by_another_run(self) -> None:
        def recovered_then_failing(conn: Any, record: Any, now: Any) -> int:
            fail_attempt(conn, record.attempt_id, Decimal("3.00"), INTERRUPTED, now)
            raise LedgerError("cohort closed")

        with mock.patch("predict_agent.research_run.record_forecast",
                        side_effect=recovered_then_failing):
            summary = self.run_day(FakeRunner(), book_queue=[])
        self.assertEqual(dict(summary.failed), {"RECORD_FAILED": 1})
        refusal = self.conn.execute(
            "SELECT reason_code, detail FROM refusals WHERE stage = 'research'").fetchone()
        self.assertEqual(refusal[0], "RECORD_FAILED")
        self.assertIn("cohort closed", refusal[1])
        self.assertEqual(self.scalar("SELECT error FROM research_attempts"), INTERRUPTED)


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

    def test_a_second_research_run_is_refused_while_the_lock_is_held(self) -> None:
        lock_path = self.root / "data" / "predict.sqlite3.research.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            runner = FakeRunner()
            code, output = self.cli(runner, books())
            self.assertEqual(code, 2)
            self.assertIn("predict-agent: another research run is in progress", output)
            self.assertEqual(runner.requests, [])
        finally:
            os.close(fd)
        code, output = self.cli(FakeRunner(), books())
        self.assertEqual(code, 0, output)

    def test_research_command_without_the_sdk_fails_closed(self) -> None:
        with mock.patch.dict(sys.modules, {"claude_agent_sdk": None}):
            code, output = self.cli(None, [])
        self.assertEqual(code, 2)
        self.assertIn("pip install", output)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM research_attempts"), 0)


if __name__ == "__main__":
    unittest.main()

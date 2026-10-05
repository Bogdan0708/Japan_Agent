from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from predict_agent.cohorts import cohort_portfolios
from predict_agent.db import connect
from predict_agent.forecasts import (
    ForecastError,
    ResumeStep,
    attach_baseline,
    fail_attempt,
    mark_no_timely_baseline,
    record_forecast,
    resume_step,
    start_attempt,
    unfinished_attempts,
    unfinished_forecasts,
    validate_forecast,
)
from predict_agent.util import isoformat
from tests.predict.fixtures import CONDITION_ID, NOW
from tests.predict.ledger_fixtures import (
    forecast_record,
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_snapshot,
)


class ForecastTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.rules_hash = seed_market(self.conn)
        self.cohort = seed_cohort(self.conn, shadow=True)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def books(self, at_offset: timedelta) -> tuple[int, int]:
        return (
            seed_snapshot(self.conn, "YES", NOW + at_offset),
            seed_snapshot(self.conn, "NO", NOW + at_offset),
        )


class AttemptTests(ForecastTestCase):
    def test_forecast_closes_its_attempt_with_its_cost(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        attempt = self.conn.execute(
            "SELECT a.status, a.cost_usd FROM research_attempts a "
            "JOIN forecasts f ON f.attempt_id = a.attempt_id WHERE f.forecast_id = ?",
            (forecast_id,),
        ).fetchone()
        self.assertEqual(tuple(attempt), ("SUCCEEDED", "0.12"))
        self.assertEqual(unfinished_attempts(self.conn, self.cohort), [])

    def test_failed_attempt_keeps_its_cost_and_cannot_be_reused(self) -> None:
        attempt = start_attempt(self.conn, self.cohort, CONDITION_ID, "entry", NOW)
        self.assertEqual(unfinished_attempts(self.conn, self.cohort), [attempt])
        fail_attempt(self.conn, attempt, Decimal("0.07"), "schema rejected", NOW)
        row = self.conn.execute("SELECT status, cost_usd, error FROM research_attempts").fetchone()
        self.assertEqual(tuple(row), ("FAILED", "0.07", "schema rejected"))
        with self.assertRaisesRegex(ForecastError, "not a STARTED attempt"):
            seed_entry_forecast(self.conn, self.cohort, self.rules_hash, attempt_id=attempt)
        with self.assertRaisesRegex(ForecastError, "not STARTED"):
            fail_attempt(self.conn, attempt, Decimal("0"), "again", NOW)

    def test_attempt_must_match_the_forecast(self) -> None:
        attempt = start_attempt(self.conn, self.cohort, CONDITION_ID, "update", NOW)
        with self.assertRaisesRegex(ForecastError, "not a STARTED attempt"):
            seed_entry_forecast(self.conn, self.cohort, self.rules_hash, attempt_id=attempt)

    def test_attempts_only_finish_once(self) -> None:
        seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE research_attempts SET cost_usd = '0'")


class RecordTests(ForecastTestCase):
    def test_records_entry_forecast_and_journals(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        row = self.conn.execute(
            "SELECT p_mid, base_rate, cost_usd, kind FROM forecasts WHERE forecast_id = ?",
            (forecast_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("0.60", "0.30", "0.12", "entry"))
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM journal ORDER BY seq")]
        self.assertEqual(kinds[-1], "FORECAST_RECORDED")

    def test_validation_rejects_bad_records(self) -> None:
        base = forecast_record(self.conn, self.cohort, self.rules_hash)
        bad = {
            "p_high above 0.99": replace(base, p_high=Decimal("0.995")),
            "unordered range": replace(base, p_low=Decimal("0.70")),
            "abstained with p": replace(base, abstained=True, abstain_reason="ambiguous"),
            "abstained without reason": replace(
                base, abstained=True, p_low=None, p_mid=None, p_high=None, confidence=None
            ),
            "missing confidence": replace(base, confidence=None),
            "unknown confidence": replace(base, confidence="certain"),
            "missing base rate": replace(base, base_rate=None),
            "base rate above 1": replace(base, base_rate=Decimal("1.5")),
            "nan cost": replace(base, cost_usd=Decimal("NaN")),
            "unknown kind": replace(base, kind="draft"),
            "bad hash": replace(base, transcript_hash="xyz"),
            "empty body": replace(base, body={}),
            "evidence not a list": replace(
                base,
                body={"evidence": "x", "rules_interpretation": "r", "exposure_flags": []},
            ),
        }
        for label, record in bad.items():
            with self.subTest(label), self.assertRaises(ForecastError):
                validate_forecast(record)

    def test_abstention_is_valid_without_base_rate(self) -> None:
        abstained = forecast_record(
            self.conn,
            self.cohort,
            self.rules_hash,
            abstained=True,
            abstain_reason="rules ambiguous",
            p_low=None,
            p_mid=None,
            p_high=None,
            confidence=None,
            base_rate=None,
        )
        validate_forecast(abstained)
        self.assertGreater(record_forecast(self.conn, abstained, NOW), 0)

    def test_one_entry_forecast_per_market_but_many_updates(self) -> None:
        seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        with self.assertRaisesRegex(ForecastError, "entry forecast already exists"):
            seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        for minutes in (1, 2):
            seed_entry_forecast(
                self.conn,
                self.cohort,
                self.rules_hash,
                kind="update",
                at=NOW + timedelta(minutes=minutes),
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0], 3)

    def test_closed_cohort_and_unknown_rules_refused(self) -> None:
        seed_cohort(self.conn, model_id="m2")
        with self.assertRaisesRegex(ForecastError, "not active"):
            seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        active = seed_cohort(self.conn, model_id="m3")
        with self.assertRaisesRegex(ForecastError, "rules version"):
            seed_entry_forecast(self.conn, active, "0" * 64)

    def test_forecasts_are_immutable(self) -> None:
        seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE forecasts SET p_mid = '0.99'")


class BaselineTests(ForecastTestCase):
    def test_baseline_attaches_once(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        yes, no = self.books(timedelta(seconds=5))
        attach_baseline(self.conn, forecast_id, yes, no, NOW + timedelta(seconds=7))
        with self.assertRaisesRegex(ForecastError, "already"):
            attach_baseline(self.conn, forecast_id, yes, no, NOW + timedelta(seconds=8))

    def test_baseline_snapshot_before_forecast_is_refused(self) -> None:
        # The forecast carries microseconds; the snapshot does not. As strings,
        # "12:00:00Z" sorts after "12:00:00.500000Z", so only parsed times compare correctly.
        forecast_id = seed_entry_forecast(
            self.conn, self.cohort, self.rules_hash, at=NOW + timedelta(milliseconds=500)
        )
        yes = seed_snapshot(self.conn, "YES", NOW)
        no = seed_snapshot(self.conn, "NO", NOW + timedelta(seconds=1))
        with self.assertRaisesRegex(ForecastError, "before the forecast"):
            attach_baseline(self.conn, forecast_id, yes, no, NOW + timedelta(seconds=2))

    def test_books_fetched_after_the_window_are_refused(self) -> None:
        # Collection started inside the window but the books arrived at +31 minutes.
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        late = NOW + timedelta(minutes=31)
        self.assertEqual(
            resume_step(self.conn, forecast_id, late), ResumeStep.BASELINE_EXPIRED
        )
        yes, no = self.books(timedelta(minutes=31))
        with self.assertRaisesRegex(ForecastError, "after the baseline window"):
            attach_baseline(self.conn, forecast_id, yes, no, late)
        self.assertEqual(
            resume_step(self.conn, forecast_id, late), ResumeStep.BASELINE_EXPIRED
        )

    def test_timely_books_may_be_attached_late(self) -> None:
        # Books fetched at +29 minutes, attached after a crash at +45 minutes.
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        yes, no = self.books(timedelta(minutes=29))
        attach_baseline(self.conn, forecast_id, yes, no, NOW + timedelta(minutes=45))
        self.assertEqual(
            resume_step(self.conn, forecast_id, NOW + timedelta(minutes=45)),
            ResumeStep.NEEDS_DECISION,
        )

    def test_baseline_sides_and_market_must_match(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        yes, no = self.books(timedelta(seconds=1))
        with self.assertRaisesRegex(ForecastError, "outcome"):
            attach_baseline(self.conn, forecast_id, no, yes, NOW + timedelta(seconds=2))
        other = "0x" + "9" * 64
        seed_market(self.conn, other)
        foreign = seed_snapshot(self.conn, "YES", NOW + timedelta(seconds=1), other)
        with self.assertRaisesRegex(ForecastError, "market"):
            attach_baseline(self.conn, forecast_id, foreign, no, NOW + timedelta(seconds=2))


class ResumeTests(ForecastTestCase):
    def test_resume_walks_baseline_then_decision(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        later = NOW + timedelta(minutes=1)
        self.assertEqual(resume_step(self.conn, forecast_id, later), ResumeStep.NEEDS_BASELINE)
        yes, no = self.books(timedelta(minutes=1))
        attach_baseline(self.conn, forecast_id, yes, no, later)
        self.assertEqual(resume_step(self.conn, forecast_id, later), ResumeStep.NEEDS_DECISION)
        self.assertEqual(unfinished_forecasts(self.conn, self.cohort), [forecast_id])

    def test_late_resume_marks_no_timely_baseline_in_every_portfolio(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        with self.assertRaisesRegex(ForecastError, "still open"):
            mark_no_timely_baseline(self.conn, forecast_id, NOW + timedelta(minutes=29))
        late = NOW + timedelta(minutes=31)
        mark_no_timely_baseline(self.conn, forecast_id, late)
        self.assertEqual(resume_step(self.conn, forecast_id, late), ResumeStep.DONE)
        decisions = [r[0] for r in self.conn.execute("SELECT kind FROM decisions")]
        self.assertEqual(decisions, ["NO_TIMELY_BASELINE", "NO_TIMELY_BASELINE"])
        self.assertEqual(unfinished_forecasts(self.conn, self.cohort), [])

    def test_no_timely_baseline_skips_portfolios_that_already_have_a_decision(self) -> None:
        forecast_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        primary = cohort_portfolios(self.conn, self.cohort)["primary"]
        # The API refuses a decision before the baseline exists; seed the state directly.
        self.conn.execute(
            "INSERT INTO decisions (portfolio_id, forecast_id, condition_id, kind, reason, "
            "ticket_id, decided_at) VALUES (?, ?, ?, 'REFUSED', 'X', NULL, ?)",
            (primary, forecast_id, CONDITION_ID, isoformat(NOW)),
        )
        late = NOW + timedelta(minutes=31)
        mark_no_timely_baseline(self.conn, forecast_id, late)
        self.assertEqual(resume_step(self.conn, forecast_id, late), ResumeStep.DONE)
        kinds = sorted(r[0] for r in self.conn.execute("SELECT kind FROM decisions"))
        self.assertEqual(kinds, ["NO_TIMELY_BASELINE", "REFUSED"])

    def test_update_forecast_is_done_after_baseline(self) -> None:
        seed_entry_forecast(self.conn, self.cohort, self.rules_hash)
        update_id = seed_entry_forecast(self.conn, self.cohort, self.rules_hash, kind="update")
        later = NOW + timedelta(minutes=1)
        yes, no = self.books(timedelta(minutes=1))
        attach_baseline(self.conn, update_id, yes, no, later)
        self.assertEqual(resume_step(self.conn, update_id, later), ResumeStep.DONE)
        self.assertNotIn(update_id, unfinished_forecasts(self.conn, self.cohort))


if __name__ == "__main__":
    unittest.main()

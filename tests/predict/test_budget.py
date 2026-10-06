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

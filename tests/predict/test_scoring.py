from __future__ import annotations

import math
import unittest
from decimal import Decimal

from predict_agent.scoring import (
    bootstrap_interval,
    brier,
    bucket_index,
    calibration,
    clip,
    log_score,
    summarize,
)

D = Decimal


class ScoreTests(unittest.TestCase):
    def test_brier_is_the_squared_error_in_exact_decimals(self) -> None:
        self.assertEqual(brier(D("0.8"), 1), D("0.04"))
        self.assertEqual(brier(D("0.8"), 0), D("0.64"))
        self.assertEqual(brier(D("0"), 1), D("1"))  # Brier is not clipped

    def test_log_score_clips_to_one_percent_for_every_forecaster(self) -> None:
        self.assertAlmostEqual(log_score(D("0.8"), 1), math.log(0.8))
        self.assertAlmostEqual(log_score(D("0.8"), 0), math.log(0.2))
        self.assertAlmostEqual(log_score(D("0"), 1), math.log(0.01))
        self.assertAlmostEqual(log_score(D("1"), 0), math.log(0.01))
        self.assertEqual((clip(D("0")), clip(D("1")), clip(D("0.5"))),
                         (D("0.01"), D("0.99"), D("0.5")))

    def test_summary_is_the_mean_over_pairs_and_none_when_empty(self) -> None:
        summary = summarize([(D("0.8"), 1), (D("0.8"), 0)])
        assert summary is not None
        self.assertEqual((summary.n, summary.brier), (2, D("0.34")))
        self.assertAlmostEqual(summary.log, (math.log(0.8) + math.log(0.2)) / 2)
        self.assertIsNone(summarize([]))


class CalibrationTests(unittest.TestCase):
    def test_bucket_edges(self) -> None:
        cases = {"0": 0, "0.0999": 0, "0.1": 1, "0.55": 5, "0.9": 9, "0.99": 9, "1": 9}
        for p, index in cases.items():
            with self.subTest(p):
                self.assertEqual(bucket_index(D(p)), index)

    def test_table_has_ten_buckets_with_mean_forecast_and_observed_rate(self) -> None:
        table = calibration([(D("0.62"), 1), (D("0.68"), 0), (D("0.05"), 0)])
        self.assertEqual(len(table), 10)
        self.assertEqual((table[6].n, table[6].mean_forecast, table[6].observed_rate),
                         (2, D("0.65"), D("0.5")))
        self.assertEqual((table[0].n, table[0].observed_rate), (1, D("0")))
        self.assertEqual((table[3].n, table[3].mean_forecast, table[3].observed_rate),
                         (0, None, None))
        self.assertEqual((table[9].lower, table[9].upper), (D("0.9"), D("1")))


class BootstrapTests(unittest.TestCase):
    def test_no_groups_has_no_interval(self) -> None:
        self.assertIsNone(bootstrap_interval({}))

    def test_one_group_gives_its_own_total(self) -> None:
        self.assertEqual(bootstrap_interval({"e1": [D("2"), D("-0.5")]}), (D("1.5"), D("1.5")))

    def test_interval_is_reproducible_and_brackets_the_resampled_totals(self) -> None:
        groups = {"e1": [D("10")], "e2": [D("-4")], "e3": [D("1"), D("1")], "e4": [D("-2")]}
        first = bootstrap_interval(groups)
        self.assertEqual(first, bootstrap_interval(groups))
        assert first is not None
        low, high = first
        self.assertLessEqual(low, high)
        self.assertGreaterEqual(low, D("-16"))  # every group at its worst total
        self.assertLessEqual(high, D("40"))  # every group at its best total
        self.assertNotEqual(first, bootstrap_interval(groups, seed=1))

    def test_whole_groups_are_resampled_not_single_values(self) -> None:
        # One event holds every value, so every resample is that event's total.
        self.assertEqual(bootstrap_interval({"e1": [D("5"), D("-1"), D("3")]}), (D("7"), D("7")))


if __name__ == "__main__":
    unittest.main()

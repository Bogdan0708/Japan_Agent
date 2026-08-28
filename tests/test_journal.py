from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from japan_agent.journal import modified_dietz_return, write_weekly_post


class ModifiedDietzTest(unittest.TestCase):
    def test_mid_period_flow_is_day_weighted(self) -> None:
        # £10 deposited with half the period remaining: denominator 100 + 5.
        result = modified_dietz_return(
            start_nav=Decimal("100"),
            end_nav=Decimal("105"),
            flows=[(date(2026, 8, 27), Decimal("10"))],
            period_start=date(2026, 8, 24),
            period_end=date(2026, 8, 30),
        )
        expected = (Decimal("105") - Decimal("100") - Decimal("10")) / Decimal("105")
        self.assertEqual(result, expected)

    def test_no_flows_matches_simple_return(self) -> None:
        result = modified_dietz_return(
            start_nav=Decimal("100"),
            end_nav=Decimal("103"),
            flows=[],
            period_start=date(2026, 8, 24),
            period_end=date(2026, 8, 30),
        )
        self.assertEqual(result, Decimal("0.03"))

    def test_flow_outside_period_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            modified_dietz_return(
                start_nav=Decimal("100"),
                end_nav=Decimal("100"),
                flows=[(date(2026, 8, 20), Decimal("10"))],
                period_start=date(2026, 8, 24),
                period_end=date(2026, 8, 30),
            )

    def test_weekly_post_renders_dietz_figures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_weekly_post(
                output_dir=Path(tmp),
                week_ending=date(2026, 8, 30),
                agent_name="Test Analyst",
                start_nav_gbp=Decimal("100"),
                end_nav_gbp=Decimal("105"),
                cash_flows=[(date(2026, 8, 27), Decimal("10"))],
                decisions=[],
                benchmark_returns={},
            )
            text = path.read_text(encoding="utf-8")
        self.assertIn("Modified Dietz return for the week: -4.76%", text)
        self.assertIn("Net external cash flows: £10.00", text)


if __name__ == "__main__":
    unittest.main()

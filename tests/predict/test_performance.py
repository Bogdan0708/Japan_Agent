from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from predict_agent.cli import main
from predict_agent.db import connect
from predict_agent.forecasts import attach_baseline, mark_no_timely_baseline, record_forecast
from predict_agent.performance import performance, render_performance
from predict_agent.settlement import settle_open_tickets
from predict_agent.tickets import open_ticket
from predict_agent.util import canonical_json, isoformat, sha256_json
from tests.predict.fixtures import NOW
from tests.predict.ledger_fixtures import (
    forecast_record,
    portfolio,
    seed_baselined_forecast,
    seed_cohort,
    seed_entry_forecast,
    seed_market,
    seed_observation,
    seed_snapshot,
    ticket_draft,
)
from tests.predict.trade_fixtures import seed_discovery

REPO = Path(__file__).resolve().parents[2]
MARKETS = ["0x" + digit * 64 for digit in "123456789"]
D = Decimal


class PerformanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.conn = connect(self.root / "data" / "predict.sqlite3")
        self.rules = {cid: seed_market(self.conn, cid) for cid in MARKETS}
        self.cohort = seed_cohort(self.conn, shadow=True)
        self.primary = portfolio(self.conn, self.cohort)
        self.shadow = portfolio(self.conn, self.cohort, "shadow_mid")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def scored_market(self, cid: str, outcome: str = "YES", *, trade: bool = True) -> int:
        """A baselined entry forecast (p 0.55/0.60/0.65, base rate 0.30; YES book 0.01 bid,
        0.40 ask), YES tickets in both portfolios, and a confirmed resolution, settled."""
        forecast, yes, _ = seed_baselined_forecast(self.conn, self.cohort, self.rules[cid],
                                                   condition_id=cid)
        if trade:
            for portfolio_id in (self.primary, self.shadow):
                open_ticket(self.conn, ticket_draft(self.conn, portfolio_id, forecast, yes),
                            NOW + timedelta(seconds=2))
        seed_observation(self.conn, outcome, condition_id=cid, fetched_at=NOW + timedelta(days=1))
        settle_open_tickets(self.conn, NOW + timedelta(days=1))
        return forecast

    def report(self, days: float = 2) -> dict[str, Any]:
        data = performance(self.conn, NOW + timedelta(days=days))
        self.assertEqual(len(data["cohorts"]), 1)
        cohort: dict[str, Any] = data["cohorts"][0]
        return cohort


class ScoringTests(PerformanceTestCase):
    def test_three_forecasters_are_scored_on_the_same_rows(self) -> None:
        self.scored_market(MARKETS[0])
        forecasts = self.report()["forecasts"]
        self.assertEqual(forecasts["counts"]["scoring_rows"], 1)
        scores = forecasts["scores"]
        self.assertEqual(scores["claude"]["brier"], "0.16")  # p_mid 0.60, YES
        self.assertEqual(scores["market"]["brier"], "0.632025")  # YES mid (0.01+0.40)/2
        self.assertEqual(scores["base_rate"]["brier"], "0.49")  # base rate 0.30
        self.assertEqual({block["n"] for block in scores.values()}, {1})
        self.assertEqual(list(forecasts["by_category"]), ["politics"])
        self.assertEqual(list(forecasts["by_range_width"]), ["<=0.10"])
        calibration = {b["bucket"]: b for b in forecasts["calibration"]}
        self.assertEqual(calibration["0.6-0.7"]["n"], 1)
        self.assertEqual(calibration["0.6-0.7"]["observed_rate"], "1")

    def test_exclusions_are_counted_and_never_scored(self) -> None:
        self.scored_market(MARKETS[0])
        seed_entry_forecast(self.conn, self.cohort, self.rules[MARKETS[1]],
                            condition_id=MARKETS[1], abstained=True,
                            abstain_reason="rules unclear", p_low=None, p_mid=None,
                            p_high=None, confidence=None, base_rate=None)
        late = seed_entry_forecast(self.conn, self.cohort, self.rules[MARKETS[2]],
                                   condition_id=MARKETS[2])
        mark_no_timely_baseline(self.conn, late, NOW + timedelta(hours=1))
        seed_baselined_forecast(self.conn, self.cohort, self.rules[MARKETS[3]],
                                condition_id=MARKETS[3])  # never resolves
        self.scored_market(MARKETS[4], "HALF", trade=False)
        self.scored_market(MARKETS[5], trade=False)
        self.change_rules(MARKETS[5])
        flagged = forecast_record(self.conn, self.cohort, self.rules[MARKETS[6]],
                                  condition_id=MARKETS[6])
        flagged = replace(flagged, body={**flagged.body, "exposure_flags": ["venue:polymarket"]})
        forecast = record_forecast(self.conn, flagged, NOW)
        yes = seed_snapshot(self.conn, "YES", NOW + timedelta(seconds=1), MARKETS[6])
        no = seed_snapshot(self.conn, "NO", NOW + timedelta(seconds=1), MARKETS[6])
        attach_baseline(self.conn, forecast, yes, no, NOW + timedelta(seconds=1))
        seed_observation(self.conn, "NO", condition_id=MARKETS[6], fetched_at=NOW)
        forecasts = self.report()["forecasts"]
        counts = forecasts["counts"]
        expected = {"entry_forecasts": 7, "abstained": 1, "no_timely_baseline": 1,
                    "unresolved": 1, "half": 1, "rules_changed": 1, "exposure_flagged": 1,
                    "scoring_rows": 1, "awaiting_baseline": 0, "entry_attempts": 7,
                    "distinct_events": 1}
        self.assertEqual({key: counts[key] for key in expected}, expected)
        self.assertEqual(counts["abstention_rate"], format(D(1) / 7, "f"))
        self.assertEqual(forecasts["scores"]["claude"]["n"], 1)
        flagged_scores = forecasts["exposure_flagged_scores"]
        self.assertEqual((flagged_scores["claude"]["n"], flagged_scores["claude"]["brier"]),
                         (1, "0.36"))  # p_mid 0.60, NO

    def change_rules(self, cid: str) -> None:
        payload = {"question": "q", "rules_text": "clarified", "resolution_source": "",
                   "end_date": "2026-11-01T03:59:00Z"}
        new_hash = sha256_json(payload)
        self.conn.execute(
            "INSERT INTO rules_versions (condition_id, rules_hash, rules_json, first_seen_at) "
            "VALUES (?, ?, ?, ?)", (cid, new_hash, canonical_json(payload), isoformat(NOW)))
        self.conn.execute("UPDATE markets SET current_rules_hash = ? WHERE condition_id = ?",
                          (new_hash, cid))

    def test_update_forecasts_are_scored_apart_from_entries(self) -> None:
        self.scored_market(MARKETS[0])
        record = forecast_record(self.conn, self.cohort, self.rules[MARKETS[0]],
                                 condition_id=MARKETS[0], kind="update",
                                 at=NOW + timedelta(days=7), p_mid=D("0.62"))
        update = record_forecast(self.conn, record, NOW + timedelta(days=7))
        later = NOW + timedelta(days=7, seconds=1)
        attach_baseline(self.conn, update, seed_snapshot(self.conn, "YES", later, MARKETS[0]),
                        seed_snapshot(self.conn, "NO", later, MARKETS[0]), later)
        cohort = self.report(days=8)
        self.assertEqual(cohort["forecasts"]["counts"]["scoring_rows"], 1)
        updates = cohort["updates"]
        self.assertEqual((updates["counts"]["update_forecasts"],
                          updates["counts"]["scoring_rows"]), (1, 1))
        self.assertEqual(updates["scores"]["claude"]["brier"], "0.1444")  # (0.62 - 1)^2

    def update(self, cid: str, **overrides: Any) -> int:
        """A baselined update forecast on `cid`, a week after the entry."""
        at = NOW + timedelta(days=7)
        record = forecast_record(self.conn, self.cohort, self.rules[cid], condition_id=cid,
                                 kind="update", at=at, **overrides)
        update = record_forecast(self.conn, record, at)
        later = at + timedelta(seconds=1)
        attach_baseline(self.conn, update, seed_snapshot(self.conn, "YES", later, cid),
                        seed_snapshot(self.conn, "NO", later, cid), later)
        return update

    def test_update_scores_apply_the_same_exclusions(self) -> None:
        for cid in MARKETS[:3]:
            self.scored_market(cid, trade=False)
        self.update(MARKETS[0])
        flagged = forecast_record(self.conn, self.cohort, self.rules[MARKETS[1]],
                                  condition_id=MARKETS[1], kind="update",
                                  at=NOW + timedelta(days=7))
        self.update(MARKETS[1], body={**flagged.body, "exposure_flags": ["venue:kalshi"]})
        self.change_rules(MARKETS[2])
        self.update(MARKETS[2])
        updates = self.report(days=8)["updates"]
        counts = updates["counts"]
        self.assertEqual((counts["update_forecasts"], counts["exposure_flagged"],
                          counts["rules_changed"], counts["scoring_rows"]), (3, 1, 1, 1))
        self.assertEqual(updates["scores"]["claude"]["n"], 1)
        self.assertEqual(updates["exposure_flagged_scores"]["claude"]["n"], 1)


class DiscoveryFunnelTests(PerformanceTestCase):
    def test_latest_discovery_funnel_is_included_with_its_shortlist(self) -> None:
        seed_discovery(self.conn, MARKETS[:2], at=NOW - timedelta(days=1))
        latest = seed_discovery(self.conn, MARKETS[:3], at=NOW)
        self.conn.execute(
            "INSERT INTO refusals (run_id, condition_id, stage, reason_code, detail, at) "
            "VALUES (?, 'x', 'discover', 'LOW_LIQUIDITY', '', ?)", (latest, isoformat(NOW)))
        discovery = performance(self.conn, NOW)["discovery"]
        self.assertEqual(discovery["run_id"], latest)
        self.assertEqual(discovery["markets_seen"], 4)
        self.assertEqual(discovery["funnel"][-1]["remaining"], 3)
        self.assertEqual(discovery["eligible_by_category"], {"politics": 3})
        self.assertEqual(discovery["shortlist_report"], f"shortlist-{latest}.md")
        self.assertIn("## Discovery funnel (latest completed run)",
                      render_performance(performance(self.conn, NOW)))

    def test_no_discovery_yet(self) -> None:
        data = performance(self.conn, NOW)
        self.assertIsNone(data["discovery"])
        self.assertIn("No completed discovery run yet.", render_performance(data))


class PortfolioTests(PerformanceTestCase):
    def test_pnl_edges_and_net_of_research_cost(self) -> None:
        self.scored_market(MARKETS[0])
        primary, shadow = self.report()["portfolios"]
        self.assertEqual((primary["variant"], primary["secondary"]), ("primary", False))
        self.assertEqual((shadow["variant"], shadow["secondary"]), ("shadow_mid", True))
        # 10 YES shares at 0.40 settled at $1: +6. Research cost: one attempt at 0.12.
        self.assertEqual(primary["realized_pnl"], "6")
        self.assertEqual(primary["realized_pnl_net_of_research"], "5.88")
        self.assertEqual((primary["equity"], primary["locked_capital"]), ("1006", "0"))
        self.assertEqual(primary["bootstrap_95_by_event"], ["6", "6"])
        self.assertEqual(primary["hit_rate"], "1")
        self.assertEqual(primary["mean_edge_at_entry"], "0.15")  # p_low 0.55 - 0.40
        self.assertEqual(shadow["mean_edge_at_entry"], "0.2")  # p_mid 0.60 - 0.40
        self.assertEqual(primary["mean_realized_edge"], "0.6")  # 1 - 0.40

    def test_open_tickets_lock_capital_and_count_toward_equity(self) -> None:
        forecast, yes, _ = seed_baselined_forecast(self.conn, self.cohort,
                                                   self.rules[MARKETS[0]],
                                                   condition_id=MARKETS[0])
        open_ticket(self.conn, ticket_draft(self.conn, self.primary, forecast, yes),
                    NOW + timedelta(seconds=2))
        primary = self.report()["portfolios"][0]
        self.assertEqual((primary["available_cash"], primary["locked_capital"],
                          primary["equity"], primary["tickets_open"]), ("996", "4", "1000", 1))
        self.assertIsNone(primary["bootstrap_95_by_event"])
        self.assertIsNone(primary["hit_rate"])


class AttentionTests(PerformanceTestCase):
    def test_resolved_but_unsettled_and_overdue_markets_are_listed(self) -> None:
        forecast, yes, _ = seed_baselined_forecast(self.conn, self.cohort,
                                                   self.rules[MARKETS[0]],
                                                   condition_id=MARKETS[0])
        open_ticket(self.conn, ticket_draft(self.conn, self.primary, forecast, yes),
                    NOW + timedelta(seconds=2))
        seed_observation(self.conn, "YES", cross_check="UNCHECKED", condition_id=MARKETS[0],
                         fetched_at=NOW + timedelta(days=1))
        seed_baselined_forecast(self.conn, self.cohort, self.rules[MARKETS[1]],
                                condition_id=MARKETS[1])
        # Both markets end 2026-11-01; 20 days later neither has a settled outcome.
        attention = self.report(days=47)["attention"]
        self.assertEqual([item["ticket_id"] for item in attention["resolved_not_settled"]], [1])
        self.assertEqual([item["condition_id"] for item in attention["overdue_unresolved"]],
                         MARKETS[:2])


class RenderTests(PerformanceTestCase):
    def test_report_is_read_only_and_renders_every_section(self) -> None:
        self.scored_market(MARKETS[0])
        before = self.conn.total_changes
        data = performance(self.conn, NOW + timedelta(days=2))
        self.assertEqual(self.conn.total_changes, before)
        markdown = render_performance(data)
        for heading in ("### Counts", "### Forecast scores (primary population)",
                        "**Calibration (Claude p_mid)**", "### Paper portfolios",
                        "shadow_mid (secondary)", "### Update forecasts",
                        "### Needs attention", "no detected price exposure",
                        "There is no automated go/no-go"):
            self.assertIn(heading, markdown)
        json.dumps(data)  # every value is JSON-serializable

    def test_empty_database_says_so(self) -> None:
        conn = connect(self.root / "data" / "empty.sqlite3")
        try:
            self.assertIn("No cohorts yet.", render_performance(performance(conn, NOW)))
        finally:
            conn.close()


class CliTests(unittest.TestCase):
    def test_report_performance_writes_markdown_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            shutil.copy(REPO / "config" / "predict-policy.example.json",
                        root / "config" / "predict-policy.json")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["report", "--performance"], root=root, now_fn=lambda: NOW)
            self.assertEqual(code, 0)
            path = Path(out.getvalue().strip())
            self.assertEqual(path.name, "performance-20261005T120000Z.md")
            self.assertTrue(path.exists())
            data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(data["cohorts"], [])


if __name__ == "__main__":
    unittest.main()

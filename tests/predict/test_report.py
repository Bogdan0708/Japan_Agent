from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from predict_agent.collect import discover, finish_run, snapshot_eligible, start_run
from predict_agent.config import load_discovery_config
from predict_agent.db import connect
from predict_agent.http import JsonClient
from predict_agent.report import render_markdown, shortlist
from tests.predict.fakes import RoutedOpener
from tests.predict.fixtures import (
    NO_TOKEN,
    NOW,
    YES_TOKEN,
    clob_book,
    gamma_event,
    gamma_market,
    resolution_row,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "predict-policy.example.json"
CONFIG, POLICY_HASH = load_discovery_config(EXAMPLE)


class ShortlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.run_id = start_run(self.conn, "test", POLICY_HASH, NOW)
        markets = [
            gamma_market(),
            gamma_market(conditionId="0x" + "b" * 64, closed=True),
            gamma_market(conditionId="0x" + "c" * 64, liquidityNum=1),
        ]
        routes: dict[str, list[object]] = {
            "tag_slug=geopolitics": [{"events": []}],
            "tag_slug=economics": [{"events": []}],
            "tag_slug=politics": [{"events": [gamma_event(markets)]}],
            "/v2/resolutions": [{"data": [resolution_row()]}],
            f"token_id={YES_TOKEN}": [clob_book()],
            f"token_id={NO_TOKEN}": [clob_book(asset_id=NO_TOKEN)],
        }
        client = JsonClient(opener=RoutedOpener(routes), sleep=lambda _: None)
        discover(self.conn, client, CONFIG, self.run_id, NOW)
        snapshot_eligible(self.conn, client, self.run_id, self.run_id, lambda: NOW)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_funnel_counts_after_every_exclusion(self) -> None:
        data = shortlist(self.conn, self.run_id)
        self.assertEqual(data["markets_seen"], 3)
        funnel = {step["reason"]: step for step in data["funnel"]}
        self.assertEqual(funnel["NOT_OPEN"]["excluded"], 1)
        self.assertEqual(funnel["NOT_OPEN"]["remaining"], 2)
        self.assertEqual(funnel["LOW_LIQUIDITY"]["remaining"], 1)
        self.assertEqual(data["funnel"][-1]["remaining"], 1)
        self.assertEqual(data["eligible_by_category"], {"politics": 1})

    def test_market_rows_carry_fee_and_yes_quote(self) -> None:
        market = shortlist(self.conn, self.run_id)["markets"][0]
        self.assertEqual(market["fee_rate"], "0.04")
        self.assertEqual(market["yes_best_ask"], "0.37")
        self.assertEqual(market["yes_best_bid"], "0.36")

    def test_markdown_lists_counts_first(self) -> None:
        text = render_markdown(shortlist(self.conn, self.run_id))
        self.assertLess(text.index("Markets seen"), text.index("| Question"))
        self.assertIn("NOT_OPEN", text)


class HistoricalReportTests(ShortlistTests):
    def test_later_discovery_does_not_change_earlier_report(self) -> None:
        finish_run(self.conn, self.run_id, "COMPLETED", NOW)
        before = render_markdown(shortlist(self.conn, self.run_id))
        later = start_run(self.conn, "discover", POLICY_HASH, NOW)
        changed = gamma_market(
            question="Edited question?",
            description="Edited rules.",
            endDate="2026-11-20T00:00:00Z",
            feeSchedule={"exponent": 1, "rate": 0.05, "takerOnly": True},
        )
        routes: dict[str, list[object]] = {
            "tag_slug=geopolitics": [{"events": []}],
            "tag_slug=economics": [{"events": []}],
            "tag_slug=politics": [{"events": [gamma_event([changed])]}],
            "/v2/resolutions": [{"data": [resolution_row()]}],
            f"token_id={YES_TOKEN}": [clob_book(asks=[{"price": "0.50", "size": "9"}])],
            f"token_id={NO_TOKEN}": [clob_book(asset_id=NO_TOKEN)],
        }
        client = JsonClient(opener=RoutedOpener(routes), sleep=lambda _: None)
        discover(self.conn, client, CONFIG, later, NOW)
        snapshot_eligible(self.conn, client, later, later, lambda: NOW)
        self.assertEqual(render_markdown(shortlist(self.conn, self.run_id)), before)
        self.assertIn("Edited question?", render_markdown(shortlist(self.conn, later)))

    def test_failed_run_report_shows_status_banner(self) -> None:
        failed = start_run(self.conn, "discover", POLICY_HASH, NOW)
        finish_run(self.conn, failed, "FAILED", NOW)
        text = render_markdown(shortlist(self.conn, failed))
        self.assertIn("Run status: FAILED", text)
        self.assertIn("partial", text)

    def test_snapshot_run_report_uses_its_own_books_and_source_discovery(self) -> None:
        snap = start_run(self.conn, "snapshot", POLICY_HASH, NOW, source_run_id=self.run_id)
        routes: dict[str, list[object]] = {
            f"token_id={YES_TOKEN}": [clob_book(asks=[{"price": "0.41", "size": "9"}])],
            f"token_id={NO_TOKEN}": [clob_book(asset_id=NO_TOKEN)],
        }
        client = JsonClient(opener=RoutedOpener(routes), sleep=lambda _: None)
        snapshot_eligible(self.conn, client, self.run_id, snap, lambda: NOW)
        data = shortlist(self.conn, snap)
        self.assertEqual(data["discovery_run_id"], self.run_id)
        self.assertEqual(data["markets"][0]["yes_best_ask"], "0.41")
        self.assertEqual(shortlist(self.conn, self.run_id)["markets"][0]["yes_best_ask"], "0.37")

    def test_both_books_count_is_reported(self) -> None:
        self.assertEqual(shortlist(self.conn, self.run_id)["markets_with_both_books"], 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from predict_agent.collect import (
    discover,
    poll_resolutions,
    record_geoblock,
    snapshot_eligible,
    start_run,
)
from predict_agent.config import load_discovery_config
from predict_agent.db import connect, verify_journal
from predict_agent.http import JsonClient
from tests.predict.fakes import RoutedOpener, http_error
from tests.predict.fixtures import (
    CONDITION_ID,
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


def events_routes(
    events_by_tag: dict[str, list[dict[str, Any]]], resolutions: list[dict[str, Any]]
) -> dict[str, list[object]]:
    routes: dict[str, list[object]] = {}
    for slug, _category in CONFIG.tag_categories:
        routes[f"tag_slug={slug}"] = [{"events": events_by_tag.get(slug, [])}]
    routes["/v2/resolutions"] = [{"data": resolutions}]
    return routes


class CollectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "data" / "predict.sqlite3")
        self.run_id = start_run(self.conn, "test", POLICY_HASH, NOW)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def client(self, opener: RoutedOpener) -> JsonClient:
        return JsonClient(opener=opener, sleep=lambda _: None, jitter=lambda: 0.0)

    def refusal_codes(self) -> list[str]:
        rows = self.conn.execute("SELECT reason_code FROM refusals ORDER BY id").fetchall()
        return [row["reason_code"] for row in rows]


class DiscoverTests(CollectTestCase):
    def test_eligible_market_is_stored_with_rules_version(self) -> None:
        market = gamma_market()
        opener = RoutedOpener(
            events_routes({"politics": [gamma_event([market])]}, [resolution_row()])
        )
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual((summary.markets_seen, summary.eligible), (1, 1))
        row = self.conn.execute("SELECT * FROM markets").fetchone()
        self.assertEqual(row["category"], "politics")
        self.assertEqual(row["yes_token_id"], YES_TOKEN)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM rules_versions").fetchone()[0], 1)

    def test_closed_child_in_active_event_is_refused(self) -> None:
        closed = gamma_market(conditionId="0x" + "a" * 64, closed=True)
        opener = RoutedOpener(
            events_routes({"politics": [gamma_event([gamma_market(), closed])]}, [resolution_row()])
        )
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual(summary.refusals, {"NOT_OPEN": 1})
        self.assertEqual(summary.eligible, 1)

    def test_discover_refuses_market_with_proposed_resolution(self) -> None:
        opener = RoutedOpener(
            events_routes(
                {"politics": [gamma_event([gamma_market()])]},
                [resolution_row(status="proposed")],
            )
        )
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual(summary.eligible, 0)
        self.assertEqual(self.refusal_codes(), ["RESOLUTION_IN_PROGRESS"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0], 0)

    def test_missing_resolution_row_is_refused(self) -> None:
        opener = RoutedOpener(events_routes({"politics": [gamma_event([gamma_market()])]}, []))
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual(summary.refusals, {"RESOLUTION_STATE_MISSING": 1})

    def test_parse_error_is_refused_not_raised(self) -> None:
        broken = gamma_market(outcomes="not json")
        opener = RoutedOpener(events_routes({"politics": [gamma_event([broken])]}, []))
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual(summary.refusals, {"PARSE_ERROR": 1})

    def test_event_under_two_tags_is_counted_once(self) -> None:
        event = gamma_event([gamma_market()], tags=("politics", "geopolitics"))
        opener = RoutedOpener(
            events_routes({"geopolitics": [event], "politics": [event]}, [resolution_row()])
        )
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual((summary.markets_seen, summary.eligible), (1, 1))
        category = self.conn.execute("SELECT category FROM markets").fetchone()["category"]
        self.assertEqual(category, "geopolitics")

    def test_rules_change_creates_new_version_and_journals(self) -> None:
        first = RoutedOpener(
            events_routes({"politics": [gamma_event([gamma_market()])]}, [resolution_row()])
        )
        discover(self.conn, self.client(first), CONFIG, self.run_id, NOW)
        second_run = start_run(self.conn, "test", POLICY_HASH, NOW)
        changed = gamma_market(description="Clarified: announcements count only if ...")
        second = RoutedOpener(
            events_routes({"politics": [gamma_event([changed])]}, [resolution_row()])
        )
        discover(self.conn, self.client(second), CONFIG, second_run, NOW)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM rules_versions").fetchone()[0], 2)
        kinds = [r["kind"] for r in self.conn.execute("SELECT kind FROM journal ORDER BY seq")]
        self.assertIn("RULES_CHANGED", kinds)
        self.assertTrue(verify_journal(self.conn))


class SnapshotTests(CollectTestCase):
    def discover_one(self) -> None:
        opener = RoutedOpener(
            events_routes({"politics": [gamma_event([gamma_market()])]}, [resolution_row()])
        )
        discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)

    def test_snapshots_both_tokens(self) -> None:
        self.discover_one()
        opener = RoutedOpener(
            {
                f"token_id={YES_TOKEN}": [clob_book()],
                f"token_id={NO_TOKEN}": [clob_book(asset_id=NO_TOKEN)],
            }
        )
        stored = snapshot_eligible(self.conn, self.client(opener), CONFIG, self.run_id, lambda: NOW)
        self.assertEqual(stored, 2)
        outcomes = {r["outcome"] for r in self.conn.execute("SELECT outcome FROM book_snapshots")}
        self.assertEqual(outcomes, {"YES", "NO"})

    def test_bad_book_is_refused_and_other_token_still_stored(self) -> None:
        self.discover_one()
        opener = RoutedOpener(
            {
                f"token_id={YES_TOKEN}": [clob_book(asks=[])],
                f"token_id={NO_TOKEN}": [clob_book(asset_id=NO_TOKEN)],
            }
        )
        stored = snapshot_eligible(self.conn, self.client(opener), CONFIG, self.run_id, lambda: NOW)
        self.assertEqual(stored, 1)
        self.assertEqual(self.refusal_codes(), ["EMPTY_SIDE"])

    def test_fetch_failure_is_refused_not_raised(self) -> None:
        self.discover_one()
        opener = RoutedOpener(
            {
                f"token_id={YES_TOKEN}": [http_error(404)],
                f"token_id={NO_TOKEN}": [clob_book(asset_id=NO_TOKEN)],
            }
        )
        snapshot_eligible(self.conn, self.client(opener), CONFIG, self.run_id, lambda: NOW)
        self.assertEqual(self.refusal_codes(), ["FETCH_ERROR"])


class ResolutionPollTests(CollectTestCase):
    def test_resolved_market_records_cross_checked_outcome(self) -> None:
        opener = RoutedOpener(
            events_routes({"politics": [gamma_event([gamma_market()])]}, [resolution_row()])
        )
        discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        poll = RoutedOpener(
            {
                "/v2/resolutions": [{"data": [resolution_row(status="resolved", price="0")]}],
                "/markets?": [[{"conditionId": CONDITION_ID, "outcomePrices": '["0", "1"]'}]],
            }
        )
        stored = poll_resolutions(self.conn, self.client(poll), self.run_id, NOW)
        self.assertEqual(stored, 1)
        row = self.conn.execute("SELECT * FROM resolution_observations").fetchone()
        self.assertEqual((row["status"], row["outcome"]), ("resolved", "NO"))
        self.assertEqual(row["cross_check"], "CONFIRMED")

    def test_open_market_observation_has_no_outcome_and_no_gamma_call(self) -> None:
        opener = RoutedOpener(
            events_routes({"politics": [gamma_event([gamma_market()])]}, [resolution_row()])
        )
        discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        poll = RoutedOpener({"/v2/resolutions": [{"data": [resolution_row()]}]})
        poll_resolutions(self.conn, self.client(poll), self.run_id, NOW)
        row = self.conn.execute("SELECT * FROM resolution_observations").fetchone()
        self.assertIsNone(row["outcome"])


class GeoblockTests(CollectTestCase):
    def test_geoblock_recorded(self) -> None:
        opener = RoutedOpener({"api/geoblock": [{"blocked": True, "country": "GB"}]})
        result = record_geoblock(self.conn, self.client(opener), self.run_id)
        self.assertEqual(result, {"blocked": True, "country": "GB"})
        stored = self.conn.execute("SELECT geoblock_json FROM runs").fetchone()[0]
        self.assertIn('"country":"GB"', stored)

    def test_geoblock_failure_never_raises(self) -> None:
        opener = RoutedOpener({"api/geoblock": [http_error(403)]})
        self.assertIsNone(record_geoblock(self.conn, self.client(opener), self.run_id))
        self.assertIn("HTTP 403", self.conn.execute("SELECT geoblock_json FROM runs").fetchone()[0])


class ReviewFixTests(CollectTestCase):
    def test_rules_edit_on_known_but_ineligible_market_is_recorded(self) -> None:
        first = RoutedOpener(
            events_routes({"politics": [gamma_event([gamma_market()])]}, [resolution_row()])
        )
        discover(self.conn, self.client(first), CONFIG, self.run_id, NOW)
        later = start_run(self.conn, "test", POLICY_HASH, NOW)
        edited = gamma_market(closed=True, description="Clarified near resolution.")
        second = RoutedOpener(events_routes({"politics": [gamma_event([edited])]}, []))
        summary = discover(self.conn, self.client(second), CONFIG, later, NOW)
        self.assertEqual(summary.refusals, {"NOT_OPEN": 1})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM rules_versions").fetchone()[0], 2)
        kinds = [r["kind"] for r in self.conn.execute("SELECT kind FROM journal ORDER BY seq")]
        self.assertIn("RULES_CHANGED", kinds)
        self.assertTrue(verify_journal(self.conn))

    def test_identical_rules_on_two_markets_keep_separate_versions(self) -> None:
        other = "0x" + "d" * 64
        markets = [gamma_market(), gamma_market(conditionId=other)]
        rows = [resolution_row(), resolution_row(condition_id=other)]
        opener = RoutedOpener(events_routes({"politics": [gamma_event(markets)]}, rows))
        discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        owners = sorted(
            r["condition_id"] for r in self.conn.execute("SELECT condition_id FROM rules_versions")
        )
        self.assertEqual(owners, sorted([CONDITION_ID, other]))

    def test_malformed_market_entries_are_refused_not_raised(self) -> None:
        event = gamma_event([gamma_market()])
        event["markets"] = ["not-an-object", gamma_market(bestBid="NaN")]
        opener = RoutedOpener(events_routes({"politics": [event]}, []))
        summary = discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        self.assertEqual(summary.refusals, {"PARSE_ERROR": 2})

    def test_resolution_without_closed_gamma_market_is_unchecked(self) -> None:
        opener = RoutedOpener(
            events_routes({"politics": [gamma_event([gamma_market()])]}, [resolution_row()])
        )
        discover(self.conn, self.client(opener), CONFIG, self.run_id, NOW)
        poll = RoutedOpener(
            {
                "/v2/resolutions": [{"data": [resolution_row(status="resolved", price="0")]}],
                "/markets?": [[]],
            }
        )
        poll_resolutions(self.conn, self.client(poll), self.run_id, NOW)
        query = "SELECT outcome, cross_check FROM resolution_observations"
        row = self.conn.execute(query).fetchone()
        self.assertEqual((row["outcome"], row["cross_check"]), ("NO", "UNCHECKED"))


if __name__ == "__main__":
    unittest.main()

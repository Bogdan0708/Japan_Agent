from __future__ import annotations

import unittest

from predict_agent.http import JsonClient
from predict_agent.resolution import (
    fetch_closed_gamma_markets,
    fetch_resolutions,
    gamma_outcome,
    parse_resolution,
    reconcile_outcome,
    resolution_refusal,
)
from tests.predict.fakes import ScriptedOpener
from tests.predict.fixtures import CONDITION_ID, resolution_row

YES = "1000000000000000000"
HALF = "500000000000000000"


class OutcomeMappingTests(unittest.TestCase):
    def test_open_market_has_no_outcome(self) -> None:
        state = parse_resolution(resolution_row())
        self.assertEqual(state.status, "posed")
        self.assertIsNone(state.outcome)

    def test_uma_price_mapping_when_payouts_absent(self) -> None:
        for price, expected in ((YES, "YES"), ("0", "NO"), (HALF, "HALF"), ("69", "UNKNOWN")):
            with self.subTest(price=price):
                state = parse_resolution(resolution_row(status="resolved", price=price))
                self.assertEqual(state.outcome, expected)

    def test_payouts_take_precedence_when_present(self) -> None:
        cases = (([1_000_000, 0], "YES"), ([0, 1_000_000], "NO"), ([500_000, 500_000], "HALF"))
        for payouts, expected in cases:
            with self.subTest(payouts=payouts):
                row = resolution_row(status="resolved", price="0", payouts=payouts)
                self.assertEqual(parse_resolution(row).outcome, expected)
        odd = resolution_row(status="resolved", payouts=[300_000, 700_000])
        self.assertEqual(parse_resolution(odd).outcome, "UNKNOWN")

    def test_disputed_then_resolved_still_has_outcome(self) -> None:
        state = parse_resolution(resolution_row(status="resolved", price=YES, was_disputed=True))
        self.assertEqual(state.outcome, "YES")
        self.assertTrue(state.was_disputed)

    def test_gamma_outcome(self) -> None:
        self.assertEqual(gamma_outcome('["1", "0"]'), "YES")
        self.assertEqual(gamma_outcome('["0", "1"]'), "NO")
        self.assertEqual(gamma_outcome('["0.5", "0.5"]'), "HALF")
        self.assertIsNone(gamma_outcome('["0.36", "0.64"]'))
        self.assertIsNone(gamma_outcome(None))

    def test_cross_check_states(self) -> None:
        resolved_no = parse_resolution(resolution_row(status="resolved", price="0"))
        cases = (
            ('["0", "1"]', ("NO", "CONFIRMED")),
            ('["1", "0"]', ("UNKNOWN", "MISMATCH")),
            ('["0.9995", "0.0005"]', ("UNKNOWN", "MISMATCH")),
            ('["0.0005", "0.9995"]', ("NO", "UNCHECKED")),
            ('["0.4", "0.6"]', ("NO", "UNCHECKED")),
            (None, ("NO", "UNCHECKED")),
        )
        for prices, expected in cases:
            with self.subTest(prices=prices):
                self.assertEqual(reconcile_outcome(resolved_no, prices), expected)

    def test_unresolved_state_is_not_applicable(self) -> None:
        state = parse_resolution(resolution_row())
        self.assertEqual(reconcile_outcome(state, '["1", "0"]'), (None, "NOT_APPLICABLE"))


class RefusalTests(unittest.TestCase):
    def test_refusals(self) -> None:
        self.assertEqual(resolution_refusal(None), "RESOLUTION_STATE_MISSING")
        self.assertIsNone(resolution_refusal(resolution_row(status="posed")))
        for status in ("proposed", "challenged", "reproposed", "disputed", "resolved"):
            with self.subTest(status=status):
                self.assertEqual(
                    resolution_refusal(resolution_row(status=status)), "RESOLUTION_IN_PROGRESS"
                )


class FetchTests(unittest.TestCase):
    def test_fetch_resolutions_batches_twenty(self) -> None:
        ids = [f"0x{i:064x}" for i in range(21)]
        opener = ScriptedOpener(
            [{"data": [resolution_row(condition_id=ids[0])]}, {"data": []}]
        )
        rows = fetch_resolutions(JsonClient(opener=opener, sleep=lambda _: None), ids)
        self.assertEqual(list(rows), [ids[0]])
        self.assertEqual(len(opener.requests), 2)
        self.assertIn("/v2/resolutions?condition=", opener.requests[0])

    def test_fetch_closed_gamma_markets_passes_closed_true(self) -> None:
        opener = ScriptedOpener([[{"conditionId": CONDITION_ID, "outcomePrices": '["0", "1"]'}]])
        rows = fetch_closed_gamma_markets(
            JsonClient(opener=opener, sleep=lambda _: None), [CONDITION_ID]
        )
        self.assertIn(CONDITION_ID, rows)
        self.assertIn("closed=true", opener.requests[0])


if __name__ == "__main__":
    unittest.main()

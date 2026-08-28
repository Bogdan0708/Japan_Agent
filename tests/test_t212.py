from __future__ import annotations

import json
import unittest
from decimal import Decimal

from japan_agent.execute.t212 import HttpResult, Trading212Client


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        body = json.dumps({"id": 99, "ticker": "SONY_US_EQ", "status": "NEW"}).encode()
        return HttpResult(200, body, {})


class Trading212ClientTests(unittest.TestCase):
    def test_market_sell_uses_negative_quantity_and_one_post(self) -> None:
        transport = RecordingTransport()
        client = Trading212Client(
            api_key="key", api_secret="secret", environment="demo", transport=transport
        )
        client.place_market_order(ticker="SONY_US_EQ", quantity=Decimal("-0.1234"))
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertTrue(call["url"].endswith("/api/v0/equity/orders/market"))
        self.assertEqual(json.loads(call["body"])["quantity"], -0.1234)
        self.assertIn("Basic ", call["headers"]["Authorization"])


if __name__ == "__main__":
    unittest.main()


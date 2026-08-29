from __future__ import annotations

import json
import unittest
from decimal import Decimal

from japan_agent.execute.t212 import (
    BrokerRejected,
    BrokerTransportUncertain,
    HttpResult,
    Trading212Client,
)


class StatusTransport:
    def __init__(self, status: int):
        self.status = status

    def request(self, **kwargs):
        return HttpResult(self.status, b'{"error": "probe"}', {})


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        body = json.dumps({"id": 99, "ticker": "SONY_US_EQ", "status": "NEW"}).encode()
        return HttpResult(200, body, {})


class ScriptedTransport:
    def __init__(self, body: bytes):
        self.body = body
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return HttpResult(200, self.body, {})


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
        self.assertEqual(
            json.loads(call["body"], parse_float=Decimal)["quantity"], Decimal("-0.1234")
        )
        self.assertIn("Basic ", call["headers"]["Authorization"])

    def test_order_quantity_is_exact_decimal_text_on_the_wire(self) -> None:
        # float(Decimal("-0.1000")) would serialize as -0.1, silently dropping
        # the approved quantity's exact representation.
        transport = RecordingTransport()
        client = Trading212Client(
            api_key="key", api_secret="secret", environment="demo", transport=transport
        )
        client.place_market_order(ticker="SONY_US_EQ", quantity=Decimal("-0.1000"))
        body = transport.calls[0]["body"].decode()
        self.assertIn('"quantity":-0.1000', body)

    def test_order_history_url_and_paginated_shape(self) -> None:
        transport = ScriptedTransport(
            json.dumps({"items": [{"id": 5, "status": "FILLED"}, "junk"]}).encode()
        )
        client = Trading212Client(
            api_key="key", api_secret="secret", environment="demo", transport=transport
        )
        items = client.order_history(ticker="SONY_US_EQ", limit=20)
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertTrue(
            call["url"].endswith("/api/v0/equity/history/orders?limit=20&ticker=SONY_US_EQ"),
            call["url"],
        )
        self.assertEqual(items, [{"id": 5, "status": "FILLED"}])

    def test_order_history_accepts_bare_list_shape(self) -> None:
        transport = ScriptedTransport(json.dumps([{"id": 7}]).encode())
        client = Trading212Client(
            api_key="key", api_secret="secret", environment="demo", transport=transport
        )
        self.assertEqual(client.order_history(), [{"id": 7}])

    def test_every_5xx_and_throttle_post_status_is_uncertain(self) -> None:
        for status in (408, 429, *range(500, 600)):
            with self.subTest(status=status):
                client = Trading212Client(
                    api_key="key",
                    api_secret="secret",
                    environment="demo",
                    transport=StatusTransport(status),
                )
                with self.assertRaises(BrokerTransportUncertain):
                    client.place_market_order(ticker="SONY_US_EQ", quantity=Decimal("1"))

    def test_definite_4xx_post_status_is_rejected(self) -> None:
        for status in (400, 401, 403, 404, 422):
            client = Trading212Client(
                api_key="key",
                api_secret="secret",
                environment="demo",
                transport=StatusTransport(status),
            )
            with self.assertRaises(BrokerRejected, msg=f"HTTP {status}"):
                client.place_market_order(ticker="SONY_US_EQ", quantity=Decimal("1"))


if __name__ == "__main__":
    unittest.main()


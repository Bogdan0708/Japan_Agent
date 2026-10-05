from __future__ import annotations

import http.client
import ssl
import unittest
import urllib.error

from predict_agent.http import MAX_SLEEP_SECONDS, FetchError, JsonClient
from tests.predict.fakes import ScriptedOpener, http_error


def client(opener: ScriptedOpener, sleeps: list[float]) -> JsonClient:
    return JsonClient(opener=opener, sleep=sleeps.append, jitter=lambda: 0.0)


class JsonClientTests(unittest.TestCase):
    def test_success_returns_parsed_json_and_encodes_params(self) -> None:
        opener = ScriptedOpener([{"ok": True}])
        result = client(opener, []).get("https://h/x", {"a": "1", "limit": 2})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(opener.requests, ["https://h/x?a=1&limit=2"])

    def test_retries_5xx_then_succeeds_with_exponential_backoff(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([http_error(503), http_error(502), [1]])
        self.assertEqual(client(opener, sleeps).get("https://h/x"), [1])
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_network_error_is_retried(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([urllib.error.URLError("down"), TimeoutError(), {"x": 1}])
        self.assertEqual(client(opener, sleeps).get("https://h/x"), {"x": 1})
        self.assertEqual(len(sleeps), 2)

    def test_retries_are_bounded(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([http_error(500)] * 4)
        with self.assertRaisesRegex(FetchError, "retries exhausted"):
            client(opener, sleeps).get("https://h/x")
        self.assertEqual(len(opener.requests), 4)
        self.assertEqual(len(sleeps), 3)

    def test_non_retryable_4xx_fails_immediately(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([http_error(404)])
        with self.assertRaisesRegex(FetchError, "HTTP 404"):
            client(opener, sleeps).get("https://h/x")
        self.assertEqual(sleeps, [])

    def test_retry_after_header_is_honoured(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([http_error(429, retry_after="7"), {}])
        client(opener, sleeps).get("https://h/x")
        self.assertEqual(sleeps, [7.0])

    def test_retry_after_is_capped(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([http_error(429, retry_after="3600"), {}])
        client(opener, sleeps).get("https://h/x")
        self.assertEqual(sleeps, [MAX_SLEEP_SECONDS])

    def test_malformed_json_fails_without_retry(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener([b"<html>blocked</html>"])
        with self.assertRaisesRegex(FetchError, "malformed JSON"):
            client(opener, sleeps).get("https://h/x")
        self.assertEqual(sleeps, [])


class ReadPhaseErrorTests(unittest.TestCase):
    def test_incomplete_read_and_ssl_errors_are_retried(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener(
            [http.client.IncompleteRead(b""), ssl.SSLError("bad record"), {"ok": 1}]
        )
        self.assertEqual(client(opener, sleeps).get("https://h/x"), {"ok": 1})
        self.assertEqual(len(sleeps), 2)


class RepeatedParamTests(unittest.TestCase):
    def test_sequence_params_repeat_keys(self) -> None:
        opener = ScriptedOpener([[]])
        client(opener, []).get("https://h/m", [("id", "a"), ("id", "b"), ("closed", "true")])
        self.assertEqual(opener.requests, ["https://h/m?id=a&id=b&closed=true"])


if __name__ == "__main__":
    unittest.main()

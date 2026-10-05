"""GET-only JSON client. There is deliberately no POST/PUT/DELETE support."""

from __future__ import annotations

import http.client
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol

USER_AGENT = "predict-agent/0.1 (read-only research)"
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
MAX_SLEEP_SECONDS = 30.0


class FetchError(RuntimeError):
    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"{reason}: {url}")
        self.url = url
        self.reason = reason


class Response(Protocol):
    def read(self) -> bytes: ...

    def __enter__(self) -> Response: ...

    def __exit__(self, *args: object) -> None: ...


Opener = Callable[[urllib.request.Request, float], Response]


def _urlopen(request: urllib.request.Request, timeout: float) -> Response:
    response: Response = urllib.request.urlopen(request, timeout=timeout)
    return response


def _retry_after(header: str | None) -> float | None:
    if header is None:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        return None


class JsonClient:
    def __init__(
        self,
        *,
        opener: Opener | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self._opener: Opener = opener or _urlopen
        self._sleep = sleep
        self._jitter = jitter
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def get(self, url: str, params: Mapping[str, str | int] | None = None) -> Any:
        full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        request = urllib.request.Request(
            full_url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            method="GET",
        )
        for attempt in range(self._max_retries + 1):
            retry_after: float | None = None
            try:
                with self._opener(request, self._timeout) as response:
                    body = response.read()
            except urllib.error.HTTPError as error:
                status = error.code
                header = error.headers.get("Retry-After") if error.headers else None
                error.close()
                if status not in RETRYABLE_STATUS:
                    raise FetchError(full_url, f"HTTP {status}") from None
                retry_after = _retry_after(header)
                reason = f"HTTP {status}"
            except (OSError, http.client.HTTPException) as error:
                # URLError, timeouts, resets and SSL errors are all OSError subclasses.
                reason = f"network error {type(error).__name__}"
            else:
                try:
                    return json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise FetchError(full_url, "malformed JSON") from None
            if attempt == self._max_retries:
                raise FetchError(full_url, f"retries exhausted ({reason})")
            delay = (
                retry_after
                if retry_after is not None
                else self._backoff_base * (2**attempt) + self._jitter()
            )
            self._sleep(min(delay, MAX_SLEEP_SECONDS))
        raise AssertionError("unreachable")

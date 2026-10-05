from __future__ import annotations

import email.message
import json
import urllib.error
import urllib.request
from typing import Any


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://example.invalid", code, "error", headers, None)


def _respond(item: object) -> FakeResponse:
    if isinstance(item, BaseException):
        raise item
    if isinstance(item, bytes):
        return FakeResponse(item)
    return FakeResponse(json.dumps(item).encode("utf-8"))


class ScriptedOpener:
    """Returns (or raises) each scripted outcome in order, recording requested URLs."""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[str] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any:
        self.requests.append(request.full_url)
        if not self._outcomes:
            raise AssertionError(f"unexpected request {request.full_url}")
        return _respond(self._outcomes.pop(0))


class RoutedOpener:
    """Routes by URL substring (first matching key in insertion order); each route is a queue."""

    def __init__(self, routes: dict[str, list[object]]) -> None:
        self._routes = {key: list(value) for key, value in routes.items()}
        self.requests: list[str] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any:
        url = request.full_url
        self.requests.append(url)
        for key, queue in self._routes.items():
            if key in url:
                if not queue:
                    raise AssertionError(f"route {key!r} exhausted for {url}")
                return _respond(queue.pop(0))
        raise AssertionError(f"no route for {url}")

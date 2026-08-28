from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


class BrokerError(RuntimeError):
    pass


class BrokerRejected(BrokerError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body[:1000]
        super().__init__(f"Trading 212 rejected request with HTTP {status}")


class BrokerTransportUncertain(BrokerError):
    """The client cannot prove whether a non-idempotent order was accepted."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    headers: dict[str, str]


class Transport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResult: ...


class UrllibTransport:
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResult:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResult(
                    status=response.status,
                    body=response.read(),
                    headers={key.lower(): value for key, value in response.headers.items()},
                )
        except urllib.error.HTTPError as error:
            return HttpResult(
                status=error.code,
                body=error.read(),
                headers={key.lower(): value for key, value in error.headers.items()},
            )
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise BrokerTransportUncertain("broker request outcome is unknown") from error


class Trading212Client:
    """Small official-v0 adapter with deliberately no POST retry behavior."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        environment: str = "demo",
        transport: Transport | None = None,
        timeout: float = 15.0,
    ):
        if environment not in {"demo", "live"}:
            raise ValueError("environment must be demo or live")
        if not api_key or not api_secret:
            raise ValueError("both Trading 212 API credentials are required")
        self.base_url = f"https://{environment}.trading212.com/api/v0"
        credentials = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode("ascii")
        self.headers = {
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
            "User-Agent": "japan-tech-analyst/0.1",
        }
        self.transport = transport or UrllibTransport()
        self.timeout = timeout

    def _json_request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
        headers = dict(self.headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        result = self.transport.request(
            method=method,
            url=self.base_url + path,
            headers=headers,
            body=body,
            timeout=self.timeout,
        )
        if not 200 <= result.status < 300:
            text = result.body.decode("utf-8", errors="replace")
            if method == "POST" and result.status in {408, 429, 500, 502, 503, 504}:
                raise BrokerTransportUncertain(
                    f"broker returned HTTP {result.status}; order outcome needs reconciliation"
                )
            raise BrokerRejected(result.status, text)
        try:
            return json.loads(result.body) if result.body else None
        except json.JSONDecodeError as error:
            if method == "POST":
                raise BrokerTransportUncertain(
                    "broker returned an invalid order response; outcome needs reconciliation"
                ) from error
            raise BrokerError("broker returned invalid JSON") from error

    def account_summary(self) -> dict[str, Any]:
        result = self._json_request("GET", "/equity/account/summary")
        if not isinstance(result, dict):
            raise BrokerError("unexpected account summary response")
        return result

    def instruments(self) -> list[dict[str, Any]]:
        result = self._json_request("GET", "/equity/metadata/instruments")
        if not isinstance(result, list):
            raise BrokerError("unexpected instruments response")
        return [item for item in result if isinstance(item, dict)]

    def positions(self) -> list[dict[str, Any]]:
        result = self._json_request("GET", "/equity/positions")
        if not isinstance(result, list):
            raise BrokerError("unexpected positions response")
        return [item for item in result if isinstance(item, dict)]

    def place_market_order(
        self, *, ticker: str, quantity: Decimal, extended_hours: bool = False
    ) -> dict[str, Any]:
        # Official endpoint is non-idempotent. Do not add a generic retry here.
        result = self._json_request(
            "POST",
            "/equity/orders/market",
            {
                "ticker": ticker,
                "quantity": float(quantity),
                "extendedHours": extended_hours,
            },
        )
        if not isinstance(result, dict):
            raise BrokerTransportUncertain("broker did not return an order object")
        return result

    def order(self, order_id: str | int) -> dict[str, Any]:
        result = self._json_request("GET", f"/equity/orders/{order_id}")
        if not isinstance(result, dict):
            raise BrokerError("unexpected order response")
        return result


class Broker(Protocol):
    def place_market_order(
        self, *, ticker: str, quantity: Decimal, extended_hours: bool = False
    ) -> dict[str, Any]: ...

    def order(self, order_id: str | int) -> dict[str, Any]: ...


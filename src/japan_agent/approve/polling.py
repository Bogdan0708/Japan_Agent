from __future__ import annotations

import sys
import time
from typing import Any, Callable, Iterable

from ..time import utc_now
from .telegram import TelegramApprovalChannel


class TelegramPoller:
    """Long-poll getUpdates instead of exposing a public webhook.

    The HTTPS request to Telegram is authenticated by the bot token, so the
    configured webhook secret is supplied to handle_update by this process
    itself; the sender/chat allowlist checks inside the channel still apply
    unchanged. Offsets always advance past a bad update so one malformed or
    hostile callback cannot wedge the approval loop.
    """

    def __init__(
        self,
        channel: TelegramApprovalChannel,
        *,
        fetch: Callable[[int, int], list[dict[str, Any]]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.channel = channel
        self.uses_api_fetch = fetch is None
        self.fetch = fetch or self._fetch_via_api
        self.sleep = sleep

    def _fetch_via_api(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        # The HTTP client must outlive Telegram's server-side long-poll wait,
        # otherwise every getUpdates times out locally before it can answer.
        result = self.channel._post(
            "/getUpdates",
            {
                "offset": str(offset),
                "timeout": str(timeout),
                "allowed_updates": '["callback_query"]',
            },
            timeout=timeout + 10,
        )
        updates = result.get("result", [])
        return updates if isinstance(updates, list) else []

    def process(self, updates: Iterable[dict[str, Any]], *, offset: int) -> tuple[int, int]:
        """Handle a batch; returns (next_offset, handled_count)."""
        handled = 0
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = max(offset, update_id + 1)
            try:
                outcome = self.channel.handle_update(
                    update,
                    webhook_secret_header=self.channel.config.webhook_secret,
                    now=utc_now(),
                )
            except (PermissionError, ValueError, KeyError):
                # Never log update contents: they can carry usernames and payloads.
                print(f"ignored update {update_id}", file=sys.stderr)
                continue
            except (RuntimeError, OSError):
                # The decision is recorded before answerCallbackQuery; only the
                # cosmetic acknowledgement can fail here. Network failures from
                # urlopen surface as URLError, an OSError, not a RuntimeError.
                print(f"processed update {update_id}; Telegram ack failed", file=sys.stderr)
                handled += 1
                continue
            print(f"processed update {update_id}: {outcome}", file=sys.stderr)
            handled += 1
        return offset, handled

    def poll_forever(self, *, timeout: int = 50, retry_delay: float = 10.0) -> None:
        if self.uses_api_fetch:
            # Telegram refuses getUpdates while a webhook is registered.
            try:
                self.channel._post("/deleteWebhook", {})
            except (RuntimeError, OSError):
                print(
                    "deleteWebhook failed; getUpdates may conflict with an active webhook",
                    file=sys.stderr,
                )
        offset = 0
        while True:
            try:
                updates = self.fetch(offset, timeout)
            except (RuntimeError, OSError):
                print("Telegram getUpdates failed; retrying", file=sys.stderr)
                self.sleep(retry_delay)
                continue
            offset, _ = self.process(updates, offset=offset)

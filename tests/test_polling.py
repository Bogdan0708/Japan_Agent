from __future__ import annotations

import unittest
from typing import Any

from japan_agent.approve.polling import TelegramPoller


class StubConfig:
    webhook_secret = "poll-secret"


class StubChannel:
    """Records handle_update calls; scripted outcomes per update_id."""

    config = StubConfig()

    def __init__(self, outcomes: dict[int, Any]):
        self.outcomes = outcomes
        self.calls: list[tuple[dict[str, Any], str]] = []

    def handle_update(self, update: dict[str, Any], *, webhook_secret_header: str, now) -> str:
        self.calls.append((update, webhook_secret_header))
        outcome = self.outcomes.get(update.get("update_id"))
        if outcome is None:
            raise ValueError("update is not a callback query")
        if isinstance(outcome, Exception):
            raise outcome
        return str(outcome)


class PollingTest(unittest.TestCase):
    def test_offset_always_advances_past_bad_updates(self) -> None:
        channel = StubChannel(
            {
                10: ValueError("update is not a callback query"),
                11: PermissionError("wrong sender"),
                12: "approved",
            }
        )
        poller = TelegramPoller(channel, fetch=lambda offset, timeout: [])
        offset, handled = poller.process(
            [{"update_id": 10}, {"update_id": 11}, {"update_id": 12}], offset=0
        )
        self.assertEqual(offset, 13)
        self.assertEqual(handled, 1)

    def test_configured_secret_is_supplied_to_the_channel(self) -> None:
        channel = StubChannel({5: "rejected"})
        poller = TelegramPoller(channel, fetch=lambda offset, timeout: [])
        poller.process([{"update_id": 5}], offset=0)
        self.assertEqual(channel.calls[0][1], "poll-secret")

    def test_ack_failure_still_counts_as_handled(self) -> None:
        # The approval is recorded before answerCallbackQuery; a RuntimeError
        # from the acknowledgement must not look like an unprocessed update.
        channel = StubChannel({7: RuntimeError("Telegram API rejected request")})
        poller = TelegramPoller(channel, fetch=lambda offset, timeout: [])
        offset, handled = poller.process([{"update_id": 7}], offset=0)
        self.assertEqual((offset, handled), (8, 1))

    def test_missing_update_id_does_not_crash_or_rewind(self) -> None:
        channel = StubChannel({3: "approved"})
        poller = TelegramPoller(channel, fetch=lambda offset, timeout: [])
        offset, handled = poller.process(
            [{"no_id": True}, {"update_id": 3}], offset=2
        )
        self.assertEqual(offset, 4)
        self.assertEqual(handled, 1)


if __name__ == "__main__":
    unittest.main()

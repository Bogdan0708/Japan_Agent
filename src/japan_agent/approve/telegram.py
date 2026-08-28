from __future__ import annotations

import hmac
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..models import TradeTicket
from .service import ApprovalService


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    approval_chat_id: str
    approver_user_id: str
    webhook_secret: str


class TelegramApprovalChannel:
    def __init__(self, config: TelegramConfig, approval_service: ApprovalService):
        if not all(
            (
                config.bot_token,
                config.approval_chat_id,
                config.approver_user_id,
                config.webhook_secret,
            )
        ):
            raise ValueError("all Telegram approval settings are required")
        self.config = config
        self.approval_service = approval_service
        self.base_url = f"https://api.telegram.org/bot{config.bot_token}"

    def send_ticket(self, ticket: TradeTicket) -> dict[str, Any]:
        short_hash = ticket.fingerprint[:12]
        side = ticket.side.value
        evidence = "\n".join(f"- {item[:500]}" for item in ticket.evidence[:4])
        text = (
            f"Japan Tech Analyst — HUMAN APPROVAL REQUIRED\n\n"
            f"{side} {abs(ticket.quantity)} shares of {ticket.ticker}\n"
            f"Estimated value: £{ticket.estimated_value_gbp}\n"
            f"Reference price: £{ticket.reference_price_gbp}; max move: "
            f"{ticket.max_price_deviation_fraction:.1%}\n"
            f"Target weight: {ticket.target_weight:.1%}; confidence: {ticket.confidence:.0%}\n"
            f"Expires: {ticket.expires_at.isoformat()}\n"
            f"Ticket hash: {short_hash}\n\n"
            f"Thesis: {ticket.thesis[:1000]}\n\n"
            f"Evidence:\n{evidence}\n\n"
            f"Invalidation: {ticket.invalidation_condition[:500]}\n\n"
            "AI-generated research, not investment advice. Approval authorizes only the fixed "
            "ticker, side and quantity shown above."
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "Approve",
                        "callback_data": f"A|{ticket.proposal_id}|{short_hash}",
                    },
                    {
                        "text": "Reject",
                        "callback_data": f"R|{ticket.proposal_id}|{short_hash}",
                    },
                ]
            ]
        }
        return self._post(
            "/sendMessage",
            {
                "chat_id": self.config.approval_chat_id,
                "text": text,
                "reply_markup": json.dumps(keyboard, separators=(",", ":")),
            },
        )

    def handle_update(
        self, update: dict[str, Any], *, webhook_secret_header: str, now: datetime
    ) -> str:
        if not hmac.compare_digest(webhook_secret_header, self.config.webhook_secret):
            raise PermissionError("invalid Telegram webhook secret")
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            raise ValueError("update is not a callback query")
        sender_id = str(callback.get("from", {}).get("id", ""))
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        if sender_id != self.config.approver_user_id or chat_id != self.config.approval_chat_id:
            raise PermissionError("callback is not from the configured human approver/chat")
        parts = str(callback.get("data", "")).split("|")
        if len(parts) != 3 or parts[0] not in {"A", "R"}:
            raise ValueError("malformed approval callback")
        action, proposal_id, short_hash = parts
        stored = self.approval_service.database.get_proposal(proposal_id)
        if stored is None or not hmac.compare_digest(stored.ticket_hash[:12], short_hash):
            raise ValueError("callback does not match the immutable ticket")
        approver = f"telegram-user:{sender_id}"
        if action == "A":
            self.approval_service.approve(
                proposal_id,
                approver=approver,
                expected_hash=stored.ticket_hash,
                now=now,
            )
            result = "approved"
        else:
            self.approval_service.reject(
                proposal_id,
                approver=approver,
                expected_hash=stored.ticket_hash,
                reason="Rejected using the Telegram ticket button",
                now=now,
            )
            result = "rejected"
        callback_id = callback.get("id")
        if callback_id:
            self._post(
                "/answerCallbackQuery",
                {"callback_query_id": str(callback_id), "text": f"Ticket {result}."},
            )
        return result

    def _post(self, path: str, values: dict[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(values).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read())
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Telegram API rejected request")
        return result

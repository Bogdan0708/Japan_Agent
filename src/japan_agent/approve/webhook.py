from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..time import utc_now
from .telegram import TelegramApprovalChannel


def serve_webhook(
    channel: TelegramApprovalChannel, *, host: str = "127.0.0.1", port: int = 8080
) -> None:
    class Handler(BaseHTTPRequestHandler):
        server_version = "JapanTechApproval/0.1"

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/telegram/webhook":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if length <= 0 or length > 1_000_000:
                self.send_error(413)
                return
            try:
                update: Any = json.loads(self.rfile.read(length))
                if not isinstance(update, dict):
                    raise ValueError("Telegram update must be an object")
                result = channel.handle_update(
                    update,
                    webhook_secret_header=self.headers.get(
                        "X-Telegram-Bot-Api-Secret-Token", ""
                    ),
                    now=utc_now(),
                )
            except PermissionError:
                self.send_error(403)
                return
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            body = json.dumps({"ok": True, "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            # Avoid emitting update details, tokens, usernames, or callback payloads.
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .models import InstrumentRule


@dataclass(frozen=True)
class Settings:
    root: Path
    database_path: Path
    journal_path: Path
    whitelist_path: Path
    killswitch_path: Path
    t212_environment: str
    t212_api_key: str | None
    t212_api_secret: str | None
    account_currency: str
    allow_live_trading: str | None
    written_consent_reference: str | None
    live_gate_file: Path | None
    telegram_bot_token: str | None
    telegram_approver_user_id: str | None
    telegram_approval_chat_id: str | None
    telegram_webhook_secret: str | None
    claude_model: str
    jquants_api_key: str | None
    edinet_api_key: str | None

    @classmethod
    def from_env(cls, root: Path | None = None) -> Settings:
        project_root = (root or Path.cwd()).resolve()
        data_dir = project_root / "data"
        live_gate = os.getenv("LIVE_GATE_FILE")
        environment = os.getenv("T212_ENV", "demo").lower()
        if environment not in {"demo", "live"}:
            raise ValueError("T212_ENV must be demo or live")
        return cls(
            root=project_root,
            database_path=data_dir / "agent.sqlite3",
            journal_path=project_root / "journal" / "decisions.jsonl",
            whitelist_path=project_root / "config" / "whitelist.json",
            killswitch_path=data_dir / "KILLSWITCH",
            t212_environment=environment,
            t212_api_key=os.getenv("T212_API_KEY"),
            t212_api_secret=os.getenv("T212_API_SECRET"),
            account_currency=os.getenv("T212_ACCOUNT_CURRENCY", "GBP").upper(),
            allow_live_trading=os.getenv("ALLOW_LIVE_TRADING"),
            written_consent_reference=os.getenv("T212_WRITTEN_CONSENT_REF"),
            live_gate_file=Path(live_gate).resolve() if live_gate else None,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_approver_user_id=os.getenv("TELEGRAM_APPROVER_USER_ID"),
            telegram_approval_chat_id=os.getenv("TELEGRAM_APPROVAL_CHAT_ID"),
            telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET"),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
            jquants_api_key=os.getenv("JQUANTS_API_KEY"),
            edinet_api_key=os.getenv("EDINET_API_KEY"),
        )

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)

    def load_whitelist(self) -> dict[str, InstrumentRule]:
        data = json.loads(self.whitelist_path.read_text(encoding="utf-8"))
        verified_at = data.get("verified_at")
        if not verified_at:
            return {}
        if data.get("account_environment") != self.t212_environment:
            raise ValueError(
                "whitelist account_environment does not match the configured T212 environment"
            )
        return {
            rule.ticker: rule
            for rule in (InstrumentRule.from_dict(item) for item in data.get("instruments", []))
        }


def load_dotenv(path: Path) -> None:
    """Minimal .env loader that never overrides an existing environment value."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

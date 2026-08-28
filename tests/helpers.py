from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from japan_agent.config import Settings
from japan_agent.models import (
    Action,
    InstrumentRule,
    Portfolio,
    Position,
    PriceSnapshot,
    ResearchDecision,
    Sleeve,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def instrument(
    *, sleeve: Sleeve = Sleeve.CORE, max_gbp: Decimal | None = None
) -> InstrumentRule:
    return InstrumentRule(
        ticker="TEST_EQ",
        display_name="Test Japan Technology ETF",
        sleeve=sleeve,
        instrument_type="ETF",
        native_currency="USD",
        max_position_gbp=max_gbp,
    )


def snapshot(*, price_gbp: str = "10", observed_at: datetime = NOW) -> PriceSnapshot:
    return PriceSnapshot(
        ticker="TEST_EQ",
        native_price=Decimal("13.50"),
        native_currency="USD",
        price_gbp=Decimal(price_gbp),
        observed_at=observed_at,
        source="test",
    )


def decision(*, action: Action = Action.BUY, target: str = "0.30") -> ResearchDecision:
    return ResearchDecision(
        thesis="Automation demand is supported by two timestamped primary-source observations.",
        evidence=("Evidence A", "Evidence B"),
        action=action,
        instrument="TEST_EQ",
        target_weight=Decimal(target),
        confidence=Decimal("0.72"),
        invalidation_condition="Earnings revisions and orders both turn negative.",
    )


def portfolio(
    *, cash: str = "100", quantity: str = "0", position_value: str = "0"
) -> Portfolio:
    positions = ()
    if Decimal(quantity) or Decimal(position_value):
        positions = (
            Position(
                ticker="TEST_EQ",
                quantity=Decimal(quantity),
                market_value_gbp=Decimal(position_value),
            ),
        )
    return Portfolio(cash_gbp=Decimal(cash), positions=positions)


def settings(root: Path, *, environment: str = "demo") -> Settings:
    return Settings(
        root=root,
        database_path=root / "data" / "agent.sqlite3",
        journal_path=root / "journal" / "decisions.jsonl",
        whitelist_path=root / "config" / "whitelist.json",
        killswitch_path=root / "data" / "KILLSWITCH",
        t212_environment=environment,
        t212_api_key="key",
        t212_api_secret="secret",
        account_currency="GBP",
        allow_live_trading=None,
        written_consent_reference=None,
        live_gate_file=None,
        telegram_bot_token=None,
        telegram_approver_user_id=None,
        telegram_approval_chat_id=None,
        telegram_webhook_secret=None,
        claude_model="test-model",
        jquants_api_key=None,
        edinet_api_key=None,
    )

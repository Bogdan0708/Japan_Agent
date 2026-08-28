from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from japan_agent.approve import ApprovalService
from japan_agent.execute.service import (
    ExecutionBlocked,
    ExecutionService,
    ManualReconciliationRequired,
)
from japan_agent.execute.t212 import BrokerTransportUncertain
from japan_agent.models import Portfolio, PortfolioSnapshot
from japan_agent.risk import ProposalBuilder
from japan_agent.storage import Database

from .helpers import NOW, decision, instrument, portfolio, settings, snapshot


class FakeBroker:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = 0

    def place_market_order(self, *, ticker, quantity, extended_hours=False):
        self.calls += 1
        if self.error:
            raise self.error
        return {"id": 1234, "ticker": ticker, "quantity": float(quantity), "status": "NEW"}

    def order(self, order_id):
        return {"id": order_id, "ticker": "TEST_EQ", "status": "FILLED"}


class ExecutionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = settings(self.root)
        self.settings.ensure_directories()
        self.settings.whitelist_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.whitelist_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "verified_at": "2026-08-28T11:00:00Z",
                    "account_environment": "demo",
                    "instruments": [
                        {
                            "ticker": "TEST_EQ",
                            "display_name": "Test Japan Technology ETF",
                            "sleeve": "CORE",
                            "instrument_type": "ETF",
                            "native_currency": "USD",
                            "max_position_fraction": "0.40",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.database = Database(self.settings.database_path)
        self.database.initialize()
        self.ticket = ProposalBuilder().build(
            decision=decision(),
            instrument=instrument(),
            snapshot=snapshot(),
            portfolio=portfolio(),
            submitted_trade_times=[],
            duplicate_open=False,
            now=NOW,
        )
        approvals = ApprovalService(self.database)
        approvals.submit(self.ticket)
        approvals.approve(
            self.ticket.proposal_id,
            approver="Bogdan",
            expected_hash=self.ticket.fingerprint,
            now=NOW + timedelta(minutes=1),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self, broker):
        return ExecutionService(database=self.database, broker=broker, settings=self.settings)

    def current_quote(self, price="10"):
        return snapshot(price_gbp=price, observed_at=NOW + timedelta(minutes=2))

    def current_portfolio(self, *, cash="100"):
        return PortfolioSnapshot(
            portfolio=Portfolio(cash_gbp=Decimal(cash), positions=()),
            observed_at=NOW + timedelta(minutes=2),
            source="TRADING212",
        )

    def test_submits_once_and_returns_stored_response_on_repeat(self) -> None:
        broker = FakeBroker()
        service = self.service(broker)
        first = service.execute(
            self.ticket.proposal_id,
            current_snapshot=self.current_quote(),
            portfolio_snapshot=self.current_portfolio(),
            now=NOW + timedelta(minutes=3),
        )
        second = service.execute(
            self.ticket.proposal_id,
            current_snapshot=self.current_quote(),
            portfolio_snapshot=self.current_portfolio(),
            now=NOW + timedelta(minutes=4),
        )
        self.assertEqual(first, second)
        self.assertEqual(broker.calls, 1)

    def test_unknown_transport_is_never_retried(self) -> None:
        broker = FakeBroker(BrokerTransportUncertain("timeout"))
        service = self.service(broker)
        with self.assertRaises(ManualReconciliationRequired):
            service.execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=3),
            )
        with self.assertRaises(ManualReconciliationRequired):
            service.execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=4),
            )
        self.assertEqual(broker.calls, 1)

    def test_killswitch_blocks_order(self) -> None:
        self.settings.killswitch_path.write_text("stop", encoding="utf-8")
        broker = FakeBroker()
        with self.assertRaises(ExecutionBlocked):
            self.service(broker).execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=3),
            )
        self.assertEqual(broker.calls, 0)

    def test_price_move_or_stale_execution_quote_blocks(self) -> None:
        with self.assertRaises(ExecutionBlocked):
            self.service(FakeBroker()).execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote("10.31"),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=3),
            )
        with self.assertRaises(ExecutionBlocked):
            self.service(FakeBroker()).execute(
                self.ticket.proposal_id,
                current_snapshot=snapshot(observed_at=NOW),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=30),
            )

    def test_fresh_portfolio_cash_change_blocks_submission(self) -> None:
        broker = FakeBroker()
        with self.assertRaises(ExecutionBlocked):
            self.service(broker).execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(cash="20"),
                now=NOW + timedelta(minutes=3),
            )
        self.assertEqual(broker.calls, 0)


if __name__ == "__main__":
    unittest.main()

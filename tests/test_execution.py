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
from japan_agent.execute.t212 import BrokerRejected, BrokerTransportUncertain
from japan_agent.models import Portfolio, PortfolioSnapshot, ProposalStatus
from japan_agent.risk import ProposalBuilder
from japan_agent.storage import Database

from .helpers import NOW, decision, instrument, portfolio, settings, snapshot


class FakeBroker:
    def __init__(
        self,
        error: Exception | None = None,
        response: dict | None = None,
        order_error: Exception | None = None,
        order_response: dict | None = None,
        history: list[dict] | None = None,
    ):
        self.error = error
        self.response = response
        self.order_error = order_error
        self.order_response = order_response
        self.history = history
        self.calls = 0

    def place_market_order(self, *, ticker, quantity, extended_hours=False):
        self.calls += 1
        if self.error:
            raise self.error
        if self.response is not None:
            return self.response
        return {"id": 1234, "ticker": ticker, "quantity": float(quantity), "status": "NEW"}

    def order(self, order_id):
        if self.order_error:
            raise self.order_error
        if self.order_response is not None:
            return self.order_response
        return {"id": order_id, "ticker": "TEST_EQ", "status": "FILLED"}

    def order_history(self, *, ticker=None, limit=50):
        return self.history or []


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

    def test_unknown_outcome_counts_toward_weekly_cap(self) -> None:
        service = self.service(FakeBroker(BrokerTransportUncertain("timeout")))
        with self.assertRaises(ManualReconciliationRequired):
            service.execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=3),
            )
        times = self.database.submitted_trade_times(NOW - timedelta(days=7))
        self.assertEqual(len(times), 1)

    def test_ambiguous_response_keeps_broker_order_id(self) -> None:
        broker = FakeBroker(response={"id": 777, "ticker": "OTHER_EQ", "status": "NEW"})
        with self.assertRaises(ManualReconciliationRequired):
            self.service(broker).execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=3),
            )
        claim = self.database.get_execution(self.ticket.proposal_id)
        self.assertEqual(claim.state, "UNKNOWN")
        self.assertEqual(claim.broker_order_id, "777")

    def test_mismatched_response_quantity_requires_reconciliation(self) -> None:
        broker = FakeBroker(
            response={"id": 555, "ticker": "TEST_EQ", "quantity": 999.0, "status": "NEW"}
        )
        with self.assertRaises(ManualReconciliationRequired):
            self.service(broker).execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=3),
            )
        claim = self.database.get_execution(self.ticket.proposal_id)
        self.assertEqual(claim.state, "UNKNOWN")
        self.assertEqual(claim.broker_order_id, "555")

    def test_terminal_status_in_submit_response_requires_reconciliation(self) -> None:
        broker = FakeBroker(
            response={
                "id": 556,
                "ticker": "TEST_EQ",
                "quantity": float(self.ticket.quantity),
                "status": "REJECTED",
            }
        )
        with self.assertRaises(ManualReconciliationRequired):
            self.service(broker).execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=3),
            )
        self.assertEqual(self.database.get_execution(self.ticket.proposal_id).state, "UNKNOWN")

    def test_expired_approved_ticket_is_marked_expired(self) -> None:
        broker = FakeBroker()
        with self.assertRaises(ExecutionBlocked):
            self.service(broker).execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(hours=25),
            )
        self.assertEqual(broker.calls, 0)
        stored = self.database.get_proposal(self.ticket.proposal_id)
        assert stored is not None
        self.assertIs(stored.status, ProposalStatus.EXPIRED)

    def _reach_unknown_with_order_id(self, broker: FakeBroker) -> None:
        broker.response = {"id": 555, "ticker": "TEST_EQ", "quantity": 999.0, "status": "NEW"}
        with self.assertRaises(ManualReconciliationRequired):
            self.service(broker).execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=3),
            )

    def test_reconcile_falls_back_to_history_for_terminal_orders(self) -> None:
        broker = FakeBroker(
            order_error=BrokerRejected(404, "not found"),
            history=[{"id": 555, "ticker": "TEST_EQ", "status": "FILLED"}],
        )
        self._reach_unknown_with_order_id(broker)
        self.service(broker).reconcile(self.ticket.proposal_id, now=NOW + timedelta(minutes=10))
        stored = self.database.get_proposal(self.ticket.proposal_id)
        assert stored is not None
        self.assertIs(stored.status, ProposalStatus.FILLED)
        self.assertEqual(self.database.get_execution(self.ticket.proposal_id).state, "FILLED")

    def test_reconcile_fails_closed_when_order_is_nowhere(self) -> None:
        broker = FakeBroker(order_error=BrokerRejected(404, "not found"), history=[])
        self._reach_unknown_with_order_id(broker)
        with self.assertRaises(ManualReconciliationRequired):
            self.service(broker).reconcile(
                self.ticket.proposal_id, now=NOW + timedelta(minutes=10)
            )
        self.assertEqual(self.database.get_execution(self.ticket.proposal_id).state, "UNKNOWN")

    def test_broker_rejection_is_terminal_and_never_retried(self) -> None:
        broker = FakeBroker(BrokerRejected(400, "bad request"))
        service = self.service(broker)
        with self.assertRaises(ExecutionBlocked):
            service.execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=3),
            )
        stored = self.database.get_proposal(self.ticket.proposal_id)
        assert stored is not None
        self.assertIs(stored.status, ProposalStatus.FAILED)
        kinds = [event["kind"] for event in self.database.iter_events()]
        self.assertIn("ORDER_REJECTED", kinds)
        with self.assertRaises(ExecutionBlocked):
            service.execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=4),
            )
        self.assertEqual(broker.calls, 1)

    def test_reconcile_terminal_without_fill_marks_failed(self) -> None:
        broker = FakeBroker(
            order_response={"id": 555, "ticker": "TEST_EQ", "status": "REJECTED"}
        )
        self._reach_unknown_with_order_id(broker)
        self.service(broker).reconcile(self.ticket.proposal_id, now=NOW + timedelta(minutes=10))
        self.assertEqual(self.database.get_execution(self.ticket.proposal_id).state, "FAILED")
        stored = self.database.get_proposal(self.ticket.proposal_id)
        assert stored is not None
        self.assertIs(stored.status, ProposalStatus.FAILED)
        kinds = [event["kind"] for event in self.database.iter_events()]
        self.assertIn("ORDER_TERMINAL_WITHOUT_FILL", kinds)

    def test_reconcile_unknown_broker_status_stays_submitted(self) -> None:
        broker = FakeBroker(
            order_response={"id": 555, "ticker": "TEST_EQ", "status": "PROCESSING"}
        )
        self._reach_unknown_with_order_id(broker)
        self.service(broker).reconcile(self.ticket.proposal_id, now=NOW + timedelta(minutes=10))
        self.assertEqual(self.database.get_execution(self.ticket.proposal_id).state, "SUBMITTED")

    def test_reconcile_without_order_id_fails_closed(self) -> None:
        broker = FakeBroker(BrokerTransportUncertain("timeout"))
        with self.assertRaises(ManualReconciliationRequired):
            self.service(broker).execute(
                self.ticket.proposal_id,
                current_snapshot=self.current_quote(),
                portfolio_snapshot=self.current_portfolio(),
                now=NOW + timedelta(minutes=3),
            )
        with self.assertRaises(ManualReconciliationRequired):
            self.service(broker).reconcile(
                self.ticket.proposal_id, now=NOW + timedelta(minutes=10)
            )

    def test_reconcile_non_404_failure_propagates_without_state_change(self) -> None:
        broker = FakeBroker(order_error=BrokerRejected(401, "unauthorized"))
        self._reach_unknown_with_order_id(broker)
        with self.assertRaises(BrokerRejected):
            self.service(broker).reconcile(
                self.ticket.proposal_id, now=NOW + timedelta(minutes=10)
            )
        self.assertEqual(self.database.get_execution(self.ticket.proposal_id).state, "UNKNOWN")

    def test_reconcile_is_get_only_and_survives_killswitch(self) -> None:
        # The kill switch is a submission interlock; read-only reconciliation
        # must keep working during an incident and must never POST.
        broker = FakeBroker()
        self._reach_unknown_with_order_id(broker)
        posts_before = broker.calls
        self.settings.killswitch_path.write_text("stop", encoding="utf-8")
        self.service(broker).reconcile(self.ticket.proposal_id, now=NOW + timedelta(minutes=10))
        self.assertEqual(broker.calls, posts_before)
        stored = self.database.get_proposal(self.ticket.proposal_id)
        assert stored is not None
        self.assertIs(stored.status, ProposalStatus.FILLED)

    def test_reconcile_marks_fill_from_pending_endpoint(self) -> None:
        broker = FakeBroker()
        self._reach_unknown_with_order_id(broker)
        self.service(broker).reconcile(self.ticket.proposal_id, now=NOW + timedelta(minutes=10))
        stored = self.database.get_proposal(self.ticket.proposal_id)
        assert stored is not None
        self.assertIs(stored.status, ProposalStatus.FILLED)

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

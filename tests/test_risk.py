from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from japan_agent.models import Action, Portfolio, Position, Sleeve
from japan_agent.risk import ProposalBuilder, RiskRejected

from .helpers import NOW, decision, instrument, portfolio, snapshot


class ProposalBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = ProposalBuilder()

    def build(self, **overrides):
        values = {
            "decision": decision(),
            "instrument": instrument(),
            "snapshot": snapshot(),
            "portfolio": portfolio(),
            "submitted_trade_times": [],
            "duplicate_open": False,
            "now": NOW,
        }
        values.update(overrides)
        return self.builder.build(**values)

    def codes(self, error: RiskRejected) -> set[str]:
        return {item.code for item in error.violations}

    def test_quantity_is_frozen_to_four_decimal_places(self) -> None:
        ticket = self.build(decision=decision(target="0.3333"), snapshot=snapshot(price_gbp="13"))
        self.assertEqual(ticket.quantity, Decimal("2.5638"))
        self.assertEqual(ticket.estimated_value_gbp, Decimal("33.33"))

    def test_rejects_position_above_forty_percent(self) -> None:
        with self.assertRaises(RiskRejected) as raised:
            self.build(decision=decision(target="0.50"))
        self.assertIn("POSITION_LIMIT", self.codes(raised.exception))

    def test_rejects_satellite_above_fifteen_pounds(self) -> None:
        with self.assertRaises(RiskRejected) as raised:
            self.build(
                decision=decision(target="0.20"),
                instrument=instrument(sleeve=Sleeve.SATELLITE),
            )
        self.assertIn("ABSOLUTE_POSITION_LIMIT", self.codes(raised.exception))

    def test_rejects_cash_floor_breach(self) -> None:
        low_cash = Portfolio(
            cash_gbp=Decimal("6"),
            positions=(
                Position("TEST_EQ", Decimal("2"), Decimal("20")),
                Position("OTHER_EQ", Decimal("7.4"), Decimal("74")),
            ),
        )
        with self.assertRaises(RiskRejected) as raised:
            self.build(decision=decision(target="0.40"), portfolio=low_cash)
        self.assertIn("CASH_FLOOR", self.codes(raised.exception))

    def test_rejects_stale_snapshot(self) -> None:
        with self.assertRaises(RiskRejected) as raised:
            self.build(snapshot=snapshot(observed_at=NOW - timedelta(hours=73)))
        self.assertIn("PRICE_STALE", self.codes(raised.exception))

    def test_rejects_missing_whitelist_and_price(self) -> None:
        with self.assertRaises(RiskRejected) as raised:
            self.build(instrument=None, snapshot=None)
        self.assertEqual(self.codes(raised.exception), {"NOT_WHITELISTED", "MISSING_PRICE"})

    def test_rejects_weekly_limit_and_duplicate(self) -> None:
        with self.assertRaises(RiskRejected) as raised:
            self.build(
                submitted_trade_times=[NOW - timedelta(days=i) for i in range(3)],
                duplicate_open=True,
            )
        self.assertIn("WEEKLY_TRADE_LIMIT", self.codes(raised.exception))
        self.assertIn("DUPLICATE_OPEN", self.codes(raised.exception))

    def test_sell_is_negative_and_cannot_exceed_holding(self) -> None:
        ticket = self.build(
            decision=decision(action=Action.SELL, target="0.10"),
            portfolio=portfolio(cash="50", quantity="5", position_value="50"),
        )
        self.assertEqual(ticket.quantity, Decimal("-4.0000"))


if __name__ == "__main__":
    unittest.main()

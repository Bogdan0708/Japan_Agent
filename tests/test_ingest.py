from __future__ import annotations

import unittest
from decimal import Decimal

from japan_agent.ingest import normalize_price_gbp


class PriceNormalizationTests(unittest.TestCase):
    def test_gbx_is_pence(self) -> None:
        self.assertEqual(normalize_price_gbp(Decimal("523.5"), "GBX"), Decimal("5.235"))

    def test_usd_requires_explicit_fx(self) -> None:
        with self.assertRaises(ValueError):
            normalize_price_gbp(Decimal("100"), "USD")
        self.assertEqual(
            normalize_price_gbp(
                Decimal("100"), "USD", gbp_per_native_unit=Decimal("0.75")
            ),
            Decimal("75.00"),
        )


if __name__ == "__main__":
    unittest.main()


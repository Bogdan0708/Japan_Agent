"""Paper fills: walk a recorded ask book (spec §5 steps 4–5). Pure, no I/O.

Fees are charged per level: shares x rate x price x (1 - price), never at the average
price. Prices must sit on the book's tick grid; share quantities are rounded down to the
venue's share precision. The walk never goes past the limit price or the recorded depth."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .tickets import Fill

# Share precision: CLOB book sizes are served with 2 decimal places (Plan 1 contract fact 4).
SHARE_QUANTUM = Decimal("0.01")


class FillError(ValueError):
    """A book the walk cannot use. `code` is the refusal reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class AskLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class Walk:
    fills: tuple[Fill, ...]
    shares: Decimal
    notional: Decimal
    fee: Decimal

    @property
    def cost(self) -> Decimal:
        return self.notional + self.fee

    @property
    def cost_per_share(self) -> Decimal:
        return self.cost / self.shares

    @property
    def average_price(self) -> Decimal:
        return self.notional / self.shares


def fee_per_share(price: Decimal, rate: Decimal) -> Decimal:
    return rate * price * (Decimal("1") - price)


def level_fee(shares: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    return shares * fee_per_share(price, rate)


def round_shares(quantity: Decimal) -> Decimal:
    return quantity.quantize(SHARE_QUANTUM, rounding=ROUND_DOWN)


def sorted_asks(asks: Sequence[AskLevel], tick_size: Decimal) -> tuple[AskLevel, ...]:
    """Best (lowest) price first, whatever order the input came in. Refuses levels off the
    tick grid or outside (0, 1), and non-positive sizes."""
    if tick_size <= 0:
        raise FillError("TICK_MISMATCH", f"tick size {tick_size}")
    for level in asks:
        if not Decimal("0") < level.price < Decimal("1") or level.size <= 0:
            raise FillError("BAD_BOOK", f"level {level.price} x {level.size}")
        if level.price % tick_size != 0:
            raise FillError("TICK_MISMATCH", f"price {level.price} is off tick {tick_size}")
    return tuple(sorted(asks, key=lambda level: level.price))


def walk_asks(
    asks: Sequence[AskLevel],
    *,
    budget: Decimal,
    fee_rate: Decimal,
    limit_price: Decimal,
    tick_size: Decimal,
) -> Walk:
    """Spend at most `budget` (fees included) buying from the cheapest level up, never above
    `limit_price` and never more than each level's recorded size. May return zero shares."""
    if budget < 0 or not budget.is_finite():
        raise FillError("BAD_BUDGET", f"budget {budget}")
    if not Decimal("0") <= fee_rate < Decimal("1"):
        raise FillError("FEE_UNKNOWN", f"fee rate {fee_rate}")
    remaining = budget
    fills: list[Fill] = []
    notional = Decimal("0")
    fee = Decimal("0")
    for level in sorted_asks(asks, tick_size):
        if level.price > limit_price:
            break
        unit = level.price + fee_per_share(level.price, fee_rate)
        take = min(round_shares(level.size), round_shares(remaining / unit))
        if take <= 0:
            break
        level_cost = take * level.price
        level_fees = level_fee(take, level.price, fee_rate)
        fills.append(Fill(level.price, take))
        notional += level_cost
        fee += level_fees
        remaining -= level_cost + level_fees
    shares = sum((fill.shares for fill in fills), Decimal("0"))
    return Walk(tuple(fills), shares, notional, fee)

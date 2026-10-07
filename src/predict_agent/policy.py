"""The paper policy (spec §5): forecast + post-forecast books + ledger state -> one trade
or one refusal. Pure: no I/O, no clock; every input arrives in PolicyInputs.

Sizing is decided before depth: budget = min(kelly_fraction x Kelly(q, a) x equity, every
cap's headroom, available cash) at the best ask `a`; the book is then walked up to a limit
price of a x (1 + max_slippage), fees included, and the realized edge q - c is rechecked."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .fills import AskLevel, FillError, Walk, fee_per_share, walk_asks
from .gamma import fee_rate
from .policy_params import CONFIDENCE_RANK, PolicyParams

ONE = Decimal("1")
ZERO = Decimal("0")


@dataclass(frozen=True)
class SideBook:
    """One token's post-forecast snapshot, as stored."""

    outcome: str  # "YES" | "NO"
    snapshot_id: int
    fetched_at: datetime
    asks: tuple[AskLevel, ...]
    tick_size: Decimal
    min_order_size: Decimal
    fees_enabled: bool
    fee_schedule: Mapping[str, object] | None


@dataclass(frozen=True)
class ForecastView:
    abstained: bool
    p_low: Decimal | None
    p_mid: Decimal | None
    p_high: Decimal | None
    confidence: str | None
    rules_hash: str
    end_date: datetime | None  # from the forecast's own rules version


@dataclass(frozen=True)
class Exposure:
    """Cost basis of this portfolio's OPEN tickets, grouped as the caps are."""

    market: Decimal
    event: Decimal
    category: Decimal
    total: Decimal


@dataclass(frozen=True)
class PolicyInputs:
    forecast: ForecastView
    current_rules_hash: str
    eligible: bool  # in the latest completed discovery run
    resolution_started: bool  # an observation's status is no longer open (resolution.OPEN_STATUSES)
    already_traded: bool  # this portfolio holds or held a ticket on the market
    books: Mapping[str, SideBook]  # "YES" and "NO"
    equity: Decimal
    available_cash: Decimal
    exposure: Exposure
    now: datetime


@dataclass(frozen=True)
class Trade:
    outcome: str
    snapshot_id: int
    q: Decimal
    best_ask: Decimal
    budget: Decimal
    walk: Walk

    @property
    def edge(self) -> Decimal:
        return self.q - self.walk.cost_per_share


@dataclass(frozen=True)
class Refusal:
    reason: str
    detail: str


def kelly(q: Decimal, price: Decimal) -> Decimal:
    return (q - price) / (ONE - price)


def side_probabilities(forecast: ForecastView, source: str) -> dict[str, Decimal]:
    """q per side: conservative bounds for `bounds`, p_mid for `mid`."""
    if forecast.p_low is None or forecast.p_mid is None or forecast.p_high is None:
        raise ValueError("an abstained forecast has no side probabilities")
    if source == "bounds":
        return {"YES": forecast.p_low, "NO": ONE - forecast.p_high}
    return {"YES": forecast.p_mid, "NO": ONE - forecast.p_mid}


def _headroom(cap: Decimal, equity: Decimal, used: Decimal) -> Decimal:
    return max(ZERO, cap * equity - used)


def budget_for(q: Decimal, ask: Decimal, inputs: PolicyInputs, params: PolicyParams) -> Decimal:
    exposure = inputs.exposure
    return max(
        ZERO,
        min(
            params.kelly_fraction * kelly(q, ask) * inputs.equity,
            _headroom(params.cap_market, inputs.equity, exposure.market),
            _headroom(params.cap_event, inputs.equity, exposure.event),
            _headroom(params.cap_category, inputs.equity, exposure.category),
            _headroom(params.cap_total_open, inputs.equity, exposure.total),
            inputs.available_cash,
        ),
    )


def _gate(inputs: PolicyInputs, params: PolicyParams) -> Refusal | None:
    """Refusals that do not depend on the book walk (spec §5 step 1, plus freshness)."""
    forecast = inputs.forecast
    if forecast.abstained:
        return Refusal("ABSTAINED", "forecast abstained")
    if forecast.rules_hash != inputs.current_rules_hash:
        return Refusal("RULES_CHANGED", "rules version changed since the forecast")
    if not inputs.eligible:
        return Refusal("ELIGIBILITY_LOST", "not in the latest completed discovery run")
    if inputs.resolution_started:
        return Refusal("RESOLUTION_STARTED", "a resolution observation is no longer open")
    if inputs.already_traded:
        return Refusal("ALREADY_TRADED", "one entry per market per portfolio, ever")
    if CONFIDENCE_RANK.get(forecast.confidence or "", -1) < CONFIDENCE_RANK[
        params.min_confidence
    ]:
        return Refusal("LOW_CONFIDENCE", f"confidence {forecast.confidence}")
    if forecast.end_date is None or forecast.end_date - inputs.now < timedelta(
        hours=params.min_hours_to_close
    ):
        return Refusal("CLOSING_SOON", f"end date {forecast.end_date}")
    if set(inputs.books) != {"YES", "NO"}:
        return Refusal("NO_BOOK", "both post-forecast snapshots are required")
    for book in inputs.books.values():
        age = inputs.now - book.fetched_at
        if age > timedelta(seconds=params.max_book_age_seconds) or age < timedelta(0):
            return Refusal("STALE_BOOK", f"{book.outcome} book age {age}")
        if book.fees_enabled and fee_rate(book.fee_schedule) is None:
            return Refusal("FEE_UNKNOWN", f"{book.outcome} book has fees but no fee rate")
    return None


def _rate(book: SideBook) -> Decimal:
    if not book.fees_enabled:
        return ZERO
    rate = fee_rate(book.fee_schedule)
    if rate is None:  # unreachable: _gate refuses it
        raise ValueError("fee rate missing")
    return rate


def _try_side(
    book: SideBook, q: Decimal, inputs: PolicyInputs, params: PolicyParams
) -> Trade | Refusal:
    if not book.asks:
        return Refusal("NO_DEPTH", f"{book.outcome} book has no asks")
    rate = _rate(book)
    best_ask = min(level.price for level in book.asks)
    provisional = q - best_ask - fee_per_share(best_ask, rate)
    if provisional < params.min_edge:
        return Refusal("NO_EDGE", f"{book.outcome} provisional edge {provisional}")
    budget = budget_for(q, best_ask, inputs, params)
    if budget <= 0:
        return Refusal("NO_BUDGET", f"{book.outcome} cap headroom or cash exhausted")
    try:
        walk = walk_asks(
            book.asks,
            budget=budget,
            fee_rate=rate,
            limit_price=best_ask * (ONE + params.max_slippage),
            tick_size=book.tick_size,
        )
    except FillError as error:
        return Refusal(error.code, f"{book.outcome}: {error}")
    if walk.shares <= 0:
        return Refusal("NO_DEPTH", f"{book.outcome} budget {budget} buys no shares")
    # The minimum order size's unit is contradictory in the docs (shares vs USDC):
    # satisfy both readings.
    if walk.shares < book.min_order_size or walk.cost < book.min_order_size:
        return Refusal(
            "BELOW_MIN_ORDER",
            f"{book.outcome} {walk.shares} shares / {walk.cost} below {book.min_order_size}",
        )
    trade = Trade(book.outcome, book.snapshot_id, q, best_ask, budget, walk)
    if trade.edge < params.min_edge:
        return Refusal("NO_EDGE_AFTER_FILL", f"{book.outcome} realized edge {trade.edge}")
    return trade


def decide(inputs: PolicyInputs, params: PolicyParams) -> Trade | Refusal:
    """At most one side per market: the side with the larger realized edge."""
    refusal = _gate(inputs, params)
    if refusal is not None:
        return refusal
    q_by_side = side_probabilities(inputs.forecast, params.probability)
    results = [
        _try_side(inputs.books[outcome], q_by_side[outcome], inputs, params)
        for outcome in ("YES", "NO")
    ]
    trades = [result for result in results if isinstance(result, Trade)]
    if trades:
        return max(trades, key=lambda trade: (trade.edge, trade.outcome == "YES"))
    refusals = [result for result in results if isinstance(result, Refusal)]
    # A side with no edge at all says least: report the first refusal that is not NO_EDGE
    # (YES is checked first); if both are NO_EDGE, report the first.
    telling = [r for r in refusals if r.reason != "NO_EDGE"] or refusals
    return Refusal(telling[0].reason, "; ".join(f"{r.reason}: {r.detail}" for r in refusals))

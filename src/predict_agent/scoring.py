"""Forecast scoring and resampling for the performance report (spec §8). Pure: no I/O,
no clock.

Brier scores are exact Decimals. The log score is the natural log of the probability given
to what happened (higher is better, at most 0), with every probability clipped to
[0.01, 0.99] first, for all forecasters alike; it is a statistic, not money, so it is a
float. The bootstrap resamples whole groups (events) with replacement and a fixed seed, so
a report is reproducible from the same database."""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

CLIP_LOW = Decimal("0.01")
CLIP_HIGH = Decimal("0.99")
BUCKETS = 10
ONE = Decimal("1")

Pair = tuple[Decimal, int]  # (probability of YES, outcome: 1 for YES, 0 for NO)


@dataclass(frozen=True)
class ScoreSummary:
    n: int
    brier: Decimal  # mean squared error, lower is better
    log: float  # mean log probability of the outcome, higher is better


@dataclass(frozen=True)
class Bucket:
    lower: Decimal
    upper: Decimal
    n: int
    mean_forecast: Decimal | None
    observed_rate: Decimal | None


def clip(p: Decimal) -> Decimal:
    return min(max(p, CLIP_LOW), CLIP_HIGH)


def brier(p: Decimal, outcome: int) -> Decimal:
    return (p - Decimal(outcome)) ** 2


def log_score(p: Decimal, outcome: int) -> float:
    q = clip(p)
    return math.log(float(q if outcome else ONE - q))


def summarize(pairs: Sequence[Pair]) -> ScoreSummary | None:
    """Mean Brier and log score over `pairs`; None when there are none."""
    if not pairs:
        return None
    n = len(pairs)
    return ScoreSummary(
        n=n,
        brier=sum((brier(p, y) for p, y in pairs), Decimal(0)) / Decimal(n),
        log=sum(log_score(p, y) for p, y in pairs) / n,
    )


def bucket_index(p: Decimal) -> int:
    """0 for [0, 0.1), ..., 9 for [0.9, 1.0] (1.0 falls in the top bucket)."""
    index = int((p * BUCKETS).to_integral_value(rounding=ROUND_FLOOR))
    return min(max(index, 0), BUCKETS - 1)


def calibration(pairs: Sequence[Pair]) -> list[Bucket]:
    """Ten equal-width buckets by forecast probability: how many forecasts fell in each,
    their mean forecast and how often YES happened."""
    grouped: list[list[Pair]] = [[] for _ in range(BUCKETS)]
    for p, y in pairs:
        grouped[bucket_index(p)].append((p, y))
    table = []
    for index, members in enumerate(grouped):
        n = len(members)
        table.append(
            Bucket(
                lower=Decimal(index) / BUCKETS,
                upper=Decimal(index + 1) / BUCKETS,
                n=n,
                mean_forecast=(sum((p for p, _ in members), Decimal(0)) / n) if n else None,
                observed_rate=(Decimal(sum(y for _, y in members)) / n) if n else None,
            )
        )
    return table


def bootstrap_interval(
    groups: Mapping[str, Sequence[Decimal]],
    *,
    resamples: int = 1000,
    seed: int = 0,
    level: Decimal = Decimal("0.95"),
) -> tuple[Decimal, Decimal] | None:
    """Percentile interval for the total of all values, resampling whole groups with
    replacement. A heuristic: one group (event) does not prove independence. None when
    there are no groups."""
    if not groups:
        return None
    keys = sorted(groups)
    totals = {key: sum(groups[key], Decimal(0)) for key in keys}
    rng = random.Random(seed)
    sums = sorted(
        sum((totals[key] for key in rng.choices(keys, k=len(keys))), Decimal(0))
        for _ in range(resamples)
    )
    tail = (ONE - level) / 2
    low = int((tail * resamples).to_integral_value(rounding=ROUND_FLOOR))
    high = resamples - 1 - low
    return sums[low], sums[high]

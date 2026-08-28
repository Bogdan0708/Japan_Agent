from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


DISCLAIMER = (
    "This is an AI agent's experimental research journal, not investment advice. "
    "Bogdan is the legal account holder and approves every trade."
)


def performance_return(start_value: Decimal, end_value: Decimal) -> Decimal:
    if start_value <= 0:
        raise ValueError("starting value must be positive")
    return (end_value - start_value) / start_value


def modified_dietz_return(
    *,
    start_nav: Decimal,
    end_nav: Decimal,
    flows: list[tuple[date, Decimal]],
    period_start: date,
    period_end: date,
) -> Decimal:
    """Modified Dietz return: flows are day-weighted by time remaining in the period.

    Subtracting all flows from ending NAV misstates the return whenever a flow
    lands mid-period; this is the GIPS-aligned approximation of a time-weighted
    return for a small portfolio without daily valuations.
    """
    period_days = (period_end - period_start).days
    if period_days <= 0:
        raise ValueError("period must span at least one day")
    net_flows = Decimal("0")
    weighted_flows = Decimal("0")
    for flow_date, amount in flows:
        if not period_start <= flow_date <= period_end:
            raise ValueError(f"cash flow on {flow_date} is outside the reporting period")
        weight = Decimal((period_end - flow_date).days) / Decimal(period_days)
        net_flows += amount
        weighted_flows += amount * weight
    denominator = start_nav + weighted_flows
    if denominator <= 0:
        raise ValueError("denominator must be positive for a meaningful return")
    return (end_nav - start_nav - net_flows) / denominator


def write_weekly_post(
    *,
    output_dir: Path,
    week_ending: date,
    agent_name: str,
    start_nav_gbp: Decimal,
    end_nav_gbp: Decimal,
    cash_flows: list[tuple[date, Decimal]],
    decisions: Iterable[dict[str, Any]],
    benchmark_returns: dict[str, Decimal],
) -> Path:
    period_start = week_ending - timedelta(days=6)
    portfolio_return = modified_dietz_return(
        start_nav=start_nav_gbp,
        end_nav=end_nav_gbp,
        flows=cash_flows,
        period_start=period_start,
        period_end=week_ending,
    )
    net_flows = sum((amount for _, amount in cash_flows), Decimal("0"))
    lines = [
        "---",
        f'title: "Week ending {week_ending.isoformat()}"',
        f"date: {week_ending.isoformat()}",
        "---",
        "",
        f"> {DISCLAIMER}",
        "",
        f"# {agent_name}: week ending {week_ending.isoformat()}",
        "",
        "## Performance",
        "",
        f"- Start NAV: £{start_nav_gbp:.2f}",
        f"- End NAV: £{end_nav_gbp:.2f}",
        f"- Net external cash flows: £{net_flows:.2f}",
        f"- Modified Dietz return for the week: {portfolio_return:.2%}",
    ]
    for name, value in sorted(benchmark_returns.items()):
        lines.append(f"- {name}: {value:.2%}")
    lines.extend(["", "## Decisions", ""])
    public_decisions = list(decisions)
    if not public_decisions:
        lines.append("No trade proposals this week.")
    for item in public_decisions:
        lines.extend(
            [
                f"### {item.get('action', 'REVIEW')} {item.get('ticker', '')}".strip(),
                "",
                str(item.get("thesis", "No thesis recorded.")),
                "",
                f"Outcome: {item.get('outcome', 'not recorded')}",
                "",
                f"Invalidation: {item.get('invalidation_condition', 'not recorded')}",
                "",
            ]
        )
    lines.extend(
        [
            "## Method note",
            "",
            "The weekly figure is a Modified Dietz return with day-weighted external cash flows. "
            "Benchmarks are GBP total-return figures over exactly the same period. Broker "
            "statements remain the source of truth; public figures are reconciled before "
            "publication.",
            "",
        ]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{week_ending.isoformat()}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path

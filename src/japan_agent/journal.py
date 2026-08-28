from __future__ import annotations

from datetime import date
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


def write_weekly_post(
    *,
    output_dir: Path,
    week_ending: date,
    agent_name: str,
    start_nav_gbp: Decimal,
    end_nav_gbp: Decimal,
    cash_flows_gbp: Decimal,
    decisions: Iterable[dict[str, Any]],
    benchmark_returns: dict[str, Decimal],
) -> Path:
    adjusted_end = end_nav_gbp - cash_flows_gbp
    portfolio_return = performance_return(start_nav_gbp, adjusted_end)
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
        f"- External cash flows: £{cash_flows_gbp:.2f}",
        f"- Cash-flow-adjusted simple return for the week: {portfolio_return:.2%}",
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
            "Returns exclude recorded external cash flows. Broker statements remain the source of "
            "truth; public figures are reconciled before publication.",
            "",
        ]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{week_ending.isoformat()}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path

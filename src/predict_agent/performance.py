"""The performance report (spec §8): per cohort, never pooled.

Scoring population (primary): one row per market per cohort, the first `entry` forecast,
not abstained, with a baseline snapshot, on a market whose settled outcome is YES or NO
(`settlement.final_outcome`, the rule settlement itself uses). Claude's p_mid, the market
baseline (YES mid of the post-forecast snapshot) and the base rate are scored on exactly
these rows. Exposure-flagged forecasts are scored as a separate subset; rules-changed
markets are left out of forecast scoring (their P&L always counts). HALF outcomes,
unresolved markets, NO_TIMELY_BASELINE and abstentions are counted, not scored.

Paper P&L comes from the cash ledger and settlements per portfolio; the shadow p_mid
portfolio is shown alongside, labelled secondary. Read-only: nothing here writes."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .artifacts import load_artifact
from .cash import available_cash, parse_money
from .paper import latest_discovery_run
from .policy import ForecastView, side_probabilities
from .policy_params import policy_from_artifact
from .report import shortlist
from .scoring import Pair, ScoreSummary, bootstrap_interval, calibration, summarize
from .settlement import final_outcome, resolved_since
from .tickets import open_cost
from .util import isoformat, parse_datetime

FORECASTERS = ("claude", "market", "base_rate")
OVERDUE = timedelta(days=14)  # spec §7: unresolved this long after the end date is surfaced
GUIDANCE = (
    "Guidance, not a gate: expect 4-8 weeks of data, longer while few events have "
    "resolved. There is no automated go/no-go."
)


@dataclass(frozen=True)
class ScoredRow:
    """One forecast on a market with a YES/NO outcome, with the three forecasters' YES
    probabilities."""

    condition_id: str
    event_id: str
    category: str
    horizon: str
    width: str
    outcome: int
    claude: Decimal
    market: Decimal
    base_rate: Decimal


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _score(summary: ScoreSummary | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {"n": summary.n, "brier": _text(summary.brier), "log": round(summary.log, 4)}


def horizon_bucket(created_at: datetime, end_date: datetime) -> str:
    days = (end_date - created_at).total_seconds() / 86400
    return "<7d" if days < 7 else "7-30d" if days <= 30 else ">30d"


def width_bucket(p_low: Decimal, p_high: Decimal) -> str:
    width = p_high - p_low
    return "<=0.10" if width <= Decimal("0.10") else "0.10-0.20" if width <= Decimal(
        "0.20") else ">0.20"


def yes_mid(conn: sqlite3.Connection, snapshot_id: int) -> Decimal:
    record = json.loads(
        conn.execute("SELECT record_json FROM book_snapshots WHERE id = ?", (snapshot_id,))
        .fetchone()["record_json"]
    )
    return (Decimal(record["bids"][0][0]) + Decimal(record["asks"][0][0])) / 2


def scores(rows: Sequence[ScoredRow]) -> dict[str, Any]:
    """Each forecaster scored on exactly the same rows."""
    return {
        name: _score(summarize([(getattr(row, name), row.outcome) for row in rows]))
        for name in FORECASTERS
    }


def grouped_scores(rows: Sequence[ScoredRow], key: Callable[[ScoredRow], str]) -> dict[str, Any]:
    groups: dict[str, list[ScoredRow]] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return {name: scores(members) for name, members in sorted(groups.items())}


def _outcomes(conn: sqlite3.Connection, condition_ids: Iterable[str]) -> dict[str, str | None]:
    found = {}
    for condition_id in set(condition_ids):
        rows = conn.execute(
            "SELECT * FROM resolution_observations WHERE condition_id = ? ORDER BY id",
            (condition_id,),
        ).fetchall()
        found[condition_id] = final_outcome(rows)
    return found


def _forecasts(conn: sqlite3.Connection, cohort_id: str, kind: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.*, m.category, m.event_id, m.current_rules_hash, r.rules_json, "
        "b.yes_snapshot_id, b.reason AS baseline_reason, b.forecast_id AS baseline_row "
        "FROM forecasts f JOIN markets m ON m.condition_id = f.condition_id "
        "JOIN rules_versions r ON r.condition_id = f.condition_id "
        "AND r.rules_hash = f.rules_hash "
        "LEFT JOIN forecast_baselines b ON b.forecast_id = f.forecast_id "
        "WHERE f.cohort_id = ? AND f.kind = ? ORDER BY f.forecast_id",
        (cohort_id, kind),
    ).fetchall()


def _scored(conn: sqlite3.Connection, row: sqlite3.Row, outcome: str) -> ScoredRow:
    created = parse_datetime(row["created_at"])
    end_date = parse_datetime(json.loads(row["rules_json"])["end_date"])
    return ScoredRow(
        condition_id=row["condition_id"],
        event_id=row["event_id"],
        category=row["category"],
        horizon=horizon_bucket(created, end_date),
        width=width_bucket(Decimal(row["p_low"]), Decimal(row["p_high"])),
        outcome=1 if outcome == "YES" else 0,
        claude=Decimal(row["p_mid"]),
        market=yes_mid(conn, row["yes_snapshot_id"]),
        base_rate=Decimal(row["base_rate"]),
    )


COUNT_KEYS = ("forecasts", "abstained", "awaiting_baseline", "no_timely_baseline",
              "unresolved", "half", "rules_changed", "exposure_flagged")


def population(
    conn: sqlite3.Connection, rows: Sequence[sqlite3.Row], outcomes: dict[str, str | None]
) -> tuple[Counter[str], list[ScoredRow], list[ScoredRow]]:
    """(counts, primary rows, exposure-flagged rows). The same exclusions apply to entry
    and update forecasts: abstained, no baseline yet, NO_TIMELY_BASELINE, unresolved, HALF
    and rules-changed rows are counted, never scored; flagged rows are scored apart."""
    counts: Counter[str] = Counter()
    primary: list[ScoredRow] = []
    flagged: list[ScoredRow] = []
    for row in rows:
        counts["forecasts"] += 1
        if row["abstained"]:
            counts["abstained"] += 1
            continue
        if row["baseline_row"] is None:
            counts["awaiting_baseline"] += 1
            continue
        if row["baseline_reason"] is not None:
            counts["no_timely_baseline"] += 1
            continue
        outcome = outcomes.get(row["condition_id"])
        if outcome == "HALF":
            counts["half"] += 1
            continue
        if outcome not in ("YES", "NO"):
            counts["unresolved"] += 1
            continue
        if row["current_rules_hash"] != row["rules_hash"]:
            counts["rules_changed"] += 1
            continue
        scored = _scored(conn, row, outcome)
        if json.loads(row["body_json"])["exposure_flags"]:
            counts["exposure_flagged"] += 1
            flagged.append(scored)
        else:
            primary.append(scored)
    return counts, primary, flagged


def forecast_section(
    conn: sqlite3.Connection, cohort_id: str, outcomes: dict[str, str | None]
) -> dict[str, Any]:
    """Counts first, then scores on the primary population and the flagged subset."""
    attempts = conn.execute(
        "SELECT status, error FROM research_attempts WHERE cohort_id = ? AND kind = 'entry'",
        (cohort_id,),
    ).fetchall()
    counts, primary, flagged = population(conn, _forecasts(conn, cohort_id, "entry"), outcomes)
    failed = Counter(row["error"] for row in attempts if row["status"] == "FAILED")
    claude_pairs: list[Pair] = [(row.claude, row.outcome) for row in primary]
    return {
        "counts": {
            "entry_attempts": len(attempts),
            "failed_attempts": dict(sorted(failed.items())),
            "entry_forecasts": counts["forecasts"],
            **{key: counts[key] for key in COUNT_KEYS[1:]},
            "abstention_rate": _text(
                Decimal(counts["abstained"]) / len(attempts) if attempts else None
            ),
            "scoring_rows": len(primary),
            "distinct_events": len({row.event_id for row in primary}),
        },
        "scores": scores(primary),
        "by_category": grouped_scores(primary, lambda row: row.category),
        "by_horizon": grouped_scores(primary, lambda row: row.horizon),
        "by_range_width": grouped_scores(primary, lambda row: row.width),
        "calibration": [
            {
                "bucket": f"{_text(bucket.lower)}-{_text(bucket.upper)}",
                "n": bucket.n,
                "mean_forecast": _text(bucket.mean_forecast),
                "observed_rate": _text(bucket.observed_rate),
            }
            for bucket in calibration(claude_pairs)
        ],
        "exposure_flagged_scores": scores(flagged),
    }


def update_section(
    conn: sqlite3.Connection, cohort_id: str, outcomes: dict[str, str | None]
) -> dict[str, Any]:
    """Weekly update forecasts, scored separately (spec §5, §8) with the same exclusions
    as entries; they never trade."""
    counts, primary, flagged = population(conn, _forecasts(conn, cohort_id, "update"),
                                          outcomes)
    return {
        "counts": {
            "update_forecasts": counts["forecasts"],
            **{key: counts[key] for key in COUNT_KEYS[1:]},
            "scoring_rows": len(primary),
        },
        "scores": scores(primary),
        "exposure_flagged_scores": scores(flagged),
    }


def _edges(
    conn: sqlite3.Connection, ticket: sqlite3.Row, probability: str
) -> tuple[Decimal, Decimal | None]:
    """(edge at entry, realized edge per share): q of the held side under the portfolio's
    probability source minus the per-share cost, and the settled payout per share minus
    the same cost (None while open)."""
    forecast = conn.execute(
        "SELECT * FROM forecasts WHERE forecast_id = ?", (ticket["forecast_id"],)
    ).fetchone()
    view = ForecastView(
        abstained=False,
        p_low=Decimal(forecast["p_low"]),
        p_mid=Decimal(forecast["p_mid"]),
        p_high=Decimal(forecast["p_high"]),
        confidence=forecast["confidence"],
        rules_hash=forecast["rules_hash"],
        end_date=None,
    )
    per_share = parse_money(ticket["cost_total"]) / parse_money(ticket["shares"])
    q = side_probabilities(view, probability)[ticket["outcome"]]
    realized = (
        None
        if ticket["payout_per_share"] is None
        else parse_money(ticket["payout_per_share"]) - per_share
    )
    return q - per_share, realized


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def portfolio_section(
    conn: sqlite3.Connection, portfolio: sqlite3.Row, research_cost: Decimal
) -> dict[str, Any]:
    policy = policy_from_artifact(load_artifact(conn, portfolio["policy_hash"])[1])
    tickets = conn.execute(
        "SELECT t.*, s.net_pnl, s.payout_per_share, m.event_id FROM paper_tickets t "
        "LEFT JOIN settlements s ON s.ticket_id = t.ticket_id "
        "JOIN markets m ON m.condition_id = t.condition_id "
        "WHERE t.portfolio_id = ? ORDER BY t.ticket_id",
        (portfolio["portfolio_id"],),
    ).fetchall()
    settled = [t for t in tickets if t["status"] == "SETTLED"]
    pnl_by_event: dict[str, list[Decimal]] = defaultdict(list)
    for ticket in settled:
        pnl_by_event[ticket["event_id"]].append(parse_money(ticket["net_pnl"]))
    realized = sum((parse_money(t["net_pnl"]) for t in settled), Decimal(0))
    edges = [_edges(conn, t, policy.probability) for t in settled]
    interval = bootstrap_interval(pnl_by_event)
    decisions = Counter(
        row["reason"] if row["kind"] == "REFUSED" else row["kind"]
        for row in conn.execute(
            "SELECT kind, reason FROM decisions WHERE portfolio_id = ?",
            (portfolio["portfolio_id"],),
        )
    )
    cash = available_cash(conn, portfolio["portfolio_id"])
    locked = open_cost(conn, portfolio["portfolio_id"])
    return {
        "variant": portfolio["variant"],
        "secondary": portfolio["variant"] != "primary",
        "probability": policy.probability,
        "starting_bankroll": _text(parse_money(portfolio["starting_bankroll"])),
        "available_cash": _text(cash),
        "locked_capital": _text(locked),
        "equity": _text(cash + locked),
        "tickets_open": len(tickets) - len(settled),
        "tickets_settled": len(settled),
        "decisions": dict(sorted(decisions.items())),
        "realized_pnl": _text(realized),
        "realized_pnl_net_of_research": _text(realized - research_cost),
        "bootstrap_95_by_event": None if interval is None else [_text(v) for v in interval],
        "hit_rate": _text(
            Decimal(sum(1 for t in settled if parse_money(t["net_pnl"]) > 0)) / len(settled)
            if settled else None
        ),
        "mean_edge_at_entry": _text(_mean([entry for entry, _ in edges])),
        "mean_realized_edge": _text(_mean([r for _, r in edges if r is not None])),
    }


def research_cost(conn: sqlite3.Connection, cohort_id: str) -> tuple[Decimal, int]:
    """(recorded spend of finished attempts, attempts still STARTED with unknown cost)."""
    spent = Decimal(0)
    open_attempts = 0
    for row in conn.execute(
        "SELECT status, cost_usd FROM research_attempts WHERE cohort_id = ?", (cohort_id,)
    ):
        if row["cost_usd"] is None:
            open_attempts += 1
        else:
            spent += parse_money(row["cost_usd"])
    return spent, open_attempts


def attention(
    conn: sqlite3.Connection, cohort_id: str, outcomes: dict[str, str | None], now: datetime
) -> dict[str, Any]:
    """What a human should look at: open tickets on markets reported resolved but not yet
    settled, and forecasted markets still unresolved 14 days after their end date."""
    pending = []
    for ticket in conn.execute(
        "SELECT t.ticket_id, t.condition_id FROM paper_tickets t "
        "JOIN portfolios p ON p.portfolio_id = t.portfolio_id "
        "WHERE p.cohort_id = ? AND t.status = 'OPEN' ORDER BY t.ticket_id",
        (cohort_id,),
    ):
        rows = conn.execute(
            "SELECT * FROM resolution_observations WHERE condition_id = ?",
            (ticket["condition_id"],),
        ).fetchall()
        since = resolved_since(rows)
        if since is not None:
            pending.append({"ticket_id": ticket["ticket_id"],
                            "condition_id": ticket["condition_id"],
                            "first_reported_resolved": isoformat(since)})
    overdue = []
    for row in conn.execute(
        "SELECT DISTINCT f.condition_id, r.rules_json FROM forecasts f "
        "JOIN rules_versions r ON r.condition_id = f.condition_id "
        "AND r.rules_hash = f.rules_hash WHERE f.cohort_id = ? AND f.kind = 'entry' "
        "ORDER BY f.condition_id",
        (cohort_id,),
    ):
        end_date = parse_datetime(json.loads(row["rules_json"])["end_date"])
        if outcomes.get(row["condition_id"]) is None and now - end_date > OVERDUE:
            overdue.append({"condition_id": row["condition_id"],
                            "end_date": isoformat(end_date)})
    return {"resolved_not_settled": pending, "overdue_unresolved": overdue}


def cohort_section(conn: sqlite3.Connection, cohort: sqlite3.Row, now: datetime) -> dict[str, Any]:
    cohort_id = cohort["cohort_id"]
    condition_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT condition_id FROM forecasts WHERE cohort_id = ?", (cohort_id,))]
    outcomes = _outcomes(conn, condition_ids)
    spent, open_attempts = research_cost(conn, cohort_id)
    identity = json.loads(cohort["identity_json"])
    return {
        "cohort_id": cohort_id,
        "status": cohort["status"],
        "started_at": cohort["started_at"],
        "closed_at": cohort["closed_at"],
        "model_id": identity.get("model_id"),
        "code_version": cohort["code_version"],
        "research_cost_usd": _text(spent),
        "attempts_with_unknown_cost": open_attempts,
        "forecasts": forecast_section(conn, cohort_id, outcomes),
        "updates": update_section(conn, cohort_id, outcomes),
        "portfolios": [
            portfolio_section(conn, portfolio, spent)
            for portfolio in conn.execute(
                "SELECT * FROM portfolios WHERE cohort_id = ? ORDER BY variant != 'primary', "
                "variant",
                (cohort_id,),
            ).fetchall()
        ],
        "attention": attention(conn, cohort_id, outcomes, now),
    }


def discovery_section(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The latest completed discovery run's eligibility funnel (markets seen, excluded at
    each step, eligible by category), from its shortlist report data; None before the
    first one."""
    run_id = latest_discovery_run(conn)
    if run_id is None:
        return None
    data = shortlist(conn, run_id)
    return {
        "run_id": run_id,
        "started_at": data["started_at"],
        "markets_seen": data["markets_seen"],
        "funnel": data["funnel"],
        "eligible_by_category": data["eligible_by_category"],
        "shortlist_report": f"shortlist-{run_id}.md",
    }


def performance(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    cohorts = conn.execute(
        "SELECT * FROM cohorts ORDER BY started_at, cohort_id"
    ).fetchall()
    return {
        "generated_at": isoformat(now),
        "guidance": GUIDANCE,
        "discovery": discovery_section(conn),
        "cohorts": [cohort_section(conn, cohort, now) for cohort in cohorts],
    }


def _score_cells(block: dict[str, Any] | None) -> str:
    if block is None:
        return "— | —"
    return f"{block['brier']} | {block['log']}"


def _score_table(title: str, groups: dict[str, Any]) -> list[str]:
    lines = [f"**{title}**", "",
             "| Group | n | Claude Brier | Claude log | Market Brier | Market log | "
             "Base-rate Brier | Base-rate log |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, block in groups.items():
        n = block["claude"]["n"] if block["claude"] else 0
        cells = " | ".join(_score_cells(block[f]) for f in FORECASTERS)
        lines.append(f"| {name} | {n} | {cells} |")
    return lines + [""]


def render_performance(data: dict[str, Any]) -> str:
    lines = [f"# Performance — generated {data['generated_at']}", "",
             f"> {data['guidance']}", "",
             "Results describe forecasts with **no detected price exposure** (detection is "
             "best-effort). Log score: mean log probability of the outcome, probabilities "
             "clipped to [0.01, 0.99]; higher is better. Brier: lower is better.", ""]
    discovery = data["discovery"]
    lines += ["## Discovery funnel (latest completed run)", ""]
    if discovery is None:
        lines += ["No completed discovery run yet.", ""]
    else:
        lines += [
            f"Run {discovery['run_id']} started {discovery['started_at']} — markets seen "
            f"{discovery['markets_seen']}; details in `{discovery['shortlist_report']}`.", "",
            "| Exclusion | Excluded | Remaining |", "|---|---:|---:|",
        ]
        lines += [f"| {step['reason']} | {step['excluded']} | {step['remaining']} |"
                  for step in discovery["funnel"]]
        lines += ["", "Eligible by category: "
                  + json.dumps(discovery["eligible_by_category"]), ""]
    if not data["cohorts"]:
        lines.append("No cohorts yet.")
    for cohort in data["cohorts"]:
        f = cohort["forecasts"]
        counts = f["counts"]
        lines += [
            f"## Cohort {cohort['cohort_id'][:12]} ({cohort['status']})", "",
            f"Model `{cohort['model_id']}` · code `{cohort['code_version']}` · started "
            f"{cohort['started_at']}" + (f" · closed {cohort['closed_at']}"
                                         if cohort["closed_at"] else ""), "",
            "### Counts", "",
            "| Count | Value |", "|---|---:|",
        ]
        for key, value in counts.items():
            shown = json.dumps(value) if isinstance(value, dict) else value
            lines.append(f"| {key} | {shown} |")
        lines += ["", f"Research cost: ${cohort['research_cost_usd']} "
                      f"(+{cohort['attempts_with_unknown_cost']} attempt(s) with unknown "
                      "cost)", "", "### Forecast scores (primary population)", ""]
        lines += _score_table("Overall", {"all": f["scores"]})
        lines += _score_table("By category", f["by_category"])
        lines += _score_table("By horizon", f["by_horizon"])
        lines += _score_table("By range width (p_high - p_low)", f["by_range_width"])
        lines += ["**Calibration (Claude p_mid)**", "",
                  "| Bucket | n | Mean forecast | Observed YES rate |", "|---|---:|---:|---:|"]
        lines += [f"| {b['bucket']} | {b['n']} | {b['mean_forecast'] or '—'} | "
                  f"{b['observed_rate'] or '—'} |" for b in f["calibration"]]
        lines += [""]
        lines += _score_table("Exposure-flagged subset (scored separately)",
                              {"flagged": f["exposure_flagged_scores"]})
        lines += ["### Paper portfolios", "",
                  "| Portfolio | Equity | Cash | Locked | Open | Settled | Realized P&L | "
                  "Net of research (full cohort cost) | 95% CI by event (heuristic) | Hit rate | "
                  "Edge at entry | "
                  "Realized edge |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|"]
        for p in cohort["portfolios"]:
            label = p["variant"] + (" (secondary)" if p["secondary"] else "")
            interval = ("—" if p["bootstrap_95_by_event"] is None
                        else " to ".join(p["bootstrap_95_by_event"]))
            lines.append(
                f"| {label} | {p['equity']} | {p['available_cash']} | {p['locked_capital']} | "
                f"{p['tickets_open']} | {p['tickets_settled']} | {p['realized_pnl']} | "
                f"{p['realized_pnl_net_of_research']} | {interval} | {p['hit_rate'] or '—'} | "
                f"{p['mean_edge_at_entry'] or '—'} | {p['mean_realized_edge'] or '—'} |"
            )
        lines += ["", "The whole cohort's research cost (entries and updates) is subtracted from "
                      "each portfolio's net.", ""]
        for p in cohort["portfolios"]:
            lines.append(f"Decisions ({p['variant']}): {json.dumps(p['decisions'])}  ")
        u = cohort["updates"]
        lines += ["", "### Update forecasts (scored separately; never traded)", "",
                  "Counts: " + json.dumps(u["counts"]), ""]
        lines += _score_table("Updates", {"updates": u["scores"]})
        lines += _score_table("Exposure-flagged updates (scored separately)",
                              {"flagged": u["exposure_flagged_scores"]})
        a = cohort["attention"]
        lines += ["### Needs attention", ""]
        if not a["resolved_not_settled"] and not a["overdue_unresolved"]:
            lines.append("Nothing.")
        for item in a["resolved_not_settled"]:
            lines.append(
                f"- Ticket {item['ticket_id']} on {item['condition_id']}: first reported "
                f"resolved {item['first_reported_resolved']} but not settled (the first "
                "such observation, even if the market was later re-posed)."
            )
        for item in a["overdue_unresolved"]:
            lines.append(f"- {item['condition_id']}: unresolved 14+ days after its end date "
                         f"{item['end_date']}.")
        lines.append("")
    return "\n".join(lines) + "\n"

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any

from .gamma import ELIGIBILITY_REASONS, fee_rate

REASON_ORDER: tuple[str, ...] = (
    ("PARSE_ERROR",) + ELIGIBILITY_REASONS + ("RESOLUTION_STATE_MISSING", "RESOLUTION_IN_PROGRESS")
)


def _count_by(conn: sqlite3.Connection, run_id: str, stage: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT reason_code, COUNT(*) AS n FROM refusals WHERE run_id = ? AND stage = ? "
        "GROUP BY reason_code",
        (run_id, stage),
    ).fetchall()
    return {row["reason_code"]: row["n"] for row in rows}


def shortlist(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"unknown run {run_id}")
    discover_refusals = _count_by(conn, run_id, "discover")
    markets = conn.execute(
        "SELECT m.*, r.rules_json FROM markets m "
        "JOIN discoveries d ON d.condition_id = m.condition_id "
        "JOIN rules_versions r ON r.condition_id = m.condition_id "
        "AND r.rules_hash = m.current_rules_hash "
        "WHERE d.run_id = ? ORDER BY m.category, m.question",
        (run_id,),
    ).fetchall()
    markets_seen = len(markets) + sum(discover_refusals.values())
    remaining = markets_seen
    funnel = []
    for reason in REASON_ORDER:
        excluded = discover_refusals.get(reason, 0)
        remaining -= excluded
        funnel.append({"reason": reason, "excluded": excluded, "remaining": remaining})
    by_category: dict[str, int] = {}
    rows = []
    for market in markets:
        by_category[market["category"]] = by_category.get(market["category"], 0) + 1
        snap = conn.execute(
            "SELECT record_json FROM book_snapshots WHERE run_id = ? AND condition_id = ? "
            "AND outcome = 'YES' ORDER BY id DESC LIMIT 1",
            (run_id, market["condition_id"]),
        ).fetchone()
        best_bid = best_ask = None
        if snap is not None:
            record = json.loads(snap["record_json"])
            best_bid = record["bids"][0][0] if record["bids"] else None
            best_ask = record["asks"][0][0] if record["asks"] else None
        schedule = json.loads(market["fee_schedule_json"]) if market["fee_schedule_json"] else None
        rate = fee_rate(schedule) if market["fees_enabled"] else Decimal("0")
        rows.append(
            {
                "condition_id": market["condition_id"],
                "question": market["question"],
                "category": market["category"],
                "end_date": json.loads(market["rules_json"])["end_date"],
                "fee_rate": None if rate is None else format(rate, "f"),
                "yes_best_bid": best_bid,
                "yes_best_ask": best_ask,
            }
        )
    return {
        "run_id": run_id,
        "started_at": run["started_at"],
        "geoblock": json.loads(run["geoblock_json"]) if run["geoblock_json"] else None,
        "markets_seen": markets_seen,
        "funnel": funnel,
        "eligible_by_category": by_category,
        "snapshot_refusals": _count_by(conn, run_id, "snapshot"),
        "markets": rows,
    }


def render_markdown(data: dict[str, Any]) -> str:
    lines = [
        f"# Shortlist — run {data['run_id']}",
        "",
        f"Started: {data['started_at']}  ",
        f"Geoblock (audit only): `{json.dumps(data['geoblock'])}`",
        "",
        f"**Markets seen: {data['markets_seen']}** — "
        f"eligible: {data['funnel'][-1]['remaining'] if data['funnel'] else 0}",
        "",
        "| Exclusion | Excluded | Remaining |",
        "|---|---:|---:|",
    ]
    lines += [f"| {s['reason']} | {s['excluded']} | {s['remaining']} |" for s in data["funnel"]]
    lines += ["", "Eligible by category: " + json.dumps(data["eligible_by_category"])]
    lines += ["Snapshot refusals: " + json.dumps(data["snapshot_refusals"]), ""]
    lines += [
        "| Question | Category | Ends | Fee rate | YES bid | YES ask |",
        "|---|---|---|---:|---:|---:|",
    ]
    for m in data["markets"]:
        question = m["question"].replace("|", "\\|")
        lines.append(
            f"| {question} | {m['category']} | {m['end_date']} | {m['fee_rate']} | "
            f"{m['yes_best_bid']} | {m['yes_best_ask']} |"
        )
    return "\n".join(lines) + "\n"

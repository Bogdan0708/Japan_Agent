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
    """Report built only from rows recorded for this run, so later runs cannot change it.
    A snapshot run reports its source discovery's immutable market rows with its own books."""
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"unknown run {run_id}")
    discovery_run_id = run["source_run_id"] if run["command"] == "snapshot" else run_id
    discover_refusals = _count_by(conn, discovery_run_id, "discover") if discovery_run_id else {}
    markets = conn.execute(
        "SELECT d.*, r.rules_json FROM discoveries d "
        "JOIN rules_versions r ON r.condition_id = d.condition_id "
        "AND r.rules_hash = d.rules_hash "
        "WHERE d.run_id = ? ORDER BY d.category, d.question",
        (discovery_run_id,),
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
    both_books = 0
    for market in markets:
        by_category[market["category"]] = by_category.get(market["category"], 0) + 1
        books = {
            snap["outcome"]: snap
            for snap in conn.execute(
                "SELECT outcome, record_json, fetched_at FROM book_snapshots "
                "WHERE run_id = ? AND condition_id = ? ORDER BY id",
                (run_id, market["condition_id"]),
            )
        }
        both_books += int({"YES", "NO"} <= set(books))
        best_bid = best_ask = book_fetched_at = None
        if "YES" in books:
            record = json.loads(books["YES"]["record_json"])
            best_bid = record["bids"][0][0] if record["bids"] else None
            best_ask = record["asks"][0][0] if record["asks"] else None
            book_fetched_at = books["YES"]["fetched_at"]
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
                "book_fetched_at": book_fetched_at,
            }
        )
    failures = conn.execute(
        "SELECT reason_code, detail FROM refusals WHERE run_id = ? AND condition_id IS NULL "
        "AND stage = ? ORDER BY id",
        (run_id, run["command"]),
    ).fetchall()
    return {
        "run_id": run_id,
        "command": run["command"],
        "status": run["status"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "discovery_run_id": discovery_run_id,
        "run_failures": [{"reason": f["reason_code"], "detail": f["detail"]} for f in failures],
        "geoblock": json.loads(run["geoblock_json"]) if run["geoblock_json"] else None,
        "markets_seen": markets_seen,
        "funnel": funnel,
        "eligible_by_category": by_category,
        "markets_with_both_books": both_books,
        "snapshot_refusals": _count_by(conn, run_id, "snapshot"),
        "markets": rows,
    }


def render_markdown(data: dict[str, Any]) -> str:
    lines = [f"# Shortlist — run {data['run_id']}", ""]
    if data["status"] != "COMPLETED":
        lines += [
            f"> **Run status: {data['status']}** — counts below are partial and must not be "
            "read as a complete collection.",
        ]
        lines += [f"> {f['reason']}: {f['detail']}" for f in data["run_failures"]]
        lines.append("")
    lines += [
        f"Command: {data['command']} · status {data['status']} · started {data['started_at']}"
        f" · finished {data['finished_at']}  ",
        f"Discovery run: {data['discovery_run_id']}  ",
        f"Geoblock (audit only): `{json.dumps(data['geoblock'])}`",
        "",
        f"**Markets seen: {data['markets_seen']}** — "
        f"discovery-eligible: {data['funnel'][-1]['remaining'] if data['funnel'] else 0} — "
        f"with both books usable in this run: {data['markets_with_both_books']}",
        "",
        "| Exclusion | Excluded | Remaining |",
        "|---|---:|---:|",
    ]
    lines += [f"| {s['reason']} | {s['excluded']} | {s['remaining']} |" for s in data["funnel"]]
    lines += ["", "Eligible by category: " + json.dumps(data["eligible_by_category"])]
    lines += ["Snapshot refusals: " + json.dumps(data["snapshot_refusals"]), ""]
    lines += [
        "| Question | Category | Ends | Fee rate | YES bid | YES ask | Book fetched |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for m in data["markets"]:
        question = m["question"].replace("|", "\\|")
        lines.append(
            f"| {question} | {m['category']} | {m['end_date']} | {m['fee_rate']} | "
            f"{m['yes_best_bid']} | {m['yes_best_ask']} | {m['book_fetched_at']} |"
        )
    return "\n".join(lines) + "\n"

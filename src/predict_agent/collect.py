from __future__ import annotations

import sqlite3
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .clob import book_refusal, fetch_book, parse_book, snapshot_hash
from .config import DiscoveryConfig
from .db import append_journal, record_refusal, transaction
from .gamma import (
    MarketCandidate,
    ParseError,
    category_for,
    eligibility_refusal,
    fetch_events,
    parse_market,
    rules_hash,
    rules_payload,
)
from .http import FetchError, JsonClient
from .resolution import (
    fetch_closed_gamma_markets,
    fetch_resolutions,
    gamma_outcome,
    parse_resolution,
    reconcile_outcome,
    resolution_refusal,
)
from .util import canonical_json, isoformat

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


@dataclass(frozen=True)
class DiscoverySummary:
    markets_seen: int
    eligible: int
    refusals: dict[str, int]


def start_run(conn: sqlite3.Connection, command: str, policy_hash: str, now: datetime) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO runs (run_id, command, started_at, policy_hash) VALUES (?, ?, ?, ?)",
        (run_id, command, isoformat(now), policy_hash),
    )
    return run_id


def record_geoblock(
    conn: sqlite3.Connection, client: JsonClient, run_id: str
) -> dict[str, Any] | None:
    try:
        result = client.get(GEOBLOCK_URL)
        stored: dict[str, Any] = result if isinstance(result, dict) else {"unexpected": result}
        returned: dict[str, Any] | None = stored if isinstance(result, dict) else None
    except FetchError as error:
        stored, returned = {"error": error.reason}, None
    conn.execute(
        "UPDATE runs SET geoblock_json = ? WHERE run_id = ?", (canonical_json(stored), run_id)
    )
    return returned


def _store_market(
    conn: sqlite3.Connection,
    candidate: MarketCandidate,
    category: str,
    run_id: str,
    now: datetime,
) -> None:
    new_hash = rules_hash(candidate)
    now_text = isoformat(now)
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO rules_versions (rules_hash, condition_id, rules_json, "
            "first_seen_at) VALUES (?, ?, ?, ?)",
            (new_hash, candidate.condition_id, canonical_json(rules_payload(candidate)), now_text),
        )
        existing = conn.execute(
            "SELECT current_rules_hash FROM markets WHERE condition_id = ?",
            (candidate.condition_id,),
        ).fetchone()
        schedule = canonical_json(candidate.fee_schedule) if candidate.fee_schedule else None
        if existing is None:
            conn.execute(
                "INSERT INTO markets (condition_id, event_id, question, category, yes_token_id, "
                "no_token_id, current_rules_hash, fees_enabled, fee_schedule_json, "
                "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.condition_id,
                    candidate.event_id,
                    candidate.question,
                    category,
                    candidate.token_ids[0],
                    candidate.token_ids[1],
                    new_hash,
                    int(candidate.fees_enabled),
                    schedule,
                    now_text,
                    now_text,
                ),
            )
            append_journal(
                conn, "MARKET_DISCOVERED", {"condition_id": candidate.condition_id}, now
            )
        else:
            if existing["current_rules_hash"] != new_hash:
                append_journal(
                    conn,
                    "RULES_CHANGED",
                    {
                        "condition_id": candidate.condition_id,
                        "from": existing["current_rules_hash"],
                        "to": new_hash,
                    },
                    now,
                )
            conn.execute(
                "UPDATE markets SET question = ?, category = ?, current_rules_hash = ?, "
                "fees_enabled = ?, fee_schedule_json = ?, last_seen_at = ? "
                "WHERE condition_id = ?",
                (
                    candidate.question,
                    category,
                    new_hash,
                    int(candidate.fees_enabled),
                    schedule,
                    now_text,
                    candidate.condition_id,
                ),
            )
        conn.execute(
            "INSERT OR IGNORE INTO discoveries (run_id, condition_id) VALUES (?, ?)",
            (run_id, candidate.condition_id),
        )


def discover(
    conn: sqlite3.Connection,
    client: JsonClient,
    config: DiscoveryConfig,
    run_id: str,
    now: datetime,
) -> DiscoverySummary:
    refusals: Counter[str] = Counter()
    seen: set[str] = set()
    passing: list[MarketCandidate] = []
    for event in fetch_events(client, config):
        for raw_market in event.get("markets") or []:
            key = str(raw_market.get("conditionId") or id(raw_market))
            if key in seen:
                continue
            seen.add(key)
            try:
                candidate = parse_market(raw_market, event)
            except ParseError as error:
                refusals["PARSE_ERROR"] += 1
                record_refusal(conn, run_id, None, "discover", "PARSE_ERROR", str(error), now)
                continue
            reason = eligibility_refusal(candidate, config, now)
            if reason is not None:
                refusals[reason] += 1
                record_refusal(conn, run_id, candidate.condition_id, "discover", reason, "", now)
                continue
            passing.append(candidate)
    states = fetch_resolutions(client, [c.condition_id for c in passing]) if passing else {}
    eligible = 0
    for candidate in passing:
        reason = resolution_refusal(states.get(candidate.condition_id))
        if reason is not None:
            refusals[reason] += 1
            record_refusal(conn, run_id, candidate.condition_id, "discover", reason, "", now)
            continue
        category = category_for(candidate.tag_slugs, config)
        if category is None:  # unreachable: eligibility already required a category
            raise AssertionError("eligible market without category")
        _store_market(conn, candidate, category, run_id, now)
        eligible += 1
    return DiscoverySummary(markets_seen=len(seen), eligible=eligible, refusals=dict(refusals))


def snapshot_eligible(
    conn: sqlite3.Connection,
    client: JsonClient,
    config: DiscoveryConfig,
    run_id: str,
    now_fn: Callable[[], datetime],
) -> int:
    rows = conn.execute(
        "SELECT m.* FROM markets m JOIN discoveries d ON d.condition_id = m.condition_id "
        "WHERE d.run_id = ? ORDER BY m.condition_id",
        (run_id,),
    ).fetchall()
    stored = 0
    for row in rows:
        for outcome, token_id in (("YES", row["yes_token_id"]), ("NO", row["no_token_id"])):
            condition_id = row["condition_id"]
            try:
                raw = fetch_book(client, token_id)
                fetched_at = now_fn()
                snapshot = parse_book(raw, fetched_at)
            except (FetchError, ParseError) as error:
                code = "FETCH_ERROR" if isinstance(error, FetchError) else "PARSE_ERROR"
                record_refusal(conn, run_id, condition_id, "snapshot", code, str(error), now_fn())
                continue
            reason = book_refusal(snapshot, condition_id, token_id)
            if reason is not None:
                record_refusal(conn, run_id, condition_id, "snapshot", reason, outcome, fetched_at)
                continue
            digest = snapshot_hash(snapshot)
            with transaction(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO book_snapshots (run_id, condition_id, token_id, "
                    "outcome, observed_at, fetched_at, record_json, fees_enabled, "
                    "fee_schedule_json, snapshot_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        condition_id,
                        token_id,
                        outcome,
                        isoformat(snapshot.observed_at),
                        isoformat(snapshot.fetched_at),
                        canonical_json(snapshot.record()),
                        row["fees_enabled"],
                        row["fee_schedule_json"],
                        digest,
                    ),
                )
            stored += 1
    return stored


def poll_resolutions(
    conn: sqlite3.Connection, client: JsonClient, run_id: str, now: datetime
) -> int:
    ids = [r["condition_id"] for r in conn.execute("SELECT condition_id FROM markets ORDER BY 1")]
    if not ids:
        return 0
    rows = fetch_resolutions(client, ids)
    states = {cid: parse_resolution(row) for cid, row in rows.items()}
    resolved = [cid for cid, state in states.items() if state.status == "resolved"]
    gamma = fetch_closed_gamma_markets(client, resolved) if resolved else {}
    stored = 0
    with transaction(conn):
        for condition_id in ids:
            state = states.get(condition_id)
            if state is None:
                record_refusal(
                    conn, run_id, condition_id, "resolve", "RESOLUTION_STATE_MISSING", "", now
                )
                continue
            market = gamma.get(condition_id)
            outcome = reconcile_outcome(
                state, gamma_outcome(market.get("outcomePrices")) if market else None
            )
            conn.execute(
                "INSERT INTO resolution_observations (run_id, condition_id, fetched_at, status, "
                "outcome, was_disputed, new_version_q, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    condition_id,
                    isoformat(now),
                    state.status,
                    outcome,
                    int(state.was_disputed),
                    int(state.new_version_q),
                    canonical_json(state.raw),
                ),
            )
            if outcome == "UNKNOWN":
                append_journal(
                    conn, "RESOLUTION_UNKNOWN", {"condition_id": condition_id}, now
                )
            stored += 1
    return stored

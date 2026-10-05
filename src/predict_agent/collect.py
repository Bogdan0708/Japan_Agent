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
    rules_payload,
    rules_payload_from_raw,
)
from .http import FetchError, JsonClient
from .resolution import (
    fetch_gamma_markets,
    fetch_resolutions,
    parse_resolution,
    reconcile_outcome,
    resolution_refusal,
)
from .util import canonical_json, isoformat, sha256_json

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


@dataclass(frozen=True)
class DiscoverySummary:
    markets_seen: int
    eligible: int
    refusals: dict[str, int]


def start_run(
    conn: sqlite3.Connection,
    command: str,
    policy_hash: str,
    now: datetime,
    source_run_id: str | None = None,
) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO runs (run_id, command, started_at, policy_hash, source_run_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, command, isoformat(now), policy_hash, source_run_id),
    )
    return run_id


def finish_run(conn: sqlite3.Connection, run_id: str, status: str, now: datetime) -> None:
    if status not in ("COMPLETED", "FAILED"):
        raise ValueError(f"invalid run status {status}")
    conn.execute(
        "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
        (status, isoformat(now), run_id),
    )


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


def _record_rules_version(
    conn: sqlite3.Connection,
    condition_id: str,
    payload: dict[str, str | None],
    now: datetime,
) -> str:
    """Store this market's rules version; journal RULES_CHANGED when a known market's rules
    moved. The caller holds the transaction."""
    new_hash = sha256_json(payload)
    conn.execute(
        "INSERT OR IGNORE INTO rules_versions (condition_id, rules_hash, rules_json, "
        "first_seen_at) VALUES (?, ?, ?, ?)",
        (condition_id, new_hash, canonical_json(payload), isoformat(now)),
    )
    existing = conn.execute(
        "SELECT current_rules_hash FROM markets WHERE condition_id = ?", (condition_id,)
    ).fetchone()
    if existing is not None and existing["current_rules_hash"] != new_hash:
        append_journal(
            conn,
            "RULES_CHANGED",
            {"condition_id": condition_id, "from": existing["current_rules_hash"], "to": new_hash},
            now,
        )
        conn.execute(
            "UPDATE markets SET current_rules_hash = ? WHERE condition_id = ?",
            (new_hash, condition_id),
        )
    return new_hash


def _track_known_market(
    conn: sqlite3.Connection, candidate: MarketCandidate, now: datetime
) -> None:
    """Keep rules history current for markets already stored, whatever their eligibility now:
    clarifications tend to land near resolution, after a market leaves the shortlist."""
    known = conn.execute(
        "SELECT 1 FROM markets WHERE condition_id = ?", (candidate.condition_id,)
    ).fetchone()
    if known is None:
        return
    with transaction(conn):
        _record_rules_version(conn, candidate.condition_id, rules_payload(candidate), now)
        conn.execute(
            "UPDATE markets SET last_seen_at = ? WHERE condition_id = ?",
            (isoformat(now), candidate.condition_id),
        )


def _store_market(
    conn: sqlite3.Connection,
    candidate: MarketCandidate,
    category: str,
    run_id: str,
    now: datetime,
) -> None:
    now_text = isoformat(now)
    schedule = canonical_json(candidate.fee_schedule) if candidate.fee_schedule else None
    with transaction(conn):
        new_hash = _record_rules_version(
            conn, candidate.condition_id, rules_payload(candidate), now
        )
        existing = conn.execute(
            "SELECT 1 FROM markets WHERE condition_id = ?", (candidate.condition_id,)
        ).fetchone()
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
            conn.execute(
                "UPDATE markets SET question = ?, category = ?, fees_enabled = ?, "
                "fee_schedule_json = ?, last_seen_at = ? WHERE condition_id = ?",
                (
                    candidate.question,
                    category,
                    int(candidate.fees_enabled),
                    schedule,
                    now_text,
                    candidate.condition_id,
                ),
            )
        conn.execute(
            "INSERT OR IGNORE INTO discoveries (run_id, condition_id, event_id, question, "
            "category, yes_token_id, no_token_id, rules_hash, fees_enabled, fee_schedule_json, "
            "observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
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
            ),
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
        raw_markets = event.get("markets")
        for raw_market in raw_markets if isinstance(raw_markets, list) else []:
            if not isinstance(raw_market, dict):
                refusals["PARSE_ERROR"] += 1
                detail = f"market entry is {type(raw_market).__name__}, not an object"
                record_refusal(conn, run_id, None, "discover", "PARSE_ERROR", detail, now)
                continue
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
            _track_known_market(conn, candidate, now)
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
    source_run_id: str,
    run_id: str,
    now_fn: Callable[[], datetime],
) -> int:
    """Snapshot books for the markets discovery run `source_run_id` found eligible. Books and
    refusals belong to the acquiring run `run_id`; tokens and the fee schedule come from the
    immutable discovery row (so the fee is as observed at discovery time)."""
    rows = conn.execute(
        "SELECT * FROM discoveries WHERE run_id = ? ORDER BY condition_id", (source_run_id,)
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
                    "INSERT OR IGNORE INTO book_snapshots (run_id, source_run_id, condition_id, "
                    "token_id, outcome, observed_at, fetched_at, record_json, fees_enabled, "
                    "fee_schedule_json, snapshot_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        source_run_id,
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
    conn: sqlite3.Connection,
    client: JsonClient,
    run_id: str,
    now_fn: Callable[[], datetime],
) -> int:
    """Observe resolution state for every tracked market, cross-check against Gamma, and
    refresh each market's rules from the same Gamma response (clarifications often arrive
    after a market has left discovery). Both source responses and their retrieval times are
    stored with the observation, plus the time the resolution request started, so
    settlement can tell overlapping polls apart."""
    ids = [r["condition_id"] for r in conn.execute("SELECT condition_id FROM markets ORDER BY 1")]
    if not ids:
        return 0
    resolution_requested_at = now_fn()
    rows = fetch_resolutions(client, ids)
    resolution_fetched_at = now_fn()
    gamma = fetch_gamma_markets(client, ids)
    gamma_fetched_at = now_fn()
    states = {cid: parse_resolution(row) for cid, row in rows.items()}
    stored = 0
    with transaction(conn):
        for condition_id in ids:
            market = gamma.get(condition_id)
            if market is not None:
                try:
                    payload = rules_payload_from_raw(market)
                except ParseError as error:
                    record_refusal(
                        conn,
                        run_id,
                        condition_id,
                        "resolve",
                        "RULES_PARSE_ERROR",
                        str(error),
                        gamma_fetched_at,
                    )
                else:
                    _record_rules_version(conn, condition_id, payload, gamma_fetched_at)
            state = states.get(condition_id)
            if state is None:
                record_refusal(
                    conn,
                    run_id,
                    condition_id,
                    "resolve",
                    "RESOLUTION_STATE_MISSING",
                    "",
                    resolution_fetched_at,
                )
                continue
            outcome, cross_check = reconcile_outcome(
                state, market.get("outcomePrices") if market else None
            )
            conn.execute(
                "INSERT INTO resolution_observations (run_id, condition_id, "
                "resolution_fetched_at, gamma_fetched_at, gamma_json, status, outcome, "
                "cross_check, was_disputed, new_version_q, raw_json, resolution_requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    condition_id,
                    isoformat(resolution_fetched_at),
                    isoformat(gamma_fetched_at) if market else None,
                    canonical_json(market) if market else None,
                    state.status,
                    outcome,
                    cross_check,
                    int(state.was_disputed),
                    int(state.new_version_q),
                    canonical_json(state.raw),
                    isoformat(resolution_requested_at),
                ),
            )
            if outcome == "UNKNOWN":
                append_journal(
                    conn, "RESOLUTION_UNKNOWN", {"condition_id": condition_id}, gamma_fetched_at
                )
            stored += 1
    return stored

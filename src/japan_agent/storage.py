from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .models import PriceSnapshot, ProposalStatus, TradeTicket, canonical_json
from .time import isoformat, parse_datetime, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    native_price TEXT NOT NULL,
    native_currency TEXT NOT NULL,
    price_gbp TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(ticker, observed_at, source)
);
CREATE INDEX IF NOT EXISTS snapshots_latest ON snapshots(ticker, observed_at DESC);

CREATE TABLE IF NOT EXISTS research_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    headline TEXT NOT NULL,
    published_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    url TEXT,
    ticker TEXT,
    payload_json TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS research_items_recent
    ON research_items(source, published_at DESC);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    observed_through TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    CHECK(status IN ('SUCCESS', 'FAILED'))
);
CREATE INDEX IF NOT EXISTS ingest_runs_latest
    ON ingest_runs(source, completed_at DESC);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    ticket_json TEXT NOT NULL,
    ticket_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    rejection_reason TEXT,
    CHECK(status IN (
        'PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'SUBMITTED',
        'FILLED', 'FAILED', 'RECONCILIATION_REQUIRED'
    ))
);
CREATE INDEX IF NOT EXISTS proposals_status ON proposals(status, expires_at);

CREATE TRIGGER IF NOT EXISTS immutable_proposal_ticket
BEFORE UPDATE OF ticket_json, ticket_hash, created_at, expires_at ON proposals
BEGIN
    SELECT RAISE(ABORT, 'trade ticket is immutable');
END;

CREATE TABLE IF NOT EXISTS executions (
    proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id),
    state TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT,
    broker_order_id TEXT,
    submitted_at TEXT,
    updated_at TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    aggregate_id TEXT,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE
);
"""


@dataclass(frozen=True)
class StoredProposal:
    ticket: TradeTicket
    status: ProposalStatus
    ticket_hash: str
    approved_by: str | None
    approved_at: datetime | None
    rejection_reason: str | None


@dataclass(frozen=True)
class ExecutionClaim:
    proposal_id: str
    state: str
    request: dict[str, Any]
    response: dict[str, Any] | None
    broker_order_id: str | None
    error: str | None
    is_new: bool = False


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def save_snapshot(self, snapshot: PriceSnapshot, raw: dict[str, Any] | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO snapshots(
                    ticker, native_price, native_currency, price_gbp, observed_at, source, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.ticker,
                    str(snapshot.native_price),
                    snapshot.native_currency,
                    str(snapshot.price_gbp),
                    isoformat(snapshot.observed_at),
                    snapshot.source,
                    canonical_json(raw or {}),
                ),
            )

    def latest_snapshot(self, ticker: str) -> PriceSnapshot | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE ticker = ? ORDER BY observed_at DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        if row is None:
            return None
        return PriceSnapshot(
            ticker=row["ticker"],
            native_price=Decimal(row["native_price"]),
            native_currency=row["native_currency"],
            price_gbp=Decimal(row["price_gbp"]),
            observed_at=parse_datetime(row["observed_at"]),
            source=row["source"],
        )

    def save_research_item(
        self,
        *,
        source: str,
        external_id: str,
        headline: str,
        published_at: datetime,
        retrieved_at: datetime,
        payload: dict[str, Any],
        url: str | None = None,
        ticker: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO research_items(
                    source, external_id, headline, published_at, retrieved_at, url, ticker,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, external_id) DO UPDATE SET
                    retrieved_at = excluded.retrieved_at,
                    payload_json = excluded.payload_json
                """,
                (
                    source,
                    external_id,
                    headline,
                    isoformat(published_at),
                    isoformat(retrieved_at),
                    url,
                    ticker,
                    canonical_json(payload),
                ),
            )

    def record_ingest_run(
        self,
        *,
        source: str,
        completed_at: datetime,
        observed_through: datetime,
        item_count: int,
        status: str = "SUCCESS",
        error: str | None = None,
    ) -> None:
        if status not in {"SUCCESS", "FAILED"}:
            raise ValueError("ingest status must be SUCCESS or FAILED")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO ingest_runs(
                    source, completed_at, observed_through, item_count, status, error
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    isoformat(completed_at),
                    isoformat(observed_through),
                    item_count,
                    status,
                    error,
                ),
            )

    def latest_successful_ingest(self, source: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM ingest_runs
                 WHERE source = ? AND status = 'SUCCESS'
                 ORDER BY completed_at DESC LIMIT 1
                """,
                (source,),
            ).fetchone()
        if row is None:
            return None
        return {
            "source": row["source"],
            "completed_at": parse_datetime(row["completed_at"]),
            "observed_through": parse_datetime(row["observed_through"]),
            "item_count": row["item_count"],
        }

    def recent_research_items(self, *, since: datetime, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT source, external_id, headline, published_at, retrieved_at, url, ticker,
                       payload_json
                  FROM research_items
                 WHERE published_at >= ?
                 ORDER BY published_at DESC LIMIT ?
                """,
                (isoformat(since), limit),
            ).fetchall()
        return [
            {
                "source": row["source"],
                "external_id": row["external_id"],
                "headline": row["headline"],
                "published_at": row["published_at"],
                "retrieved_at": row["retrieved_at"],
                "url": row["url"],
                "ticker": row["ticker"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def create_proposal(self, ticket: TradeTicket) -> None:
        serialized = canonical_json(ticket.to_dict())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO proposals(
                    proposal_id, status, ticket_json, ticket_hash, created_at, expires_at
                ) VALUES (?, 'PENDING', ?, ?, ?, ?)
                """,
                (
                    ticket.proposal_id,
                    serialized,
                    ticket.fingerprint,
                    isoformat(ticket.created_at),
                    isoformat(ticket.expires_at),
                ),
            )

    def get_proposal(self, proposal_id: str) -> StoredProposal | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        if row is None:
            return None
        return StoredProposal(
            ticket=TradeTicket.from_dict(json.loads(row["ticket_json"])),
            status=ProposalStatus(row["status"]),
            ticket_hash=row["ticket_hash"],
            approved_by=row["approved_by"],
            approved_at=(parse_datetime(row["approved_at"]) if row["approved_at"] else None),
            rejection_reason=row["rejection_reason"],
        )

    def decide_proposal(
        self,
        proposal_id: str,
        *,
        approve: bool,
        approver: str,
        now: datetime,
        expected_hash: str,
        reason: str | None = None,
    ) -> StoredProposal:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"proposal {proposal_id!r} not found")
            if row["status"] != ProposalStatus.PENDING.value:
                raise ValueError(f"proposal is already {row['status']}")
            if row["ticket_hash"] != expected_hash:
                raise ValueError("approval hash does not match immutable ticket")
            if parse_datetime(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE proposals SET status = 'EXPIRED' WHERE proposal_id = ?",
                    (proposal_id,),
                )
                raise ValueError("proposal has expired")
            status = ProposalStatus.APPROVED if approve else ProposalStatus.REJECTED
            connection.execute(
                """
                UPDATE proposals
                   SET status = ?, approved_by = ?, approved_at = ?, rejection_reason = ?
                 WHERE proposal_id = ?
                """,
                (status.value, approver, isoformat(now), reason, proposal_id),
            )
        stored = self.get_proposal(proposal_id)
        assert stored is not None
        return stored

    def has_open_duplicate(self, ticker: str, side: str) -> bool:
        pattern = f'%"side":"{side}"%'
        ticker_pattern = f'%"ticker":"{ticker}"%'
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM proposals
                 WHERE status IN ('PENDING', 'APPROVED', 'SUBMITTED', 'RECONCILIATION_REQUIRED')
                   AND ticket_json LIKE ? AND ticket_json LIKE ?
                 LIMIT 1
                """,
                (pattern, ticker_pattern),
            ).fetchone()
        return row is not None

    def submitted_trade_times(self, since: datetime) -> list[datetime]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT submitted_at FROM executions
                 WHERE submitted_at IS NOT NULL AND submitted_at >= ?
                """,
                (isoformat(since),),
            ).fetchall()
        return [parse_datetime(row["submitted_at"]) for row in rows]

    def claim_execution(
        self, proposal_id: str, request: dict[str, Any], now: datetime
    ) -> ExecutionClaim:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM executions WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO executions(
                        proposal_id, state, request_json, updated_at
                    ) VALUES (?, 'CLAIMED', ?, ?)
                    """,
                    (proposal_id, canonical_json(request), isoformat(now)),
                )
                return ExecutionClaim(proposal_id, "CLAIMED", request, None, None, None, True)
        return self.get_execution(proposal_id)

    def get_execution(self, proposal_id: str) -> ExecutionClaim:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM executions WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"execution {proposal_id!r} not found")
        return ExecutionClaim(
            proposal_id=row["proposal_id"],
            state=row["state"],
            request=json.loads(row["request_json"]),
            response=json.loads(row["response_json"]) if row["response_json"] else None,
            broker_order_id=row["broker_order_id"],
            error=row["error"],
            is_new=False,
        )

    def update_execution(
        self,
        proposal_id: str,
        *,
        state: str,
        now: datetime,
        response: dict[str, Any] | None = None,
        broker_order_id: str | None = None,
        error: str | None = None,
        proposal_status: ProposalStatus | None = None,
    ) -> None:
        submitted_at = isoformat(now) if state == "SUBMITTED" else None
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE executions
                   SET state = ?, response_json = COALESCE(?, response_json),
                       broker_order_id = COALESCE(?, broker_order_id),
                       submitted_at = COALESCE(?, submitted_at), updated_at = ?, error = ?
                 WHERE proposal_id = ?
                """,
                (
                    state,
                    canonical_json(response) if response is not None else None,
                    broker_order_id,
                    submitted_at,
                    isoformat(now),
                    error,
                    proposal_id,
                ),
            )
            if proposal_status is not None:
                connection.execute(
                    "UPDATE proposals SET status = ? WHERE proposal_id = ?",
                    (proposal_status.value, proposal_id),
                )

    def append_event(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        aggregate_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        from hashlib import sha256

        occurred_at = now or utc_now()
        event_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            last = connection.execute(
                "SELECT event_hash FROM events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            previous_hash = last["event_hash"] if last else "0" * 64
            core = {
                "event_id": event_id,
                "occurred_at": isoformat(occurred_at),
                "kind": kind,
                "aggregate_id": aggregate_id,
                "payload": payload,
                "previous_hash": previous_hash,
            }
            event_hash = sha256(canonical_json(core).encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO events(
                    event_id, occurred_at, kind, aggregate_id, payload_json,
                    previous_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    isoformat(occurred_at),
                    kind,
                    aggregate_id,
                    canonical_json(payload),
                    previous_hash,
                    event_hash,
                ),
            )
        return {**core, "event_hash": event_hash}

    def iter_events(self) -> Iterator[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM events ORDER BY sequence").fetchall()
        for row in rows:
            yield {
                "sequence": row["sequence"],
                "event_id": row["event_id"],
                "occurred_at": row["occurred_at"],
                "kind": row["kind"],
                "aggregate_id": row["aggregate_id"],
                "payload": json.loads(row["payload_json"]),
                "previous_hash": row["previous_hash"],
                "event_hash": row["event_hash"],
            }

    def sync_jsonl(self, path: Path) -> int:
        """Rebuild the public JSONL mirror atomically from the append-only DB ledger."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        count = 0
        with temporary.open("w", encoding="utf-8") as handle:
            for event in self.iter_events():
                handle.write(canonical_json(event) + "\n")
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        return count

"""Paper trading (spec §3 stages 4–5): for each entry forecast with a timely baseline,
decide in every undecided portfolio of its cohort and record a ticket or a refusal.

Each portfolio's decision runs in one transaction: the policy inputs (forecast, books,
cash, exposure, eligibility) are read inside the same lock that writes the result, and
the policy is the portfolio's frozen artifact, never the config file."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .artifacts import load_artifact
from .cash import LedgerError, available_cash, parse_money
from .config import ConfigError
from .db import transaction
from .fills import AskLevel
from .forecasts import ResumeStep, resume_step, undecided_portfolios, unfinished_forecasts
from .policy import Exposure, ForecastView, PolicyInputs, SideBook, Trade, decide
from .policy_params import PolicyParams, policy_from_artifact
from .resolution import OPEN_STATUSES
from .tickets import TicketDraft, equity, open_ticket_locked, record_refusal_locked
from .util import parse_datetime

COHORT_CLOSED = "COHORT_CLOSED"


@dataclass(frozen=True)
class DecisionResult:
    portfolio_id: str
    forecast_id: int
    ticket_id: int | None
    reason: str | None  # refusal code, None when traded
    detail: str


@dataclass(frozen=True)
class TradeSummary:
    traded: int
    refused: dict[str, int]
    waiting: int  # entry forecasts not ready for a decision (no baseline yet)


def _optional_decimal(text: str | None) -> Decimal | None:
    return None if text is None else parse_money(text)


def latest_discovery_run(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM runs WHERE command IN ('discover', 'run-data') "
        "AND status = 'COMPLETED' ORDER BY started_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return None if row is None else str(row["run_id"])


def _side_book(conn: sqlite3.Connection, snapshot_id: int, outcome: str) -> SideBook:
    row = conn.execute("SELECT * FROM book_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if row is None:
        raise LedgerError(f"baseline snapshot {snapshot_id} is missing")
    record = json.loads(row["record_json"])
    schedule = row["fee_schedule_json"]
    return SideBook(
        outcome=outcome,
        snapshot_id=snapshot_id,
        fetched_at=parse_datetime(row["fetched_at"]),
        asks=tuple(AskLevel(Decimal(price), Decimal(size)) for price, size in record["asks"]),
        tick_size=Decimal(record["tick_size"]),
        min_order_size=Decimal(record["min_order_size"]),
        fees_enabled=bool(row["fees_enabled"]),
        fee_schedule=None if schedule is None else json.loads(schedule),
    )


def _exposure(
    conn: sqlite3.Connection, portfolio_id: str, condition_id: str, event_id: str, category: str
) -> Exposure:
    totals = {"market": Decimal("0"), "event": Decimal("0"), "category": Decimal("0")}
    total = Decimal("0")
    for row in conn.execute(
        "SELECT t.cost_total, t.condition_id, m.event_id, m.category FROM paper_tickets t "
        "JOIN markets m ON m.condition_id = t.condition_id "
        "WHERE t.portfolio_id = ? AND t.status = 'OPEN'",
        (portfolio_id,),
    ):
        cost = parse_money(row["cost_total"])
        total += cost
        if row["condition_id"] == condition_id:
            totals["market"] += cost
        if row["event_id"] == event_id:
            totals["event"] += cost
        if row["category"] == category:
            totals["category"] += cost
    return Exposure(totals["market"], totals["event"], totals["category"], total)


def load_inputs(
    conn: sqlite3.Connection, portfolio_id: str, forecast_id: int, now: datetime
) -> PolicyInputs:
    forecast = conn.execute(
        "SELECT * FROM forecasts WHERE forecast_id = ?", (forecast_id,)
    ).fetchone()
    if forecast is None:
        raise LedgerError(f"unknown forecast {forecast_id}")
    condition_id = forecast["condition_id"]
    market = conn.execute(
        "SELECT * FROM markets WHERE condition_id = ?", (condition_id,)
    ).fetchone()
    if market is None:
        raise LedgerError(f"forecast {forecast_id} is for an unknown market")
    rules = conn.execute(
        "SELECT rules_json FROM rules_versions WHERE condition_id = ? AND rules_hash = ?",
        (condition_id, forecast["rules_hash"]),
    ).fetchone()
    end_text = json.loads(rules["rules_json"]).get("end_date") if rules else None
    baseline = conn.execute(
        "SELECT yes_snapshot_id, no_snapshot_id FROM forecast_baselines "
        "WHERE forecast_id = ? AND reason IS NULL",
        (forecast_id,),
    ).fetchone()
    books = {}
    if baseline is not None:
        books = {
            "YES": _side_book(conn, baseline["yes_snapshot_id"], "YES"),
            "NO": _side_book(conn, baseline["no_snapshot_id"], "NO"),
        }
    discovery = latest_discovery_run(conn)
    eligible = discovery is not None and conn.execute(
        "SELECT 1 FROM discoveries WHERE run_id = ? AND condition_id = ?",
        (discovery, condition_id),
    ).fetchone() is not None
    started = any(
        row["status"] not in OPEN_STATUSES
        for row in conn.execute(
            "SELECT status FROM resolution_observations WHERE condition_id = ?",
            (condition_id,),
        )
    )
    traded = conn.execute(
        "SELECT 1 FROM paper_tickets WHERE portfolio_id = ? AND condition_id = ?",
        (portfolio_id, condition_id),
    ).fetchone() is not None
    return PolicyInputs(
        forecast=ForecastView(
            abstained=bool(forecast["abstained"]),
            p_low=_optional_decimal(forecast["p_low"]),
            p_mid=_optional_decimal(forecast["p_mid"]),
            p_high=_optional_decimal(forecast["p_high"]),
            confidence=forecast["confidence"],
            rules_hash=forecast["rules_hash"],
            end_date=parse_datetime(end_text) if end_text else None,
        ),
        current_rules_hash=market["current_rules_hash"],
        eligible=eligible,
        resolution_started=started,
        already_traded=traded,
        books=books,
        equity=equity(conn, portfolio_id),
        available_cash=available_cash(conn, portfolio_id),
        exposure=_exposure(
            conn, portfolio_id, condition_id, market["event_id"], market["category"]
        ),
        now=now,
    )


def _policy(conn: sqlite3.Connection, policy_hash: str) -> PolicyParams:
    kind, content = load_artifact(conn, policy_hash)
    if kind != "policy":
        raise LedgerError(f"artifact {policy_hash[:12]} is not a policy")
    try:
        return policy_from_artifact(content)
    except ConfigError as error:
        raise LedgerError(f"policy artifact {policy_hash[:12]} is unreadable: {error}") from None


def decide_portfolio(
    conn: sqlite3.Connection, portfolio_id: str, forecast_id: int, now: datetime
) -> DecisionResult:
    with transaction(conn):
        portfolio = conn.execute(
            "SELECT p.policy_hash, c.status FROM portfolios p "
            "JOIN cohorts c ON c.cohort_id = p.cohort_id WHERE p.portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()
        if portfolio is None:
            raise LedgerError(f"unknown portfolio {portfolio_id[:12]}")
        if portfolio["status"] != "ACTIVE":
            detail = "cohort closed before this forecast was decided"
            record_refusal_locked(conn, portfolio_id, forecast_id, COHORT_CLOSED, now, detail)
            return DecisionResult(portfolio_id, forecast_id, None, COHORT_CLOSED, detail)
        params = _policy(conn, portfolio["policy_hash"])
        inputs = load_inputs(conn, portfolio_id, forecast_id, now)
        result = decide(inputs, params)
        if not isinstance(result, Trade):
            record_refusal_locked(
                conn, portfolio_id, forecast_id, result.reason, now, result.detail
            )
            return DecisionResult(portfolio_id, forecast_id, None, result.reason, result.detail)
        forecast = conn.execute(
            "SELECT condition_id, rules_hash FROM forecasts WHERE forecast_id = ?",
            (forecast_id,),
        ).fetchone()
        draft = TicketDraft(
            portfolio_id=portfolio_id,
            forecast_id=forecast_id,
            snapshot_id=result.snapshot_id,
            condition_id=forecast["condition_id"],
            outcome=result.outcome,
            fills=result.walk.fills,
            fee=result.walk.fee,
            policy_hash=portfolio["policy_hash"],
            rules_hash=forecast["rules_hash"],
        )
        ticket_id = open_ticket_locked(conn, draft, now)
        detail = f"{result.outcome} edge {result.edge} cost {result.walk.cost}"
        return DecisionResult(portfolio_id, forecast_id, ticket_id, None, detail)


def trade_ready(conn: sqlite3.Connection, now: datetime) -> TradeSummary:
    """Decide every entry forecast whose baseline is attached, in every cohort (a closed
    cohort's forecasts are refused COHORT_CLOSED). Safe to rerun: decided portfolios are
    skipped."""
    traded = 0
    refused: Counter[str] = Counter()
    waiting = 0
    cohorts = [row["cohort_id"] for row in conn.execute("SELECT cohort_id FROM cohorts")]
    for cohort_id in cohorts:
        for forecast_id in unfinished_forecasts(conn, cohort_id):
            if resume_step(conn, forecast_id, now) is not ResumeStep.NEEDS_DECISION:
                waiting += 1
                continue
            for portfolio_id in undecided_portfolios(conn, forecast_id):
                result = decide_portfolio(conn, portfolio_id, forecast_id, now)
                if result.ticket_id is not None:
                    traded += 1
                else:
                    refused[result.reason or ""] += 1
    return TradeSummary(traded, dict(refused), waiting)

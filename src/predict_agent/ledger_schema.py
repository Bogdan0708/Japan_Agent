"""Ledger tables (Plan 2). Every table is append-only except the documented transitions:
research_attempts STARTED -> SUCCEEDED|FAILED, paper_tickets OPEN -> SETTLED and
cohorts ACTIVE -> CLOSED.

A cohort is one research identity (prompt, model, research settings, scoring version,
baseline window) and owns the shared forecasts and baselines. Each cohort has one or more
portfolios (variant `primary` plus pre-registered shadows such as `shadow_mid`), each with
its own frozen policy, FUNDING entry, cash, tickets and decisions (spec §4, §5)."""

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL
        CHECK (kind IN ('prompt', 'policy', 'research_input', 'tool_transcript')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cohorts (
    cohort_id TEXT PRIMARY KEY,
    identity_json TEXT NOT NULL,
    code_version TEXT NOT NULL,
    starting_bankroll TEXT NOT NULL,
    baseline_window_seconds INTEGER NOT NULL CHECK (baseline_window_seconds > 0),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED')),
    started_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_cohort ON cohorts (status) WHERE status = 'ACTIVE';
CREATE TABLE IF NOT EXISTS portfolios (
    portfolio_id TEXT PRIMARY KEY,
    cohort_id TEXT NOT NULL REFERENCES cohorts (cohort_id),
    variant TEXT NOT NULL,
    policy_hash TEXT NOT NULL REFERENCES artifacts (artifact_hash),
    starting_bankroll TEXT NOT NULL,
    UNIQUE (cohort_id, variant)
);
CREATE TABLE IF NOT EXISTS research_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cohort_id TEXT NOT NULL REFERENCES cohorts (cohort_id),
    condition_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('entry', 'update')),
    started_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('STARTED', 'SUCCEEDED', 'FAILED')),
    finished_at TEXT,
    cost_usd TEXT,
    error TEXT,
    CHECK ((status = 'STARTED') = (finished_at IS NULL)),
    CHECK ((status = 'STARTED') = (cost_usd IS NULL)),
    CHECK ((status = 'FAILED') = (error IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL UNIQUE REFERENCES research_attempts (attempt_id),
    cohort_id TEXT NOT NULL REFERENCES cohorts (cohort_id),
    condition_id TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('entry', 'update')),
    created_at TEXT NOT NULL,
    abstained INTEGER NOT NULL,
    abstain_reason TEXT,
    p_low TEXT,
    p_mid TEXT,
    p_high TEXT,
    confidence TEXT,
    base_rate TEXT,
    body_json TEXT NOT NULL,
    research_input_hash TEXT NOT NULL REFERENCES artifacts (artifact_hash),
    transcript_hash TEXT NOT NULL REFERENCES artifacts (artifact_hash),
    cost_usd TEXT NOT NULL,
    forecast_hash TEXT NOT NULL UNIQUE,
    FOREIGN KEY (condition_id, rules_hash) REFERENCES rules_versions (condition_id, rules_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_entry_forecast_per_market
    ON forecasts (cohort_id, condition_id) WHERE kind = 'entry';
CREATE TABLE IF NOT EXISTS forecast_baselines (
    forecast_id INTEGER PRIMARY KEY REFERENCES forecasts (forecast_id),
    yes_snapshot_id INTEGER REFERENCES book_snapshots (id),
    no_snapshot_id INTEGER REFERENCES book_snapshots (id),
    reason TEXT,
    attached_at TEXT NOT NULL,
    CHECK (
        (yes_snapshot_id IS NOT NULL AND no_snapshot_id IS NOT NULL AND reason IS NULL)
        OR (yes_snapshot_id IS NULL AND no_snapshot_id IS NULL AND reason IS NOT NULL)
    )
);
CREATE TABLE IF NOT EXISTS paper_tickets (
    ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT NOT NULL REFERENCES portfolios (portfolio_id),
    forecast_id INTEGER NOT NULL REFERENCES forecasts (forecast_id),
    snapshot_id INTEGER NOT NULL REFERENCES book_snapshots (id),
    condition_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('YES', 'NO')),
    direction TEXT NOT NULL CHECK (direction = 'BUY'),
    shares TEXT NOT NULL,
    fills_json TEXT NOT NULL,
    fee TEXT NOT NULL,
    cost_total TEXT NOT NULL,
    policy_hash TEXT NOT NULL REFERENCES artifacts (artifact_hash),
    rules_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'SETTLED')),
    ticket_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (portfolio_id, condition_id),
    UNIQUE (portfolio_id, forecast_id)
);
CREATE TABLE IF NOT EXISTS decisions (
    portfolio_id TEXT NOT NULL REFERENCES portfolios (portfolio_id),
    forecast_id INTEGER NOT NULL REFERENCES forecasts (forecast_id),
    condition_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('TRADED', 'REFUSED', 'NO_TIMELY_BASELINE')),
    reason TEXT,
    ticket_id INTEGER REFERENCES paper_tickets (ticket_id),
    decided_at TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, forecast_id),
    CHECK ((kind = 'TRADED') = (ticket_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS cash_ledger (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT NOT NULL REFERENCES portfolios (portfolio_id),
    entry_type TEXT NOT NULL CHECK (entry_type IN ('FUNDING', 'DEBIT', 'CREDIT')),
    amount TEXT NOT NULL,
    ticket_id INTEGER REFERENCES paper_tickets (ticket_id),
    at TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE,
    CHECK ((entry_type = 'FUNDING') = (ticket_id IS NULL)),
    UNIQUE (ticket_id, entry_type)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_funding_per_portfolio
    ON cash_ledger (portfolio_id) WHERE entry_type = 'FUNDING';
CREATE TABLE IF NOT EXISTS settlements (
    ticket_id INTEGER PRIMARY KEY REFERENCES paper_tickets (ticket_id),
    observation_id INTEGER NOT NULL REFERENCES resolution_observations (id),
    outcome TEXT NOT NULL CHECK (outcome IN ('YES', 'NO', 'HALF')),
    payout_per_share TEXT NOT NULL,
    payout TEXT NOT NULL,
    net_pnl TEXT NOT NULL,
    rules_hash_at_settlement TEXT NOT NULL,
    settled_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS artifacts_no_update BEFORE UPDATE ON artifacts
BEGIN SELECT RAISE(ABORT, 'artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS artifacts_no_delete BEFORE DELETE ON artifacts
BEGIN SELECT RAISE(ABORT, 'artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS portfolios_no_update BEFORE UPDATE ON portfolios
BEGIN SELECT RAISE(ABORT, 'portfolios are immutable'); END;
CREATE TRIGGER IF NOT EXISTS portfolios_no_delete BEFORE DELETE ON portfolios
BEGIN SELECT RAISE(ABORT, 'portfolios are immutable'); END;
CREATE TRIGGER IF NOT EXISTS attempts_no_delete BEFORE DELETE ON research_attempts
BEGIN SELECT RAISE(ABORT, 'research attempts are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS attempts_finish_once BEFORE UPDATE ON research_attempts
WHEN NOT (
    OLD.status = 'STARTED' AND NEW.status IN ('SUCCEEDED', 'FAILED')
    AND NEW.attempt_id IS OLD.attempt_id AND NEW.cohort_id IS OLD.cohort_id
    AND NEW.condition_id IS OLD.condition_id AND NEW.kind IS OLD.kind
    AND NEW.started_at IS OLD.started_at
)
BEGIN SELECT RAISE(ABORT, 'research attempts only move STARTED -> SUCCEEDED|FAILED'); END;
CREATE TRIGGER IF NOT EXISTS forecasts_no_update BEFORE UPDATE ON forecasts
BEGIN SELECT RAISE(ABORT, 'forecasts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecasts_no_delete BEFORE DELETE ON forecasts
BEGIN SELECT RAISE(ABORT, 'forecasts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS baselines_no_update BEFORE UPDATE ON forecast_baselines
BEGIN SELECT RAISE(ABORT, 'baselines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS baselines_no_delete BEFORE DELETE ON forecast_baselines
BEGIN SELECT RAISE(ABORT, 'baselines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS cash_no_update BEFORE UPDATE ON cash_ledger
BEGIN SELECT RAISE(ABORT, 'cash ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS cash_no_delete BEFORE DELETE ON cash_ledger
BEGIN SELECT RAISE(ABORT, 'cash ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS settlements_no_update BEFORE UPDATE ON settlements
BEGIN SELECT RAISE(ABORT, 'settlements are immutable'); END;
CREATE TRIGGER IF NOT EXISTS settlements_no_delete BEFORE DELETE ON settlements
BEGIN SELECT RAISE(ABORT, 'settlements are immutable'); END;
CREATE TRIGGER IF NOT EXISTS observations_no_delete BEFORE DELETE ON resolution_observations
BEGIN SELECT RAISE(ABORT, 'resolution observations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS tickets_no_delete BEFORE DELETE ON paper_tickets
BEGIN SELECT RAISE(ABORT, 'tickets are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS tickets_open_to_settled_only BEFORE UPDATE ON paper_tickets
WHEN NOT (
    OLD.status = 'OPEN' AND NEW.status = 'SETTLED'
    AND NEW.ticket_id IS OLD.ticket_id AND NEW.portfolio_id IS OLD.portfolio_id
    AND NEW.forecast_id IS OLD.forecast_id AND NEW.snapshot_id IS OLD.snapshot_id
    AND NEW.condition_id IS OLD.condition_id AND NEW.outcome IS OLD.outcome
    AND NEW.direction IS OLD.direction AND NEW.shares IS OLD.shares
    AND NEW.fills_json IS OLD.fills_json AND NEW.fee IS OLD.fee
    AND NEW.cost_total IS OLD.cost_total AND NEW.policy_hash IS OLD.policy_hash
    AND NEW.rules_hash IS OLD.rules_hash AND NEW.ticket_hash IS OLD.ticket_hash
    AND NEW.created_at IS OLD.created_at
)
BEGIN SELECT RAISE(ABORT, 'tickets only move OPEN -> SETTLED'); END;
CREATE TRIGGER IF NOT EXISTS cohorts_no_delete BEFORE DELETE ON cohorts
BEGIN SELECT RAISE(ABORT, 'cohorts are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS cohorts_active_to_closed_only BEFORE UPDATE ON cohorts
WHEN NOT (
    OLD.status = 'ACTIVE' AND NEW.status = 'CLOSED' AND NEW.closed_at IS NOT NULL
    AND NEW.cohort_id IS OLD.cohort_id AND NEW.identity_json IS OLD.identity_json
    AND NEW.code_version IS OLD.code_version
    AND NEW.starting_bankroll IS OLD.starting_bankroll
    AND NEW.baseline_window_seconds IS OLD.baseline_window_seconds
    AND NEW.started_at IS OLD.started_at
)
BEGIN SELECT RAISE(ABORT, 'cohorts only move ACTIVE -> CLOSED'); END;
"""

# Architecture and trust boundaries

## Data flow

```text
timestamped local sources
  -> SQLite snapshots + successful-ingest heartbeats
  -> fail-closed research bundle
  -> Claude structured decision (untrusted)
  -> deterministic risk engine
  -> immutable ticket + SHA-256 hash
  -> Telegram human approval of exact hash
  -> fresh quote + fresh T212 portfolio risk preflight
  -> one non-idempotent broker POST
  -> reconciliation + hash-chained event ledger
  -> redacted/derived public journal
```

## Hard boundaries

`research/` does not import `execute/`. Claude receives only a serialized snapshot and has no tools.
The broker receives only a fixed ticket that already exists in SQLite with a matching approval hash.
Ticker, side, quantity, expiry, price tolerance, thesis, evidence, and invalidation condition are
immutable at the database level.

An order is not submitted if any of these is false:

- environment gate and local kill switch pass;
- proposal is approved, unexpired, and its hash still matches;
- quote is for the same ticker, no more than 20 minutes old, and within the approved 3% move;
- portfolio is marked `TRADING212` and no more than two minutes old;
- instrument remains on the verified cash-equity/ETF whitelist;
- fresh cash, holdings, position caps, satellite cap, and weekly trade count still pass;
- no execution claim exists for the proposal.

## Non-idempotent submission

Trading 212 documents the market-order endpoint as non-idempotent. The database claims the proposal
before the HTTP request. A definite 4xx rejection becomes `FAILED`. A timeout, connection loss, 408,
429, 5xx, malformed success body, or missing order ID becomes `RECONCILIATION_REQUIRED`. The same
proposal is never POSTed again. A human must compare broker history before creating a new ticket.

## Event and public-journal model

SQLite is the source of truth. Each event stores the previous event hash; `journal-sync` produces a
local JSONL mirror atomically. The public Markdown generator receives only derived decisions and
reconciled P&L. It must not publish API keys, Telegram IDs, account IDs, full event payloads, or raw
licensed market data.

## Phase status

| Phase | State | Exit work |
|---|---|---|
| 0 accounts/identity | Human-blocked | Create accounts, keys, bot, email and machine user |
| 1 scaffold | Foundation complete | Verify demo instruments and observed response schemas |
| 2 paper pipeline | Core implemented | Add production collectors, T212 portfolio mapper and scheduler |
| 3 journal | Generator/skeleton | Reconcile broker statement and deploy Pages under agent account |
| 4 track record | Not started | 14+ manual-fix-free days, no breaches, clean journal |
| 5 live | Hard-blocked | Written T212 confirmation plus signed live gate and £100 funding |


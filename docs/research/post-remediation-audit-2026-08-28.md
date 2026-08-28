# Post-remediation audit of `25b0891` (2026-08-28)

> **Resolution addendum (same day, follow-up commit):** the three account-free
> blockers below were addressed — (1) `weekly.sh` now ingests Sunday digests,
> refreshes quotes, runs a distinct `research --mode weekly` portfolio review
> against `data/portfolio.json`, and **exits non-zero when the review refuses**;
> (2) the gate rejects `observed_through` beyond now + 15 minutes for every
> source, and EDINET records `min(end-of-date in Asia/Tokyo, retrieved_at)`
> (daily.sh ingests yesterday before today so the last heartbeat is freshest);
> (3) each research run persists a manifest (run id, snapshot hash, exact
> permitted citation IDs) and proposal validation checks citations against that
> manifest — the 200-day-old-item probe now fails with
> `EVIDENCE_UNKNOWN_SOURCE`, a missing/unrecorded run fails with
> `EVIDENCE_NO_RUN`, and a run older than 24h with `EVIDENCE_STALE_RUN`.
> `doctor` now reports grouped states (setup / credentials / dependencies /
> data-freshness / integrity) including SDK/yfinance importability, J-Quants
> code config, and a live freshness-gate evaluation. Committed regression tests
> cover future coverage, EDINET partial-day coverage, clock skew, manifest
> binding, and ledger UPDATE/DELETE rejection plus hash/reorder tampering
> (62 tests, ResourceWarning as errors). **Known residual:** the manifest
> binds citation IDs and the bundle hash, but does not hash each item's
> payload — a post-run upsert of a cited item's text is not detected; the
> ticket embeds the claim text at proposal time and the ledger records it.

This document preserves the verification performed after commit `25b0891`
claimed closure of the account-free audit blockers. It is an engineering audit,
not legal, tax, or investment advice. No credentials, account identifiers, raw
licensed market data, or Telegram payloads were inspected or recorded.

## Outcome

The remediation is substantial and the principal money-safety fixes work, but
three account-free false-green paths remain. Do not start the official
manual-fix-free paper-track clock until they are closed:

1. the Sunday research job normally refuses stale NEWS/TDnet data but the
   wrapper still exits successfully;
2. the freshness gate accepts arbitrarily future `observed_through` values and
   EDINET claims more current-day coverage than it retrieved; and
3. citations are checked against the whole mutable database, not the exact
   snapshot manifest supplied to the model.

The demo-derived Trading 212 portfolio mapper and execution worker remain
properly deferred until real demo payloads exist. Live trading remains blocked
until Trading 212 affirmatively addresses API Terms clauses 4.2(a) and 6.7 for
the exact workflow.

## Checkout and verification evidence

State observed at the start of the audit:

```text
HEAD: 25b0891 Fix audit blockers: GBp pence, freshness gate, citations, Dietz, polling
branch: main
worktree: clean
```

The repository subsequently gained commit `7a3e504`, which archives the
research documents. The code findings below concern `25b0891`.

Commands run:

```bash
PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -v
bash -n scripts/daily.sh scripts/weekly.sh
python3 -m compileall -q src tests
git diff --check 25b0891^ 25b0891
```

Results:

- 46 tests passed in 2.408 seconds; `ResourceWarning` was promoted to an error.
- Both shell wrappers passed Bash syntax validation.
- Source and tests compiled successfully.
- The commit contained no whitespace errors.
- `flock` and `jq` were installed on the audited host.
- `pytest`, `ruff`, and `mypy` were not installed in the active Python 3.14
  environment, so the declared lint/type-check gates were not independently
  rerun. The canonical zero-network unittest gate was rerun exactly.

## Confirmed remediations

### Yahoo London-pence normalisation

`normalize_currency_code()` now detects Yahoo's case-sensitive `GBp` before
uppercasing and canonicalises it to `GBX`. The canonical form is applied at the
yfinance adapter, collector comparison, importer, and GBP normaliser. The
regression test uses the real vendor spelling and proves 850 pence becomes
£8.50. This closes the silent 100x-pricing path identified in the first audit.

### Telegram polling

The HTTP timeout for `getUpdates` is the server-side long-poll wait plus ten
seconds, and the API poller requests `deleteWebhook` before polling. This fixes
the prior 50-second/15-second timeout mismatch without dropping queued updates.
A webhook-removal failure is logged and polling continues; a future service
health check should make a permanently failing poller visible to supervision.

### Decision-file and run isolation

The wrappers use non-blocking `flock`, write research output to a temporary
file, validate it as JSON, and atomically rename it. A failed research command
can no longer truncate the last valid decision. The daily and weekly jobs use
different lock files, so cross-job manual overlap is still possible, although
the configured cron schedules do not overlap.

### Research-context controls

Per-source quotas prevent EDINET volume from consuming the full context:
EDINET 35, TDnet 30, NEWS 25, and J-Quants 10. The prompt explicitly labels
third-party snapshot content as untrusted data, tells the model to ignore
embedded instructions, supplies no tools, and constrains evidence strings to a
structured citation prefix.

### Ledger integrity

SQLite triggers reject `UPDATE` and `DELETE` against `events`, and
`verify_event_chain()` recomputes every link and event hash. A focused temporary
database probe confirmed both update rejection and successful verification.
This is append-only protection inside the application schema; the database
file owner can still replace the file or drop triggers. Committing the exported
JSONL history provides the intended external anchor.

### Performance reporting and frontier risk

Weekly reporting now uses Modified Dietz with dated, day-weighted cash flows and
rejects flows outside the reporting period. The method note defines comparison
against GBP total-return benchmarks over the identical period. `FRONTIER` is a
separate research sleeve but shares the deterministic £15 absolute cap with
`SATELLITE`; the £5 cash floor and 40% per-instrument cap remain unchanged.

## Remaining blocker 1: Sunday research returns a false green

`SOURCE_MAX_AGES` requires NEWS within 24 hours and TDnet within 36 hours. The
normal daily cadence finishes on Friday at 07:15 London. At Sunday 10:00, the
Friday NEWS heartbeat is roughly 51 hours old and TDnet is similarly outside
its limit.

`scripts/weekly.sh` refreshes J-Quants but neither generates nor ingests a
Sunday NEWS/TDnet digest. It invokes the same `research` command used by the
daily pass. On refusal it deletes the temporary decision, logs a message, then
continues to ledger verification and journal rendering. The wrapper therefore
usually exits zero even though the advertised deep research did not occur.

Required resolution:

- generate and ingest fresh Sunday NEWS and TDnet inputs before research, or
  define a deliberate weekend policy that still protects catalyst freshness;
- if deep weekly research is a required job outcome, propagate its refusal as
  a non-zero job status rather than converting it to success; and
- introduce an explicit weekly research/portfolio-review mode if the intended
  behavior includes drift, overlap, and rebalance analysis. Calling the same
  one-decision prompt does not by itself make the pass "deep."

Tests required:

- Friday heartbeat followed by Sunday run;
- fresh Sunday inputs;
- research refusal produces a visible failed job; and
- optional journal rendering does not hide the failed research result.

## Remaining blocker 2: observation coverage can point arbitrarily forward

The gate calculates:

```text
observation_lag = now - observed_through
reject only when observation_lag > source_max_age
```

There is no negative-lag/future bound. A focused probe recorded
`observed_through = now + 365 days` for PRICE, EDINET, TDnet, and NEWS; the
snapshot assembled successfully.

The EDINET ingester also records the requested filing date at 23:59:59 UTC.
That is neither the end of the date in `Asia/Tokyo` nor proof that a current-day
request observed filings through the rest of the day. At 07:15 London, a query
of the current Tokyo date is necessarily only complete through retrieval time.

Required resolution:

- for a completed historical date, use end-of-day in `Asia/Tokyo`, converted to
  UTC;
- for the current Tokyo date, use `min(retrieved_at, tokyo_end_of_day)`;
- reject `observed_through` later than `now` plus a small documented clock-skew
  allowance; and
- record one batch coverage result after the intended EDINET date range is
  complete, or query yesterday before today so the latest heartbeat represents
  the newest observation window.

Tests required:

- re-fetching stale data remains blocked;
- current-day partial coverage uses retrieval time;
- a small allowed clock skew passes;
- a materially future window fails; and
- the two-date EDINET batch reports the newest honest coverage.

## Remaining blocker 3: citations are not bound to the model snapshot

The research prompt claims that a citation absent from the supplied snapshot is
rejected automatically. `ProposalWorkflow` actually checks only:

- PRICE: whether any latest snapshot exists for the cited ticker; or
- other sources: whether `(source, external_id)` exists anywhere in
  `research_items`.

The research bundle includes only items published within 120 days and applies
per-source quotas. A focused probe inserted an EDINET record published 200 days
earlier, outside the model bundle, then proposed a decision citing that ID. A
ticket was created successfully:

```text
old_citation_ticket_created= True
```

Database existence prevents a wholly invented ID but does not prove the model
saw the item, that it belonged to the research run, or that an upsert did not
change its payload after the decision.

Required resolution:

- persist a research-run record with a run ID and canonical snapshot hash;
- persist the exact allowed evidence IDs and exact price snapshot identifiers
  or timestamps for that run;
- link the structured decision to the research-run ID; and
- validate proposal citations against that immutable manifest, not the entire
  database.

Tests required:

- an ID present in the exact manifest passes;
- an old DB item omitted from the manifest fails;
- an item excluded by a source quota fails;
- a price reference from another observation/run fails; and
- mutation/upsert after the research run cannot change the evidence being
  approved.

## Secondary gaps

### `doctor` remains a setup check, not a complete readiness proof

The expanded twelve checks are useful, but a green result still does not prove:

- `yfinance` and Claude Agent SDK are importable;
- `config/jquants-codes.json` exists and covers the intended context set;
- digest production is operational;
- required source heartbeats and observation windows are currently fresh;
- data-symbol values are non-empty and structurally valid;
- Telegram polling is healthy; or
- T212 connectivity and demo permissions work.

Account/network checks should remain separate where appropriate, but the
command should distinguish static configuration, dependency readiness, data
freshness, transport health, and broker connectivity rather than presenting one
undifferentiated green state.

### Regression coverage

The committed suite directly tests GBp, citations by database existence,
Modified Dietz, Telegram timeout, and the FRONTIER cap. At the audited commit it
did not directly exercise future `observed_through`, exact snapshot binding,
event UPDATE/DELETE triggers, hash tampering, Sunday stale-news behavior, or the
wrapper exit status. The focused probes above cover the first three only as
audit evidence; they should become committed regression tests.

## Account-bound work deliberately left open

These are not defects to guess around:

- generate a Trading 212 demo key with minimum necessary permissions;
- inspect real account summary, position, instrument, fractional-quantity, and
  order payloads;
- implement and test the broker-payload-to-`PortfolioSnapshot` mapper;
- implement the separate execution worker using a quote observed within 20
  minutes and broker portfolio state observed within two minutes;
- run the complete demo approval/fill/reconciliation path; and
- begin the 2-4 week clock only after the full loop runs without manual fixes.

Live remains separately blocked by the paper-track gate and an affirmative
Trading 212 reply for the exact AI-sizing plus named-human-approval workflow.

## Acceptance gate after the next patch

Before calling the account-free layer complete:

1. all existing 46 tests still pass with resource warnings as errors;
2. the new future-coverage, snapshot-manifest, ledger-tamper, and Sunday-job
   tests pass;
3. Sunday research either receives fresh inputs and produces a validated
   decision or returns a non-zero, supervised refusal;
4. `doctor` reports separate, meaningful states and cannot be green while core
   optional dependencies or scheduler inputs are missing; and
5. documentation describes actual guarantees rather than database-wide or
   wrapper-level approximations.

## Audit probe outputs

The focused probes used temporary databases only and did not modify the
repository:

```text
old_citation_ticket_created= True
one_year_future_observed_through_accepted= True
event_update_trigger_blocks= True
verified_events= 1
```

These values are preserved because they distinguish two remaining false greens
from the ledger remediation that genuinely worked.

# Polymarket Paper Forecaster — Phase 1 Design

Date: 2026-10-05. Status: draft for review. Branch: `design/polymarket-paper-forecaster`.

## 1. Intent

**What the user said.** Investing via Trading 212 and similar is no longer worth it; refocus the project on Polymarket-style prediction markets, phased: a paper forecaster first, then real money on a UK-legal venue only if paper results justify it. Freeze the existing Japan-equities code in place. Focus on geopolitics/world events, politics/elections, and economics/macro. The Phase 1 → Phase 2 go/no-go is the user's judgment, informed by the metrics this system produces.

**Assumptions.** The user is UK-resident. The paper-first, fail-closed, deterministic-money philosophy of the existing project carries over.

**Success for Phase 1.** A forward-only record of Claude forecasts made without the market price on real Polymarket markets, with paper trades simulated against real order books, settled at real resolutions, producing a report that lets the user judge: (a) is Claude better calibrated than the market, and (b) would trading on it have made money after fees and slippage.

**Non-goals.** No real-money trading, no wallet/keys/signing, no execution code of any kind, no Telegram approval, no backtesting on historical markets, no public journal, no intraday/websocket trading, no changes to `src/japan_agent/`.

## 2. Constraints established by research (verified 2026-10-05)

- **Polymarket blocks new trades from the UK.** `GET https://polymarket.com/api/geoblock` from the user's connection returned `{"blocked":true,"country":"GB"}`. Help-centre lists GB as restricted; developer docs say "close-only". VPN circumvention violates ToS. The UK Gambling Commission (Feb 2026) treats prediction markets as betting intermediation requiring a licence. Kalshi also prohibits UK-domiciled trading. Nothing in this project may attempt to circumvent geoblocking.
- **Public read APIs work from the UK without auth** (verified live): Gamma `https://gamma-api.polymarket.com` (events/markets metadata) and CLOB `https://clob.polymarket.com/book?token_id=…` (order books). Data API `https://data-api.polymarket.com` also public.
- **Observed API shapes** (live sample): prices/sizes are decimal strings; `clobTokenIds`, `outcomes`, `outcomePrices` are JSON-encoded strings inside JSON; market-level `category` can be `null` (category must come from event tags); books carry `timestamp` (ms epoch — the observation time), `hash`, `tick_size`, `min_order_size`, `neg_risk`; bids were returned worst-price-first and must be sorted.
- **Fees:** takers pay `shares × feeRate × p × (1 − p)` with `feeRate` 0.04–0.07 by category; geopolitics is fee-free; makers pay none. Fee rate must be read per market, never assumed.
- **Settlement:** winning token redeems $1 (pUSD on Polygon since CLOB V2, April 2026), losing $0; UMA optimistic oracle with 2h challenge window; disputes take ~4–6 days; exceptional "unknown" resolutions pay $0.50.
- **Edge evidence is weak.** LLM forecasting accuracy approaches skilled humans (ForecastBench, Metaculus FutureEval Spring 2026), but a 2026 live-trading study on Kalshi found six models all lost 16–31%. Calibration does not imply net profit — hence measuring both.
- **UK-legal Phase 2 venues:** Betfair Exchange and Smarkets (active UKGC remote betting-intermediary licences, both with APIs).
- **Documentation inconsistencies** to pin with contract tests: minimum order size described in shares (CLOB) vs USDC (Gamma market details); fees denominated in USDC vs settlement in pUSD.

Independent prior analysis: `docs/POLYMARKET-PIVOT-PLAN.md` (untracked draft from another session). This spec adopts its correlated-exposure, unit-ambiguity, double-counted-spread, research-cost, probability-range/abstention, baseline, frozen-cohort and data-first-milestone points, and its subsequent review of commit `5a3c1b7` (forecast-before-price ordering, no voiding, cash ledger and restart, immutable artifacts, scoring population, enforceable tool isolation, sizing/fee/unit corrections, child-market eligibility and resolution-state settlement); it departs from it on package location (separate package, per the user's freeze decision) and category focus (per the user's choice).

## 3. Architecture

New package `src/predict_agent/`, stdlib-only core, **no imports from `japan_agent`**. Small primitives (canonical JSON + SHA-256, UTC clock helpers, `.env` loader, hash-chained journal) are copied, not imported, so the frozen package can later be deleted without breakage. New console script `predict-agent` in `pyproject.toml`. Optional `research` extra (Claude Agent SDK) is lazily imported with a clear error, as today.

Run cadence: once daily from cron via `scripts/predict-daily.sh` (flock, atomic writes), mirroring `scripts/daily.sh`.

### Data flow

Ordering rule: **the forecast is committed before any execution or baseline price is observed.** The book used for the paper fill and for the market baseline is always fetched after the forecast row is durably written, so a fill can never use a price that predates information Claude saw.

| Stage | Module | Input → output | Fails closed on |
|---|---|---|---|
| 1 discover | `ingest/gamma.py` | Gamma `/events` (tags: geopolitics, politics, economics) → per-**child-market** eligibility → `markets` + immutable `rules_versions` | child market closed / not accepting orders / resolution already proposed or known (via resolution-state API); unmapped tags; missing rules text; non-binary; neg-risk (excluded in Phase 1); liquidity below floor; end date outside 2–90 days; outcome already effectively known (best ask ≥ 0.98 or ≤ 0.02 on either token) |
| 2 research | `research/forecaster.py` | question + pinned `rules_version` + end date + today (**no price**) → `forecasts` (committed) | schema invalid, uncited/unfetched citations, budget exhausted |
| 3 snapshot | `ingest/clob.py` | after forecast commit: CLOB `/book` for both tokens → `book_snapshots`; linked to the forecast as its baseline/execution snapshot | book last-change time in the future (clock skew), empty or crossed side, unparseable levels |
| 4 recheck + policy | `policy.py` (pure) | forecast + post-forecast snapshot + current rules/fees/eligibility + ledger state → decision or refusal | rules version changed since forecast, fee schedule missing, eligibility lost, see §5 |
| 5 paper-fill | `paper.py` | decision + snapshot → ticket + cash debit in **one SQLite transaction** | insufficient depth |
| 6 resolve | `ingest/resolution.py` | resolution-state API for **every forecasted market** (traded or not) → `resolutions`; settles tickets with credit in one transaction | status not final |
| 7 report | `report.py` | DB → Markdown + JSON in `data/reports/` | — |

CLI: `predict-agent doctor | discover | research | snapshot | trade | resolve | report | run-daily`.

### Forecasting without the price

Claude is never given the market price, order book, or volume, and its tools are restricted to keep it from finding them (§6). Results are described as **"no detected price exposure"**, not as proven blind: detection is best-effort. Rationale: a model that sees the price anchors to it, so Claude-vs-market would measure imitation. (Price-aware model/crowd blends can forecast better — Halawi et al. 2024 — but Phase 1 deliberately tests Claude's independent signal.)

## 4. Data model and accounting

SQLite at `data/predict.sqlite3`; directories 0700, files 0600; schema-version table from day one. The hash-chained journal is a **table inside the same database**, written in the same transaction as the state change it records; `journal/predict.jsonl` is an export, never the source of truth. (This avoids the existing project's open defect where state and ledger commit separately.)

**Immutable artifacts** (content-addressed by SHA-256, never updated or deleted): `rules_versions` (full rules text, resolution source, end date as seen), `prompt_artifacts` (full prompt template text), `policy_artifacts` (full policy JSON content), `research_inputs` (exact rendered prompt sent), `tool_transcripts` (every model-visible tool call and result, see §6). Forecasts, tickets and cohorts reference artifacts by hash, so any result can be reconstructed even after config files change.

- `markets`: condition_id (PK), yes/no token ids, event_id, question, current_rules_hash, category (from event tags), neg_risk, first_seen_at, last_seen_at. Mutable pointer; history lives in `rules_versions`.
- `book_snapshots`: id, condition_id, token_id, observed_at (book's own ms timestamp → UTC; this is the book's **last-change** time), fetched_at, book_hash, bids/asks (Decimal-text levels, best-first), tick_size, min_order_size, fee_schedule (fees_enabled + fee_rate as served at that time), snapshot_hash.
- `forecasts`: id, cohort_id, condition_id, rules_hash, kind (`entry`|`update`), created_at, abstained, abstain_reason, p_low/p_mid/p_high (Decimal in [0.01, 0.99], p_low ≤ p_mid ≤ p_high; null if abstained), confidence, base_rate, evidence + citations, rules_interpretation, research_input_hash, transcript_hash, exposure_flags, cost_usd, forecast_hash, baseline_snapshot_ids (set in stage 3; null + reason if the snapshot failed).
- `stage_runs`: (cohort_id, condition_id, stage) → status, durable ids produced, attempted_at. Drives resume.
- `paper_tickets`: id, cohort_id, forecast_id, snapshot_id, condition_id, outcome (YES/NO token bought), direction (`BUY`), shares, fills (per-level price × shares), fee (per-level sum), cost_total, policy_hash, rules_hash, status (`OPEN`|`SETTLED`), ticket_hash.
- `cash_ledger`: cohort_id, entry_type (`FUNDING`|`DEBIT`|`CREDIT`), amount (Decimal), ref (ticket id), at, entry_hash. **Available cash = sum of entries; it can never go negative** (enforced in the transaction).
- `resolutions`: condition_id, status (as served), payouts (as served), was_disputed, outcome (`YES`|`NO`|`HALF`|`CANCELLED`), resolved_at, rules_changed_since_first_forecast (bool).
- `refusals`, `abstentions`: run_id, cohort_id, condition_id, stage, reason_code, detail, at.
- `cohorts`: cohort_id, policy_hash, prompt_hash, model id (exact), research settings (tools, blocked domains, budgets), code_version (git commit SHA + dirty flag), scoring_version, starting_bankroll, started_at.

**Cohorts.** A change to policy content, prompt content, model id, research settings, or scoring version opens a new cohort. Each cohort is an **independent virtual portfolio** with its own `FUNDING` entry (default $1,000) — no capital is inherited. The old cohort keeps settling its open tickets but takes no new entries. Reports never pool cohorts.

**Ticket lifecycle.** `OPEN → SETTLED` only, settled from the official resolution: payout per share = the served payout for the held token (YES/NO → $1 or $0; `HALF` → $0.50). There is **no voiding**: a rules clarification never removes a position or its P&L. A market whose rules changed after the forecast is flagged (`rules_changed_since_first_forecast`) and may be excluded only from a clearly labelled *forecast-scoring* subset; its P&L always counts. `CANCELLED` is used only when the resolution API documents a cancellation, and its payout follows what is served. One entry per market per cohort, ever (no re-entry after settlement).

**Settlement mapping.** Accepted final statuses and the payouts → outcome mapping are pinned by contract tests against recorded resolution-state responses (data milestone). An unknown status or payout vector → no settlement, surfaced in the report. `was_disputed=true` with a final status settles normally; disputes delay but never block settlement.

## 5. Paper policy

Pure functions, no I/O. Parameters in `config/predict-policy.json` — human-edited; code reads, never writes (example committed as `predict-policy.example.json`).

Defaults: starting bankroll $1,000; min edge 0.05; min confidence `medium`; Kelly fraction 0.25; caps as % of **equity** — per market 2%, per event 5%, per category 15%, total open cost 50%; max slippage 2% above best ask; min time to close 48h; max book age 2 min, measured from our `fetched_at` to the policy decision (the CLOB `timestamp` is the book's last-change time, not its freshness — verified 2026-10-05).

**Equity** = available cash + cost basis of open tickets (cost basis, not mark-to-market, so sizing never depends on post-entry prices). Losses shrink equity, and therefore every cap and Kelly size.

Algorithm (per market, after the post-forecast snapshot):
1. Refuse if: forecast abstained; rules version changed since the forecast; eligibility lost; this cohort already holds or held a ticket on this market; `fees_enabled` with no fee rate in the snapshot.
2. Conservative probabilities: q_YES = p_low, q_NO = 1 − p_high. Per side, provisional edge at the best ask `a`: `q − a − fee(a)`; drop sides with edge < min_edge.
3. **Size before depth:** budget `B = min(0.25 × Kelly(q, a) × equity, every cap's remaining headroom, available cash)`, where `Kelly(q, c) = (q − c)/(1 − c)`.
4. Walk the ask book level by level spending at most `B`, including fees: each level's fee is `shares_at_level × fee_rate × price_level × (1 − price_level)` — **fees are summed per level, never computed at the average price**. Price levels respect `tick_size` (a price property); share quantity is rounded down to the venue's share precision (a quantity property, pinned by contract test). The walked prices already include spread and slippage; nothing is subtracted twice.
5. Recompute realized per-share cost `c` (total cost / shares) and edge `q − c`; refuse if edge < min_edge, slippage > 2%, shares < `min_order_size` (unit pinned by contract test; if the unit cannot be determined, refuse), or depth is insufficient. Never fabricate fills beyond recorded depth.
6. Pick the side with the larger realized edge (at most one side per market). Emit a decision referencing forecast, snapshot, policy, and rules hashes; `paper.py` writes ticket + `DEBIT` atomically.

**About the range.** Claude's p_low/p_high have **no validated coverage**; the range is an unvalidated sensitivity check, not a confidence interval. The prompt elicits it explicitly as "the lowest and highest probability you would still find defensible given the evidence". Reports show results by range width. As a **pre-registered secondary analysis**, a shadow policy using p_mid instead of the bounds is run on the same post-forecast snapshots with its own virtual cash ledger (shadow tickets are labelled and never affect the primary portfolio). The primary policy is not changed based on results within a cohort.

**Updates.** Weekly update forecasts on markets with open tickets are scored separately and never trade.

## 6. Research layer

- Claude via the Agent SDK, configured so the **available** tool set is exactly WebSearch + WebFetch: set the SDK's available-tools option (not just `allowed_tools`, which only auto-approves), `disallowed_tools` for everything else, `setting_sources=[]`, an explicit empty MCP server set, and a deny-by-default permission callback/hook. Note: the existing `japan_agent/research/claude.py` relies on `allowed_tools=[]`, which does not restrict tools; that pattern must not be copied. A startup self-check fails closed if the SDK reports any other tool available.
- **Domain enforcement on both tools:** WebSearch `blocked_domains` and a PreToolUse hook that denies WebFetch for the same list (polymarket.com, kalshi.com, betfair.com, smarkets.com, manifold.markets, metaculus.com, oddschecker.com, predictit.org, and similar; list lives in the policy artifact).
- **Exposure detection covers everything the model saw:** a PostToolUse hook records every tool result (search snippets included, cited or not) into `tool_transcripts`; the scanner runs over transcripts plus final reasoning for odds/market-probability phrasing and prediction-market names. Flags are stored per forecast; flagged forecasts are reported separately. Wording everywhere: "no detected price exposure".
- Strict JSON schema; invalid output → abstention-like refusal recorded, no retry within the run. Citations must be URLs actually fetched in that session.
- Forward-only; no historical backtests.
- **Monetary budget:** daily and per-forecast USD caps from config, enforced from the SDK's reported cost, counting failed attempts and weekly updates. Volume cap of 15 new entry forecasts/day also applies. Budget exhausted → remaining markets refused with `BUDGET`.

## 7. Error handling and restart

- HTTP: unauthenticated GETs only. Bounded retries (3, exponential backoff, jitter) on timeouts/5xx/429 within published limits (Gamma `/markets` 300/10s, CLOB `/book` 1,500/10s). 4xx or malformed body → refusal record.
- Stale/incomplete data → fail closed with a reason code; an empty shortlist is valid.
- **Resume, not skip.** Each run resumes per `(cohort, market, stage)` from `stage_runs` using durable ids: a committed forecast without a snapshot gets its snapshot; a forecast + snapshot without a decision gets a decision. If the snapshot step is reached too late (forecast older than a configured window, default 30 min), the forecast is kept for scoring with `NO_TIMELY_BASELINE` and does not trade — this prevents a crash-restart from trading on a stale forecast.
- Resolution: polled for every forecasted market until final. Markets > 14 days past end date and unresolved are surfaced, never guessed.
- Each run records `GET /api/geoblock` for audit. **No wallet, key, signing, or order-submission code exists in Phase 1.**

## 8. Reporting and testing

**Scoring population (primary).** One row per market per cohort: the **first `entry` forecast**, non-abstained, with a baseline snapshot, on a market that reached a final `YES`/`NO` resolution. Claude p_mid, the market baseline (YES mid from the same post-forecast snapshot), and the base-rate baseline are scored **on exactly these rows**. Excluded and counted separately: abstentions (rate = abstained / forecast attempts), `HALF`/`CANCELLED` resolutions (reported, not Brier-scored), unresolved markets, `NO_TIMELY_BASELINE`, exposure-flagged forecasts (scored as a separate subset), rules-changed markets (forecast-scoring subset only; P&L unaffected). Log score uses probabilities clipped to [0.01, 0.99] for all three forecasters. Update forecasts are reported in a separate section.

**Report** (`predict-agent report`, local Markdown + JSON, every section per cohort):
- Counts first: eligible markets after each exclusion, forecasts, abstentions, resolved scoring rows, distinct events.
- Brier and log score for Claude vs both baselines; by category, horizon, range width.
- 10-bucket calibration table.
- Paper P&L from the cash ledger (fees and slippage included); bootstrap CI resampling **by event** — labelled as a heuristic, since one event id does not prove independence; hit rate; edge at entry vs realized; locked capital; research cost and P&L net of it.
- Shadow p_mid policy results alongside, labelled secondary.
- Guidance (not a gate): expect 4–8 weeks, extended while resolved events are few. No automated go/no-go.

**Tests** (unittest, zero-network, `-W error::ResourceWarning`):
- Ordering: snapshot/fill can only reference a snapshot fetched after the forecast commit.
- Policy: worked example (q=0.80, ask 0.50, equity $1,000 → Kelly 300 shares, capped to 40, filled from a 50-share book); per-level fee example (20 @ 0.50 + 20 @ 0.51, rate 0.05 → 0.49990, not 0.49995); conservative bounds; abstention; each cap incl. per-event; equity shrinks after losses; cash never negative; tick vs share precision; min order size; rules-version change refuses; no re-entry.
- Book walk: multi-level, insufficient depth, worst-first input.
- Settlement: YES, NO, HALF, CANCELLED, disputed-then-final, unknown status → unsettled; rules change never removes P&L; ticket+debit and settlement+credit atomic (simulated failure mid-transaction leaves no partial state).
- Restart: crash after forecast commit → resume snapshots, no duplicate forecast; late resume → `NO_TIMELY_BASELINE`.
- Ingest/eligibility: recorded fixtures incl. closed child markets inside active events, JSON-in-string fields, null category, ms timestamps, stale/malformed variants.
- Research: available-tool self-check, WebFetch domain denial, exposure detected in an uncited search snippet, schema rejection, budget enforcement incl. failed attempts.
- Scoring: population rules above; baselines scored on identical rows.
- Isolation: `predict_agent.research` imports nothing from `policy`/`paper`; `predict_agent` imports nothing from `japan_agent`.

## 9. Repository changes

- `src/predict_agent/` (new), `tests/predict/` (new), `scripts/predict-daily.sh`, `config/predict-policy.example.json`.
- `pyproject.toml`: add package and `predict-agent` script.
- `.gitignore`: `data/predict.sqlite3`, `journal/predict.jsonl`, `data/reports/`, `config/predict-policy.json`.
- CI: add `bash -n scripts/predict-daily.sh`.
- `CLAUDE.md`: add a `predict_agent` section — commands, "no execution code in Phase 1", "never circumvent geoblocking", no-price-input rule and research tool isolation, forecast-before-price ordering, policy config is human-owned.
- `src/japan_agent/` untouched.

## 10. Build order

1. **Data milestone** — discover (child-market eligibility), resolution-state polling, post-hoc snapshots, refusals, `predict-agent report --shortlist`. No Claude calls. Acceptance: recorded fixtures for every API shape used (events with closed children, books, fee fields, resolution states incl. disputed and unknown); contract tests pinning share precision, min-order-size unit, fee-rate field and status/payout mapping; eligible-market counts reported after every exclusion; a permitted read-only smoke run compared against the live source.
2. Ledger: cash, tickets, settlement, atomic transactions, artifacts, cohorts, restart.
3. Policy + paper fills (incl. shadow p_mid policy) on recorded snapshots.
4. Research layer (tool isolation, domain enforcement, transcripts, exposure scanner, budget).
5. Full report and `run-daily` cron wrapper.

## 11. Phase 2 handoff (out of scope; separate spec)

Polymarket paper results **do not transfer** to another venue: prices, liquidity, commission and settlement terms must be evaluated on the destination venue itself, so Phase 2 begins with its own paper/data period there. Candidates: Betfair Exchange (verified 2026-10-05: £499 one-off live app-key activation; live keys may not be used read-only; delayed key free for development) and Smarkets (reported £150 setup fee and restrictions on data-only/benchmarking API use — **unverified**, terms page could not be fetched). Requires a human-maintained mapping `config/venue-markets.json` (code reads, never writes). Reuses forecasts and ticket hashing; reintroduces Telegram approval, non-idempotent order rules and reconciliation from the existing design. Gambling-licence terms, venue API terms and the user's tax position must be reviewed before any real money.

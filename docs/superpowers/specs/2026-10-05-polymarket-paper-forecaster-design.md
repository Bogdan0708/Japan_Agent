# Polymarket Paper Forecaster — Phase 1 Design

Date: 2026-10-05. Status: draft for review. Branch: `design/polymarket-paper-forecaster`.

## 1. Intent

**What the user said.** Investing via Trading 212 and similar is no longer worth it; refocus the project on Polymarket-style prediction markets, phased: a paper forecaster first, then real money on a UK-legal venue only if paper results justify it. Freeze the existing Japan-equities code in place. Focus on geopolitics/world events, politics/elections, and economics/macro. The Phase 1 → Phase 2 go/no-go is the user's judgment, informed by the metrics this system produces.

**Assumptions.** The user is UK-resident. The paper-first, fail-closed, deterministic-money philosophy of the existing project carries over.

**Success for Phase 1.** A forward-only record of blind Claude forecasts on real Polymarket markets, with paper trades simulated against real order books, settled at real resolutions, producing a report that lets the user judge: (a) is Claude better calibrated than the market, and (b) would trading on it have made money after fees and slippage.

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

Independent prior analysis: `docs/POLYMARKET-PIVOT-PLAN.md` (untracked draft from another session). This spec adopts its correlated-exposure, unit-ambiguity, double-counted-spread, research-cost, probability-range/abstention, baseline, frozen-cohort and data-first-milestone points; it departs from it on package location (separate package, per the user's freeze decision) and category focus (per the user's choice).

## 3. Architecture

New package `src/predict_agent/`, stdlib-only core, **no imports from `japan_agent`**. Small primitives (canonical JSON + SHA-256, UTC clock helpers, `.env` loader, hash-chained journal) are copied, not imported, so the frozen package can later be deleted without breakage. New console script `predict-agent` in `pyproject.toml`. Optional `research` extra (Claude Agent SDK) is lazily imported with a clear error, as today.

Run cadence: once daily from cron via `scripts/predict-daily.sh` (flock, atomic writes), mirroring `scripts/daily.sh`.

### Data flow

| Stage | Module | Input → output | Fails closed on |
|---|---|---|---|
| 1 discover | `ingest/gamma.py` | Gamma `/events` filtered by tags (geopolitics, politics, economics) → `markets` | unmapped tags, missing rules text, non-binary, neg-risk (excluded in Phase 1), liquidity below floor, end date outside 2–90 days |
| 2 snapshot | `ingest/clob.py` | CLOB `/book` per token → `book_snapshots` | book timestamp > 10 min old, empty side, unparseable levels |
| 3 research | `research/forecaster.py` | question + rules + end date + today (**no price**) → `forecasts` | schema invalid, uncited/unfetched citations |
| 4 policy | `policy.py` (pure) | forecast + snapshot + config + open positions → decision or refusal | see §5 |
| 5 paper-fill | `paper.py` | decision + snapshot → `paper_tickets` + journal | insufficient depth |
| 6 resolve | `ingest/resolution.py` | Gamma closed/resolved markets → `resolutions`, `settlements` | resolution not final, disputed |
| 7 report | `report.py` | DB → Markdown + JSON in `data/reports/` | — |

CLI: `predict-agent doctor | discover | snapshot | research | trade | resolve | report | run-daily`.

### Blind forecasting

Claude never receives the market price, order book, or volume. Rationale: a model that sees the price anchors to it; calibration would look good while measuring imitation. Blindness makes Claude-vs-market a genuine comparison.

## 4. Data model

SQLite at `data/predict.sqlite3`, journal at `journal/predict.jsonl`; both gitignored; directories 0700, files 0600. Schema version table from day one.

- `markets`: condition_id (PK), yes_token_id, no_token_id, event_id, question, rules_text, rules_hash, resolution_source, end_date (UTC), category (derived from event tags), fee_rate (Decimal text, nullable), fees_enabled, tick_size, min_order_size, first_seen_at, last_seen_at.
- `book_snapshots`: id, token_id, observed_at (book timestamp), fetched_at, book_hash, bids/asks (JSON of Decimal-text levels, best-first), snapshot_hash.
- `forecasts`: id, condition_id, created_at, cohort_id, abstained (bool), abstain_reason, p_low / p_mid / p_high (Decimal, p_low ≤ p_mid ≤ p_high; null when abstained), confidence (low/medium/high), base_rate, evidence (JSON with citations), rules_interpretation, model, prompt_version, inputs_hash, price_exposed (bool), research_cost_usd, kind (`entry` | `reforecast`), forecast_hash. **Every non-abstained forecast is scored on p_mid**, traded or not; abstentions are counted by reason.
- `paper_tickets`: id, cohort_id, forecast_hash, snapshot_hash, condition_id, outcome (YES/NO token bought), direction (always BUY in Phase 1), shares, avg_price, fee, cost_total, policy_version, rules_hash, status (`OPEN`|`SETTLED`|`VOIDED`), ticket_hash, created_at. Immutable except status transition, which is journaled.
- `resolutions`: condition_id, outcome (`YES`|`NO`|`HALF`), resolved_at, disputed (bool), raw_status.
- `settlements`: ticket_id, payout, net_pnl, settled_at.
- `refusals`: run_id, condition_id, stage, reason_code, detail, at.
- `cohorts`: cohort_id, policy_hash (hash of `predict-policy.json`), prompt_version, model, started_at. Any change to policy config, prompt or model opens a new cohort; reports never pool cohorts silently.

State transitions: `OPEN → SETTLED` on final resolution (HALF pays $0.50/share). `OPEN → VOIDED` if the market is cancelled or its `rules_hash` changes after the ticket; voided tickets are excluded from P&L and journaled with the reason.

## 5. Paper policy

Pure functions, no I/O. Parameters in `config/predict-policy.json` — human-edited; code reads, never writes (example file committed as `predict-policy.example.json`).

Defaults: bankroll $1,000; min edge 0.05; min confidence `medium`; Kelly fraction 0.25; per-market cap 2% of bankroll; **per-event cap 5%** (correlated markets under one event); per-category cap 15%; total open exposure cap 50%; max slippage 2% from best ask; min time to close 48h; max book age 10 min; forecast must be from the same run as the snapshot.

Algorithm for each candidate market:
1. Refuse if the market already has an `OPEN` or `SETTLED` ticket (one entry per market, hold to resolution; no exits, adds or re-trades in Phase 1).
2. Refuse if the forecast abstained, or if `fees_enabled` and `fee_rate` is null.
3. For each side s ∈ {YES, NO} with **conservative** q_s = p_low (YES) or 1 − p_high (NO): compute target size by Kelly using best ask, walk the ask book for that size to get `avg_price`; per-share cost `c_s = avg_price + fee_rate × avg_price × (1 − avg_price)`. The walked average already includes spread/slippage — do not subtract spread again. Edge `e_s = q_s − c_s`. Using the conservative bound means an edge that vanishes under a modestly less confident estimate is never traded.
4. Pick the side with larger edge; refuse if `e < min_edge`.
5. Size: `f = 0.25 × (q − c)/(1 − c)` of bankroll (conservative q), then clamp by per-market, per-event, per-category and total caps (computed on cost of open tickets); convert to shares, round down to `tick_size` precision and enforce `min_order_size` (unit interpretation pinned by contract tests, §8). Re-walk the book at the final size; refuse if slippage > 2% or depth insufficient. Never fabricate a fill beyond recorded depth.
6. Emit decision with all inputs' hashes; `paper.py` writes the immutable ticket.

Weekly re-forecasts of open markets are blind, scored, and never trade in Phase 1.

## 6. Research layer

- Claude via the Agent SDK with **only** WebSearch and WebFetch tools; no filesystem, shell, DB, or policy/paper imports. Strict JSON output validated against a schema; invalid output → refusal, no retry within the run.
- Prompt inputs: question, full rules text, resolution source, end date, today's UTC date. Prompt asks for a base rate, evidence for and against, interpretation of the rules, a probability range (p_low, p_mid, p_high), confidence, and permits explicit abstention with a reason (e.g. ambiguous rules, no relevant evidence).
- **Leakage controls:** WebSearch `blocked_domains` includes polymarket.com, kalshi.com, betfair.com, smarkets.com, manifold.markets, metaculus.com, oddschecker.com, predictit.org and similar. Post-hoc scanner flags `price_exposed` when citations or reasoning contain odds/probability-of-market phrasing ("% chance", "odds", "traders give", "market prices", prediction-market names). Reports score flagged forecasts separately. This is mitigation, not elimination: news articles may quote odds.
- Citations must be URLs actually fetched in the session (same principle as the existing citation validator).
- Forward-only: forecasts are made before outcomes are known; no historical backtests (model training data contaminates resolved markets).
- Budget: max 15 new entry forecasts/day, ranked by liquidity then nearest end date; per-forecast cost recorded; model and caps in config.

## 7. Error handling

- HTTP: only unauthenticated GETs. Bounded retries (3, exponential backoff, jitter) on timeouts/5xx/429, respecting published limits (Gamma `/markets` 300/10s, CLOB `/book` 1,500/10s). 4xx or malformed body → skip market with refusal record.
- Stale/incomplete data → fail closed with a reason code; an empty shortlist is a valid outcome.
- Resolution: settle only when Gamma reports closed and UMA resolution final; disputed markets wait. Markets > 14 days past end date and unresolved are surfaced in the report, never guessed.
- Each run records `GET /api/geoblock` for audit. Phase 1 places no orders, so a blocked result does not stop the run. **No wallet, private key, signing or order-submission code exists in Phase 1.**
- Same-day reruns are idempotent: a market already forecast/traded today is skipped.

## 8. Reporting and testing

**Report** (`predict-agent report`, local Markdown + JSON):
- Sample size (resolved markets, independent events) first.
- Brier and log score: Claude p_mid vs baselines at the same timestamp — market mid, and Claude's own stated base rate — overall and by category, horizon bucket, `price_exposed` split.
- 10-bucket calibration table.
- Paper P&L net of fees and slippage; **event-clustered** bootstrap 95% CI; hit rate; mean edge at entry vs realized; open capital locked; total research cost and P&L net of research cost.
- Refusal and abstention counts by reason code.
- Every section split by cohort.
- Guidance (not a gate): expect a 4–8 week collection window, extended while independent resolved events are too few to read.
- No automated go/no-go; the user decides on Phase 2.

**Tests** (unittest, zero-network, `-W error::ResourceWarning`):
- Policy tables: conservative-bound edge (range straddling the ask refuses), abstained forecast refuses, both sides, fees on/off, missing fee rate refuses, each cap including per-event, tick rounding, min order size under both unit interpretations, 48h cutoff, no-double-spread.
- Book walk: multi-level fills, insufficient depth, worst-first input ordering.
- Settlement: YES, NO, HALF, VOIDED on rules-hash change, disputed pending.
- Ingest: fixtures shaped from verified live responses (JSON-in-string fields, null category, ms timestamps), plus stale and malformed variants.
- Research: schema rejection, unfetched citations, leakage scanner.
- Ledger: hash-chain integrity, ticket immutability.
- Isolation: test asserts `predict_agent.research` imports nothing from `predict_agent.policy`/`paper`, and `predict_agent` imports nothing from `japan_agent`.

## 9. Repository changes

- `src/predict_agent/` (new), `tests/predict/` (new), `scripts/predict-daily.sh`, `config/predict-policy.example.json`.
- `pyproject.toml`: add package and `predict-agent` script.
- `.gitignore`: `data/predict.sqlite3`, `journal/predict.jsonl`, `data/reports/`, `config/predict-policy.json`.
- CI: add `bash -n scripts/predict-daily.sh`.
- `CLAUDE.md`: add a `predict_agent` section — commands, "no execution code in Phase 1", "never circumvent geoblocking", blind-forecast rule, policy config is human-owned.
- `src/japan_agent/` untouched.

## 10. Build order

1. **Data milestone** — discover, snapshot, refusals, `predict-agent report --shortlist` (eligible markets, books, fees). No Claude calls. Proves enough suitable markets exist before spending on research.
2. Policy + paper fills + settlement on fixtures and recorded snapshots.
3. Research layer (blind forecaster, leakage scanner, cohorts).
4. Full report and `run-daily` cron wrapper.

## 11. Phase 2 handoff (out of scope; separate spec)

Venue: Betfair Exchange or Smarkets. Requires a human-maintained mapping `config/venue-markets.json` (Polymarket condition_id → venue market id; code reads, never writes). Reuses forecasts, policy and ticket hashing; reintroduces Telegram approval, non-idempotent order rules and reconciliation from the existing design. Gambling-licence terms, venue API terms and the user's tax position must be reviewed before any real money.

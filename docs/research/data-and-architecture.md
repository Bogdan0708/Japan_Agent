# Data sources & agent architecture research — Japanese market

Researched 2026-08-28. Licensing and pricing change; re-verify before extending
usage.

> Post-remediation verification found that the Sunday wrapper can report
> success after stale-input research refusal, observation windows can point
> implausibly into the future, and evidence is database-bound rather than bound
> to the exact model snapshot. These remain open in
> [post-remediation-audit-2026-08-28.md](post-remediation-audit-2026-08-28.md).

## Japanese market data

| Source | Data | Cost / access | Role here |
|---|---|---|---|
| J-Quants API (JPX official) | Prices, calendar, financials, fundamentals | Free tier (**12 weeks delayed**); paid ~¥1,650–¥5,500/mo; TDnet add-on ¥11,000/mo (individual use) | Historical context only; never same-day catalysts; no republication (licence) |
| EDINET API v2 (FSA) | Securities reports, filings metadata | Free with API key | Filing headlines; timestamps preserved |
| TDnet | Timely disclosures (earnings, buybacks, M&A) | Public pages viewable 31 days; the official API is **paid** | Cited, timestamped digests from the public surface; no aggressive scraping |
| yfinance / Yahoo | Daily OHLCV for .T/LSE/NYSE | Free, unofficial, personal/research use | Whitelist daily closes + FX; not sole execution authority; no republication. **Pence trap: London prices come back as "GBp"** |
| Nikkei | News | Subscription; API needs commercial agreement | Not used directly; Perplexity-cited digests instead |
| JPX pages | Trading calendar, halts, notices | Free | Holiday/session authority |

Data-design rules adopted: canonical timestamped snapshots; observation time ≠
fetch time; symbol-history awareness (Japanese codes change); Japanese text is
first-class data; store raw + retrieval + publication timestamps.

## Framework survey (what informed the build)

FinRL (RL research, not turnkey), TradingAgents (multi-agent LLM debate ≠
independent evidence), FinGPT (sentiment/extraction), LumiBot (execution
scheduling), Freqtrade (crypto-centric), Backtrader/vectorbt (backtesting).
None fit directly; the adopted design separates an **LLM research layer** from
a **deterministic portfolio/execution layer**:

1. LLM extracts claims/events from filings and news (structured output only).
2. Deterministic code validates dates, prices, identities, and citations.
3. A risk engine applies hard limits.
4. Execution converts an approved fixed ticket into one order; the broker
   adapter never accepts prose.

Lessons from LLM trading experiments: LLMs are strongest at document
extraction/classification, weakest at unconstrained price prediction; backtest
look-ahead bias is the dominant failure (revised filings, later translations,
survivorship); agent "debate" amplifies shared errors; every model output must
become a structured, auditable object with thesis, evidence IDs, timestamps,
confidence, and a falsifiable invalidation condition.

## Guardrails adopted

Fail-closed everywhere; whitelist-only cash instruments; 40% NAV cap; £15
satellite/frontier cap; 3 trades/week; £5 cash floor; price collars via
fresh-quote preflight; no leverage/shorts; duplicate and stale-order guards;
research-only → paper → gated live progression; three-level kill switch
(strategy file, local interlock, broker-side key revocation); immutable
hash-bound tickets; named-human approval with 24h expiry; hash-chained
append-only decision ledger supporting replay.

## Scheduling around TSE hours

TSE cash sessions: 09:00–11:30 and 12:30–15:30 JST ≈ **00:00–06:30 UTC** — the
Japanese day is over by UK breakfast, and the tradable instruments (LSE/NYSE
lines) trade during UK hours. Adopted cadence (Europe/London cron):

- **Daily 07:15 Mon–Fri** — post-Tokyo-close: collect prices+FX, ingest EDINET
  for today's and yesterday's Tokyo dates, ingest digests, bounded research
  pass (`scripts/daily.sh`).
- **Sunday 10:00** — J-Quants historical refresh, deep research, ledger
  verification, journal sync, weekly post (`scripts/weekly.sh`).
- Execution remains a separate human-initiated step until the demo-payload
  mapper exists.

Timestamps stored in UTC, displayed Asia/Tokyo + Europe/London; official JPX
calendar over any generic market-hours library.

Key sources: https://jpx-jquants.com/ ;
https://www.jpx.co.jp/english/markets/paid-info-listing/tdnet/02.html ;
https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WEEK0060.html ;
https://github.com/ranaroussi/yfinance/blob/main/README.md

# Broker research — UK resident, £100, Japanese exposure, API trading

Researched 2026-08-28 (Perplexity, high context). Point-in-time; re-verify fees
and API capabilities before relying on them.

## Decision

**Trading 212** chosen: zero commission, fractional shares, UCITS ETFs + NYSE
ADRs, free practice environment, REST API that can place orders. Its API terms
are the binding constraint (see `../COMPLIANCE.md`): algorithmic trading is
prohibited and customised-interface deployment needs prior written consent, so
the workflow is propose → named-human approval → execute, and live trading is
blocked until T212's written reply.

**IBKR** is the fallback if T212 declines consent: automated trading permitted,
direct TSE access, but minimum commissions (~£1–3/trade) consume 1–3% of every
£100-scale trade and fractional does not cover Japanese stocks.

## Comparison

| Broker | Trading API | Small orders / fractional | Japan exposure | Verdict at £100 |
|---|---|---|---|---|
| Trading 212 | Beta public API; Invest + Stocks ISA; demo + live hosts | Fractional decimal quantities; orders by quantity, not value | Japan/robotics UCITS ETFs, NYSE ADRs; no direct TSE | **Best fit** |
| Interactive Brokers | Web API, TWS API, FIX | Fractional is security/venue-specific; NOT Japanese stocks | Direct TSE access (permissions, JPY, fees) | Fallback; costs dominate |
| Alpaca | Full brokerage API | Fractional US shares | US-focused; UK retail availability unconfirmed | Not a fit |
| Saxo | OpenAPI for eligible clients | Whole shares; minimum commissions dominate | Broad international | Too expensive at £100 |
| IG | API | CFD-centric for Japan | Derivatives, not ownership | Not a fit |
| Freetrade / Lightyear | No public order API | Fractional/low minimums | Varies | Manual only |

## Trading 212 API details (as of 2026-08-28)

- Places real orders: market, limit, stop, stop-limit; monitoring and cancels.
- Beta; Invest and Stocks ISA account types; separate demo environment.
- Orders execute in the account's primary currency only; multi-currency
  accounts unsupported via API.
- Rate limits: market orders ~50/min; limit/stop placement ~1 per 2s; position
  reads ~1/s; active-order reads ~1 per 5s.
- Orders are submitted by share quantity (sells use negative quantity); no
  value-based endpoint — compute quantity from a live quote.
- The order POST is **not idempotent**: never auto-retry (implemented as the
  `RECONCILIATION_REQUIRED` state).
- "Zero commission" still leaves FX conversion (~0.15%), spread, and ETF
  ongoing charges.

Sources: https://docs.trading212.com/api ; https://docs.trading212.com/api/orders/placemarketorder ;
https://www.trading212.com/legal-documentation/API-Terms_EN.pdf ;
https://www.interactivebrokers.co.uk/en/pricing/commissions-home.php

## TSE lot sizes — why £100 cannot buy Japanese stocks directly

Most TSE-listed companies trade in **100-share units**: minimum outlay ≈ 100 ×
share price. A ¥1,200 stock needs ~¥120,000 (~£600+) per lot before fees. Odd-lot
services exist at some Japanese brokers but with worse execution windows and
liquidity, and are not accessible through the chosen venue. Consequence: all
Japan exposure at this budget goes through UCITS ETFs and fractional NYSE ADRs.

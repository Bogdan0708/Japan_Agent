# Compliance and data-licence gate

This is a technical risk record, not legal, tax, or investment advice. Re-check the documents before
any live deployment because API and licence terms can change.

## Trading 212

The official [Public API reference](https://docs.trading212.com/api) currently says the beta API is
available for Invest and Stocks ISA accounts, uses separate demo/live hosts, supports quantity orders,
and executes values in the primary account currency. Its
[market-order reference](https://docs.trading212.com/api/orders/placemarketorder) warns that the POST
is non-idempotent and that market prices can slip.

The [API Terms](https://www.trading212.com/legal-documentation/API-Terms_EN.pdf), last updated in the
document on 17 October 2025, are the live blocker:

- clause 4.2(a) prohibits use for algorithmic trading;
- the definition includes algorithms determining whether, timing, price, quantity, or management of
  an order with limited to no human intervention;
- clauses 6.1–6.2 frame API access as personal testing use;
- clause 6.7 requires testing and says deployment of a customised interface is subject to prior written
  consent.

A Telegram button is valuable evidence of human review but is not a legal conclusion about those
clauses. Before live use, obtain written Trading 212 confirmation describing the exact workflow:
research model, deterministic sizing, fixed ticket, named-human approval, 24-hour expiry, fresh-price
guard, low frequency, and personal account only. Record the response reference in both the environment
and signed live-gate file.

The code also requires:

```text
T212_ENV=live
ALLOW_LIVE_TRADING=I_ACKNOWLEDGE_REAL_MONEY
T212_WRITTEN_CONSENT_REF=<written response reference>
LIVE_GATE_FILE=<absolute chmod-600 JSON path>
```

The JSON must record a human approver, review timestamp, paper start, at least 14 manual-fix-free days,
no risk breaches, verified journal output, and the same written-consent reference. A sample is in
`config/live-gate.example.json`. These controls do not substitute for permission; they prevent an
accidental configuration flip.

## J-Quants

The official [J-Quants product page](https://jpx-jquants.com/) says Free data is 12 weeks delayed and
does not provide today's price. The current v2 example uses an `x-api-key` header. J-Quants also warns
that obtained data cannot be redistributed in viewable form and that continuously distributing
investment-analysis results to third parties is not personal use. Therefore:

- use Free only as delayed historical context;
- never publish raw J-Quants rows or reconstructable tables;
- obtain written licence clarification before a recurring public journal relies materially on it;
- use current filings/news and tradable-instrument quotes for catalyst work.

## EDINET and TDnet

The FSA's [EDINET API page](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WEEK0060.html)
lists API v2 documentation and key-based access. The client stores filing metadata locally and does not
automatically publish filing bodies.

JPX describes the [official TDnet API](https://www.jpx.co.jp/english/markets/paid-info-listing/tdnet/02.html)
as a paid subscription. The separate public announcement service makes disclosures available for 31
days. This repository accepts a cited, timestamped digest from that public surface; it does not scrape
aggressively or label the paid API as free.

## Public persona and journal

Every public page must state that the analyst is AI, Bogdan is the account holder, all trades require
human approval, and the material is not investment advice. Do not expose account identifiers, API
data prohibited from redistribution, private Telegram identifiers, or tax information.


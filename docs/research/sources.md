# Primary-source register (accessed 2026-08-28)

This register maps the external sources consulted during design and audit to
the claims they support. It is point-in-time research: broker terms, product
availability, tax, APIs, fees, software behavior, and government programs can
change. Re-verify before live deployment or publication. Repository code and
tests, rather than this register, are the authority for current implementation
behavior.

## Broker API, account access, and execution

- Trading 212 Public API reference: https://docs.trading212.com/
  - Beta demo/live hosts, Invest and Stocks ISA availability, authentication,
    primary-account-currency limitation, rate-limit headers, metadata,
    positions, order and history endpoints, negative sell quantities.
- Trading 212 API Terms, published 17 October 2025:
  https://www.trading212.com/legal-documentation/API-Terms_EN.pdf
  - Clause 4.2(a) algorithmic-trading prohibition; definition covering
    computer-determined order parameters with limited-to-no human intervention;
    personal/testing language; customised-interface consent; market-data
    redistribution restrictions; beta/API-risk allocation.
- Trading 212 API-key help:
  https://helpcentre.trading212.com/hc/en-us/articles/14584770928157-Trading-212-API-key
  - Permission-scoped keys, secret handling, revocation, account availability,
    and optional IP restrictions described by the official documentation.
- Trading 212 market-order help:
  https://helpcentre.trading212.com/hc/en-us/articles/360007081257-Market-Orders
  - Market-price movement/slippage risk.
- Interactive Brokers commissions:
  https://www.interactivebrokers.co.uk/en/pricing/commissions-home.php
  - Fallback cost comparison; re-check exact UK account, venue, FX, and TSE
    permissions if the fallback is activated.

## Japanese market access and data

- JPX domestic-stock trading units:
  https://www.jpx.co.jp/english/equities/trading/domestic/03.html
  - TSE domestic stocks trade in 100-share units.
- JPX trading-unit standardisation:
  https://www.jpx.co.jp/english/equities/improvements/unit/index.html
  - Historical standardisation to 100-share units.
- JPX/EDINET API v2 guidance:
  https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WEEK0060.html
  - Key-based EDINET document API documentation.
- JPX TDnet API:
  https://www.jpx.co.jp/english/markets/paid-info-listing/tdnet/02.html
  - Official TDnet API is a paid service; do not label it a free API.
- J-Quants product site: https://jpx-jquants.com/
  - Free-plan delay and plan/licence constraints; re-check the current plan and
    redistribution terms before public journal use.
- yfinance project README:
  https://github.com/ranaroussi/yfinance/blob/main/README.md
  - Unofficial, research/educational orientation, Yahoo personal-use warning,
    and the need to consult Yahoo's data terms.
- Current yfinance London-pence reproduction:
  https://github.com/ranaroussi/yfinance/issues/2866
  - Yahoo/yfinance reports London pence as case-sensitive `GBp`; prices can be
    approximately 100x the GBP amount if the unit is lost.

## Messaging and agent security

- Telegram Bot FAQ: https://core.telegram.org/bots/faq
  - Long polling and webhooks are mutually exclusive; `offset` acknowledges
    updates and prevents repeated delivery.
- Anthropic prompt-injection guidance:
  https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/mitigate-jailbreaks
  - Treat third-party documents/tool results as untrusted, identify provenance,
    JSON-encode where possible, state the policy in the system prompt, minimise
    privileges, screen outputs, and red-team indirect injection.
- OWASP LLM01:2025 Prompt Injection:
  https://genai.owasp.org/llmrisk/llm01-prompt-injection/
  - Indirect prompt injection through retrieved documents and data is a primary
    LLM-application risk.
- NIST AI RMF Generative AI Profile, NIST AI 600-1:
  https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence
  - Governance, mapping, measurement, management, testing, evaluation,
    verification, and validation practices for generative-AI systems.

## Identity, public journal, and performance reporting

- GitHub Terms of Service:
  https://docs.github.com/en/site-policy/github-terms/github-terms-of-service
  - One free machine account may accompany a free personal account, and it must
    be used only for operating the machine identity. Re-check before creating
    the public persona account.
- CFA Institute GIPS Standards for Asset Owners:
  https://www.cfainstitute.org/-/media/documents/code/gips/2020-gips-standards-asset-owners.pdf
  - Time-weighted performance, fees/currency disclosure, and comparable
    total-return benchmark practices. This project does not claim GIPS
    compliance; Modified Dietz is used as a documented approximation when
    daily valuations are unavailable.
- FCA PERG 8, financial promotions and related activities:
  https://handbook.fca.org.uk/handbook/perg8
  - Objective editorial may differ from an invitation or inducement; a
    disclaimer is not by itself a universal legal shield. Keep the journal
    retrospective, factual, transparent, non-personalised, and free of
    calls-to-action or affiliate inducements; obtain advice before monetising.
- UK capital-gains guidance:
  https://www.gov.uk/guidance/capital-gains-tax-rates-and-allowances
  - Current allowance/rate source; re-check for the applicable tax year. The
    account holder remains responsible for records and tax treatment.

## Japan frontier-technology research leads

- Japan Integrated Innovation Strategy 2026, Cabinet Office:
  https://www8.cao.go.jp/cstp/tougosenryaku/2026.html
  - Seventeen strategic technology fields, including AI/advanced robotics,
    semiconductors/communications, quantum, fusion, space, advanced materials,
    mobility/logistics, biotechnology, and resource/energy-security/GX.
- METI semiconductor and digital-industry strategy:
  https://www.meti.go.jp/policy/mono_info_service/joho/conference/semicon_digital.html
  - 2026 ecosystem framing spanning data, AI models, compute, communications,
    power, people, security, AI agents, and physical AI.
- Cabinet Office quantum strategy portal:
  https://www8.cao.go.jp/cstp/english/quantum/index.html
  - Quantum industrialisation, ecosystem, and international-collaboration
    policy research leads.
- METI 2026 strategic-investment overview:
  https://www.meti.go.jp/english/speeches/2026newyeargreetings.html
  - Public-policy leads involving AI/semiconductors, robotics, quantum,
    nuclear, perovskite solar, offshore wind, and geothermal. Government support
    is evidence for further research, never a standalone investment thesis.

## Interpretation rules

- Prefer primary sources for broker/API/technical claims.
- Treat product availability, prices, fees, tax, terms, model names, plan
  limits, and public policy as time-sensitive.
- Separate sourced facts, inferences, hypotheses, and mandate choices.
- Never convert a policy announcement, thematic label, or model-generated
  narrative directly into a trade.
- Verify the exact T212 instrument, currency unit, fractional rules, KID,
  liquidity, overlap, and account environment before whitelist admission.

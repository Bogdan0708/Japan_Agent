# Legal & compliance research — AI agent trading a UK personal account

Researched 2026-08-28. General information, not legal or tax advice. The
maintained operational conclusions are in `../COMPLIANCE.md`; this file keeps
the fuller background.

## Can the AI have its own brokerage identity? No.

An AI agent is not a legal person: it cannot pass KYC, own assets, enter a
brokerage contract, or be a beneficial owner. The account must belong to a
human (Bogdan) or a registered legal entity. The agent's "identity" is
operational only — persona, email, GitHub machine account, API keys, journal —
and every public page must say the analyst is an AI, the human is the account
holder, and trades require human approval. The broker treats any instruction
sent with the account's credentials as the account holder's instruction.

## FCA authorisation — not needed for own-money automation

A UK resident trading their own money at their own risk does not need FCA
authorisation merely because the trading is automated. Authorisation triggers
would be: trading for others, discretionary management of another's
investments, personalised advice, operating a fund/copy-trading service,
selling the system commercially, or performance fees from others. Market-abuse
rules still apply (no spoofing/layering/insider dealing); the account holder
remains responsible for monitoring, records, and the ability to stop it.

## Broker terms are the real constraint

- **Trading 212 API Terms** (dated 17 Oct 2025 in the document): clause 4.2(a)
  prohibits algorithmic trading, defined as an algorithm determining whether/
  when/price/quantity/management of an order with limited-to-no human
  intervention; clauses 6.1–6.2 frame access as personal testing; clause 6.7
  requires prior written consent for deploying a customised interface. Because
  the model chooses target_weight and code derives quantity, human approval
  alone does not conclusively escape the definition — hence the written-consent
  request (`../T212-CONSENT-REQUEST.md`) and the code-enforced live block.
- **IBKR**: APIs support automated submission; verify the current user
  agreement, credential-sharing rules, and market-data terms if the fallback is
  ever used.

## UK tax (2026/27)

- CGT annual exempt amount **£3,000**; rates 18%/24% by income band. Irrelevant
  in practice at £100 scale, but each disposal is still a taxable event;
  same-day and 30-day bed-and-breakfast matching rules apply to frequent
  trading.
- ISA subscriptions ~£20,000/yr; gains inside an ISA are CGT-exempt. Current
  T212 docs describe API access for both Invest and Stocks ISA (an earlier
  claim that ISA API access is read-only is outdated — verify against the real
  account before locking the account type).

## Practical identity structure (safer setup)

1. Account and beneficial ownership stay with the human.
2. Only use a broker/API that permits the intended automation in writing.
3. Restricted, immediately revocable API keys; never grant withdrawals,
   transfers, or account-settings access.
4. Dedicated agent email is fine; never let the broker believe the AI is the
   customer, and never conceal the beneficial owner.
5. Hard limits, full logging, kill switch, human review — all implemented in
   this repo.

Key sources: https://www.trading212.com/legal-documentation/API-Terms_EN.pdf ;
https://www.gov.uk/guidance/capital-gains-tax-rates-and-allowances ;
https://docs.trading212.com/

# Trading 212 written-consent request (draft)

Send from Bogdan's own account email via the in-app help centre or
support@trading212.com. Live trading stays blocked until a written response
reference exists (see `docs/COMPLIANCE.md`). Replace bracketed values before
sending.

---

**Subject:** API Terms clause 6.7 — prior written consent request for a
human-approved personal trading workflow

Hello,

I hold a personal Trading 212 Invest account ([account email / ID]) and I am
writing to request written consent under clause 6.7 of the API Terms before
using the Public API beyond personal testing, and to confirm that my intended
workflow is not "Algorithmic Trading" as prohibited by clause 4.2(a).

The workflow, in full:

1. Software I run privately, on my own machine, for my own money only,
   researches a fixed whitelist of LSE/NYSE-listed ETFs and shares and drafts
   at most a handful of order suggestions per week (hard-capped at three).
2. Every suggestion is presented to me personally as a fixed ticket — exact
   ticker, side, and quantity, cryptographically hashed so it cannot change
   after I see it. Nothing is ever submitted without my explicit approval of
   that exact ticket; unapproved tickets expire after 24 hours.
3. Only after I approve does the software place that single order through the
   Public API, re-checking a fresh quote first. It cannot decide, retime,
   reprice, resize, or resubmit an order on its own, and it has no access to
   withdrawals or account settings.
4. The account is my personal Invest account; no one else's money, no leverage,
   no derivatives, no commercial service, and expected activity of a few small
   orders per month on a portfolio of roughly £100.

My reading is that because every order's parameters are fixed before my human
approval and no order exists without it, this is human trading with software
assistance rather than algorithmic trading with limited human intervention. I
would rather confirm that reading with you in writing than rely on it.

Could you please confirm in writing:

1. that the workflow above is permitted under the API Terms, or what would need
   to change for it to be; and
2. that this message satisfies the prior-written-consent requirement of clause
   6.7 for deploying this personal, non-commercial interface, or how I should
   obtain that consent.

I am happy to provide further detail, demonstrate the approval flow, or accept
additional conditions (for example, stricter rate or order limits).

Thank you,
Bogdan Godja
[account email]

---

When a reply arrives: store the ticket/reference number as
`T212_WRITTEN_CONSENT_REF` in `.env`, save the reply itself (PDF or full email
export) under `docs/consent/`, and record the same reference in the live-gate
JSON. If the reply is a refusal or a non-answer, live trading on Trading 212
stays off and the fallback decision in the plan applies (IBKR, or a permanent
public paper-trading persona).

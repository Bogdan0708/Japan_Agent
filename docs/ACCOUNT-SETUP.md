# Account setup checklist (human tasks — Phase A)

Everything the software cannot do because it requires a real person's identity,
clicks, or consent. Order matters only where noted. Estimated total effort: one
evening plus waiting on Trading 212's reply.

## 1. Trading 212 (do first — the consent reply has lead time)

- [ ] Ensure a personal **Invest** account exists (ISA can come later).
- [ ] Send the consent request in `docs/T212-CONSENT-REQUEST.md` from your
      account email. Live trading stays code-blocked until the written reply
      reference is in `.env` — paper work continues regardless.
- [ ] In the app: Settings → API (Beta) → generate a **practice/demo** key with
      metadata, account, portfolio, history, and order permissions. Add an IP
      allowlist if offered.
- [ ] Put the key in `.env` as `T212_API_KEY` / `T212_API_SECRET`, keep
      `T212_ENV=demo`.

## 2. Telegram approval channel

- [ ] Message @BotFather → `/newbot` → pick a bot name/username for the agent.
- [ ] Put the token in `.env` as `TELEGRAM_BOT_TOKEN`.
- [ ] Message the new bot once from your own Telegram, then find your numeric
      user id (e.g. via @userinfobot) → `TELEGRAM_APPROVER_USER_ID` and
      `TELEGRAM_APPROVAL_CHAT_ID` (your direct chat with the bot).
- [ ] Set any random string as `TELEGRAM_WEBHOOK_SECRET` (the long-polling mode
      still requires it as an internal invariant).

## 3. Agent identity accounts

Do these AFTER `japan-agent identity` has run and the agent has a name — the
accounts should carry its chosen name.

- [ ] Create the agent's email address (its name @ your preferred provider).
- [ ] Create a GitHub **machine account** for the agent using that email
      (GitHub ToS permits one machine account for automation). Enable 2FA;
      store recovery codes in your password manager, not the repo.
- [ ] Turn on GitHub Pages for the journal repo once `pages/` is pushed there.

## 4. Data sources

- [ ] J-Quants: register at jpx-jquants.com (free plan), put the key in `.env`
      as `JQUANTS_API_KEY`. Free data is 12 weeks delayed — context only.
- [ ] EDINET: request an API key via the FSA's EDINET API page →
      `EDINET_API_KEY`.
- [ ] Anthropic API key for the research runs → `ANTHROPIC_API_KEY`.

## 5. After the accounts exist (joint work with the agent)

- [ ] `japan-agent doctor` — expect only account-dependent FAILs to disappear
      as each item above lands.
- [ ] `japan-agent discover-instruments --term japan --term robotics ...` and
      verify exact tickers, currencies, and fractional availability; populate
      `config/whitelist.json` with `verified_at` and
      `config/data-symbols.json` with the matching yfinance symbols.
- [ ] Dry-run the loop end to end in demo (see README bring-up order), then
      start the 2–4 week paper track record. The live gate criteria live in
      `docs/COMPLIANCE.md` and `config/live-gate.example.json`.

## Never

- Put real keys in the repo, journal, or public pages.
- Present the agent as a human anywhere its accounts are visible.
- Let any credential grant withdrawals or account-settings access.

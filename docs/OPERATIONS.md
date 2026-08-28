# Paper operations

## Daily cadence

The intended 07:15 Europe/London job is a collector/research job, not an execution job. Run EDINET for
the current Japanese date, import a cited TDnet digest and news digest, refresh normalized tradable
quotes, and assemble research. The gate requires PRICE within 72 hours, JQUANTS within seven days,
EDINET/TDNET within 36 hours, and NEWS within 24 hours. Empty successful disclosure results still need
an ingest heartbeat; a failed network call does not.

J-Quants Free is delayed and only needs a weekly historical-context refresh. It must not drive a claim
that a catalyst is current.

## Suggested cron shape

Do not install this until each command has passed manually in demo. The wrapper scripts live in
`scripts/daily.sh` and `scripts/weekly.sh`: they stop on the first non-zero exit, log to
`data/logs/` (git-ignored), and never print secrets. The daily script collects whitelist prices with
matched FX (`collect-prices`), ingests EDINET plus any dropped `data/digests/{tdnet,news}-YYYYMMDD.json`
digest, and runs research; it never proposes or executes.

```cron
CRON_TZ=Europe/London
15 7 * * 1-5 /absolute/path/to/japan-agent/scripts/daily.sh
0 10 * * 0 /absolute/path/to/japan-agent/scripts/weekly.sh
*/2 8-21 * * 1-5 /absolute/path/to/approved-demo-execution-wrapper
```

The execution wrapper is intentionally not included yet. It depends on a verified mapper from the
observed Trading 212 account-summary and positions payloads into a two-minute-old GBP portfolio
snapshot. Guessing that mapping would make the risk gate falsely green.

## Telegram approvals

The default transport is `telegram-poll`: it long-polls getUpdates outbound over HTTPS, so no inbound
port, TLS certificate, or reverse proxy is needed on a home machine. The bot token authenticates the
connection; the poller supplies the configured secret internally and the same user/chat allowlist and
ticket-hash checks apply. Run it as an unprivileged long-lived service (e.g. a systemd user unit or a
tmux session started at boot).

The webhook alternative remains for hosted deployments: run `telegram-serve` bound to localhost,
terminate TLS in a maintained reverse proxy exposing only `/telegram/webhook`, and configure Telegram
with a high-entropy secret token matching `TELEGRAM_WEBHOOK_SECRET`. Either way the callback accepts
only the configured Telegram user and chat. Rotate the bot token and secret if logs or backups may have
exposed them.

Approval changes state; it does not execute inside the HTTP request. A separate demo worker should fetch
a fresh quote and broker portfolio, then call `execute`. This keeps slow broker calls away from the
webhook and permits a final deterministic preflight.

## Incident states

- `FAILED`: broker definitively rejected the request. Fix the cause and create a new proposal.
- `RECONCILIATION_REQUIRED`: broker acceptance is unknown. Do not retry. Inspect T212 order history,
  reconcile holdings/cash, then record the result before any replacement ticket.
- kill switch engaged: local submissions are blocked. If compromise is suspected, revoke the key in
  Trading 212 as well.
- stale/missing source: expected normal fail-closed result is no proposal and a non-zero job exit.

## Paper-to-live review

Review at least 14 consecutive manual-fix-free days (the project goal allows 2–4 weeks), all proposal
and submission events, broker fills, weekly trade counts, stale-source refusals, public post redaction,
and benchmark calculations. Sign the live-gate JSON only after separate written Trading 212 confirmation.


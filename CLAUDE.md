# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- Canonical test run (zero-network, resource leaks are errors — not plain pytest):
  `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -v`
- Single module/test: same command with `-m unittest tests.test_risk -v` (or `tests.test_risk.Class.test_name`).
- Lint/type-check (`.venv` has only the `dev` extra): `ruff check src tests` and `mypy src` (strict). Bare `mypy` fails (no `py.typed`); `mypy src` reports `import-not-found` for `claude_agent_sdk`/`yfinance` unless the `research`/`data` extras are installed. Ruff has existing errors — don't mass-fix unrelated files. No formatter is configured: never run `ruff format`.
- CI (Python 3.11 and 3.14) runs only the unittest command, `bash -n scripts/daily.sh scripts/weekly.sh`, and `git diff --check` — run those before pushing. Code must stay 3.11-compatible.
- CLI without install: `PYTHONPATH=src python3 -m japan_agent.cli <command>`; `japan-agent doctor` fails closed on incomplete setup.

## Hard rules — do not "fix" these

- Core code is stdlib-only. New hard dependencies are forbidden; optional integrations go in the `research`/`data` extras with a lazy import and a clear error.
- The LLM research layer must never import the broker adapter or gain money-moving tools. Everything that touches money is deterministic pure Python.
- Trading 212 order POSTs are non-idempotent: never add automatic retries around submission. A definite 4xx is `FAILED`; timeout, 408, 429, 5xx, malformed body or missing order ID go to `RECONCILIATION_REQUIRED`, which blocks resubmission.
- Fail-closed refusals (stale sources, empty whitelist, missing heartbeat, live gate) are correct behavior, not bugs. Never weaken a gate to make a command succeed.
- Live trading is contractually blocked until a Trading 212 written-consent reference exists — read `docs/COMPLIANCE.md` before touching `execute/`, live gates, or anything the public journal renders.
- Money math uses `Decimal` only, never float. Timestamps are UTC; market observation time must never be conflated with fetch time.
- `config/whitelist.json` and `config/data-symbols.json` are human-verified: code reads them, never writes them. `data-symbols.json` is absent until the human creates it from the `.example` file — don't create it.
- The execution cron wrapper and a T212 portfolio mapper are intentionally absent (`daily.sh` only collects and researches). Don't add them by guessing.
- Yahoo reports London prices as `GBp`; always normalize via `normalize_currency_code` (GBX), never treat them as GBP.
- Never log or commit secrets, Telegram payloads/usernames, or raw J-Quants data (its licence forbids republication; free tier is 12 weeks delayed).

## Context

Paper-first, human-approved investing agent. Flow: ingest (fail-closed snapshots) → structured Claude research → deterministic risk engine → immutable hash-bound ticket → Telegram approval by the named human → T212 demo/live adapter → reconciliation → append-only journal. Architecture details: `docs/ARCHITECTURE.md`; operations/cron: `docs/OPERATIONS.md`; human account tasks: `docs/ACCOUNT-SETUP.md`.

## predict_agent (Polymarket paper forecaster, Phase 1)

- Spec: `docs/superpowers/specs/2026-10-05-polymarket-paper-forecaster-design.md`; plans in `docs/superpowers/plans/`.
- Separate package: `predict_agent` must never import `japan_agent` (enforced by `tests/predict/test_cli_isolation.py`). `japan_agent` is frozen.
- Phase 1 has **no execution code**: no wallet, keys, signing, or non-GET HTTP. Never attempt to circumvent Polymarket's UK geoblock; the geoblock check is audit-only.
- CLI: `PYTHONPATH=src python3 -m predict_agent.cli doctor|discover|snapshot|resolve|report|run-data`. Needs `config/predict-policy.json` (human-owned copy of the `.example`; code never writes it).
- Live API quirks pinned in `tests/predict/fixtures.py`: JSON-string list fields, worst-first books on both sides, resolution rows without `payouts`, closed Gamma markets only returned with `closed=true`, and the CLOB book `timestamp` is the last-change time (a quiet book is still current — never treat an old one as stale).

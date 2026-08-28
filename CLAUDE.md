# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- Canonical test run (zero-network, resource leaks are errors — not plain pytest):
  `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -v`
- Lint/type-check (require the `dev` extra installed in `.venv`): `ruff check src tests` and `mypy` (strict mode is configured).
- CLI without install: `PYTHONPATH=src python3 -m japan_agent.cli <command>`; `japan-agent doctor` fails closed on incomplete setup.

## Hard rules — do not "fix" these

- Core code is stdlib-only. New hard dependencies are forbidden; optional integrations go in the `research`/`data` extras with a lazy import and a clear error.
- The LLM research layer must never import the broker adapter or gain money-moving tools. Everything that touches money is deterministic pure Python.
- Trading 212 order POSTs are non-idempotent: never add automatic retries around submission. An uncertain outcome goes to `RECONCILIATION_REQUIRED` and blocks resubmission.
- Fail-closed refusals (stale sources, empty whitelist, missing heartbeat, live gate) are correct behavior, not bugs. Never weaken a gate to make a command succeed.
- Live trading is contractually blocked until a Trading 212 written-consent reference exists — read `docs/COMPLIANCE.md` before touching `execute/`, live gates, or anything the public journal renders.
- Money math uses `Decimal` only, never float. Timestamps are UTC; market observation time must never be conflated with fetch time.
- `config/whitelist.json` and `config/data-symbols.json` are human-verified: code reads them, never writes them.
- Never log or commit secrets, Telegram payloads/usernames, or raw J-Quants data (its licence forbids republication; free tier is 12 weeks delayed).

## Context

Paper-first, human-approved investing agent. Flow: ingest (fail-closed snapshots) → structured Claude research → deterministic risk engine → immutable hash-bound ticket → Telegram approval by the named human → T212 demo/live adapter → reconciliation → append-only journal. Architecture details: `docs/ARCHITECTURE.md`; operations/cron: `docs/OPERATIONS.md`; human account tasks: `docs/ACCOUNT-SETUP.md`.

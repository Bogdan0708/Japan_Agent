---
name: verify
description: Run the full japan-agent quality gate — safety test suite, ruff, mypy strict, and the doctor setup check — and interpret the results. Use after any code change and before committing.
---

Run these from the repo root, in order. Report each result; do not stop at the first failure — the full picture matters.

1. Safety suite (must pass with zero network and no resource leaks):
   `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -v`
2. Lint: `.venv/bin/ruff check src tests` (fall back to `ruff check src tests`; if ruff is missing, say the `dev` extra isn't installed rather than skipping silently).
3. Types: `.venv/bin/mypy` (strict mode configured in pyproject; same fallback rule).
4. Setup state: `PYTHONPATH=src python3 -m japan_agent.cli doctor`

Interpretation rules:
- `doctor` FAILs for missing credentials/whitelist/persona are EXPECTED until the account
  checklist (`docs/ACCOUNT-SETUP.md`) is done — report them as pending setup, not defects.
- Any refusal that mentions stale sources, empty whitelist, or the live gate is the fail-closed
  design working. Never propose weakening a gate to turn a FAIL green.
- A ResourceWarning-as-error means a leaked file/socket in code or tests — fix the leak, never
  drop the `-W` flag.

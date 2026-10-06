# Plan 4 (research layer) — independent final review

Branch `feat/predict-research`, range `9218529..HEAD`.
Reviewer: Fable 5.1. Real SDK and network never called; Task 6 (live smoke) not run.

Status: **no Critical or Important findings.** Three Minor findings fixed (two commits); two Minor findings left with reasons.

Commits:
- `7a284e0` fix(predict): scan tool-error text for exposure; refusal rows for aborted markets
- `1a9065f` fix(predict): fake SDK rejects option names unknown to claude-agent-sdk 0.2.163

## Findings

### M1 (Minor, fixed in 7a284e0) — exposure scan skipped tool-error text
- `src/predict_agent/research_run.py:179-183` (`_exposure_texts`).
- Verified: probe built a `ResearchOutcome` whose transcript held a `tool_error` event with error text "Polymarket traders give it 62% (upstream 403)"; `scan(_exposure_texts(o))` returned `()`. The CLI shows a failed tool's error text to the model, so spec §6 ("everything the model saw") covers it.
- Fix: `_exposure_texts` now also walks each event's `error` string. Denial reasons and tool inputs stay excluded (they are the hook's or the model's own words). RED: `test_exposure_in_a_failed_tool_call_is_flagged_on_the_forecast` failed (flags lacked `venue:kalshi`); GREEN after the change.

### M2 (Minor, fixed in 7a284e0) — aborted markets left no durable refusal row
- `src/predict_agent/research_run.py:361-364` (systemic-failure abort in `run_research_day`).
- Verified: new test ran a TOOLSET_MISMATCH with three candidates; the `refusals` table held no rows for the two unresearched markets (`Lists differ: [] != [...]`), while BUDGET/VOLUME/STALE skips all record one. Only the printed summary carried the count.
- Fix: each remaining candidate gets `record_refusal(..., "research", "ABORTED_<code>", "run aborted after <code>")`; the summary count is unchanged (existing abort tests still pass). RED then GREEN as above.

### M3 (Minor, fixed in 1a9065f) — fake `ClaudeAgentOptions` accepted any keyword
- `tests/predict/fake_sdk.py:18-21`, module factory at `module()`.
- Verified: `FakeSdk(Script()).module().ClaudeAgentOptions(allowed_tool=[])` succeeded. A misspelled option in `build_options` would therefore pass every test except `test_real_sdk_accepts_every_option_name`, which CI skips (no SDK installed; a CI job with the extra is deferred to Plan 5).
- Fix: the fake pins the 49 `ClaudeAgentOptions` field names extracted (via `ast`) from SDK 0.2.163's `types.py` and raises `TypeError` for anything else. RED: `test_fake_options_accept_only_sdk_option_names` failed (`TypeError not raised`); GREEN after. All existing option tests still pass, which also proves `build_options` uses only real field names.

### M4 (Minor, left) — interrupted attempts are charged at the *current* `per_forecast_usd`, not the attempt's cohort cap
- `src/predict_agent/budget.py:60-72` (`recover_interrupted_attempts`), `day_usage` likewise for STARTED rows.
- Verified by reading: the cap passed in is the running config's. `per_forecast_usd` is part of the cohort identity, so if the operator lowers it between a crash and the next run, the orphaned attempt of the old cohort is charged the new, lower cap (under-count by at most one cap once; over-count if raised).
- Left because: one-off, bounded by a single cap, only on a config change across a crash, and the conservative alternative (max of the two caps) adds identity-JSON parsing to a Plan 2 function for little gain. Worth a line in Plan 5 if the cron wrapper touches recovery.

### M5 (Minor, left) — INTERRUPTED attempts never count toward `MAX_FAILED_ENTRY_ATTEMPTS`
- `src/predict_agent/research_run.py:95-120` (`candidates`).
- Verified by reading and by `RetryLimitTests`: a market whose research always crashes the process (hang then external kill) is re-researched every run, each charged at the cap. Damage is bounded by `daily_usd` and the volume cap, and the design choice ("a crash, not a verdict on the market") is explicit in the docstring and tests. The wall-clock session timeout that would make this concrete is deferred to Plan 5, so I did not change the policy.

## What I checked and found sound

Isolation boundary and the no-price guarantee
- `tests/predict/test_cli_isolation.py`: allow-list `{config, util}` and the transitive fresh-interpreter test both pass; `research/` imports nothing from policy, paper, cash, clob, gamma or market data. `sdk.py` only imports `.config.blocked_host` and stdlib.
- Rendered prompt carries question, verbatim rules, resolution source, end date and today's date only (`gamma.rules_payload` has exactly those four keys). `test_the_prompt_never_carries_a_price` passes.
- Against the SDK 0.2.163 source and the bundled CLI 2.1.286 binary: `tools=[...]` maps to `--tools WebSearch,WebFetch`; `setting_sources=[]` maps to `--setting-sources=` and `skills=[]` does not re-default it to `["user","project"]` (`_apply_skills_defaults`); `strict_mcp_config` → `--strict-mcp-config`; `disallowed_tools` → `--disallowedTools`; a `str` system prompt replaces the default; `cwd` is passed to the subprocess and must exist; `verbatim_prompts` only logs a warning on an old CLI (the bundled one is newer).
- Hook contract: the CLI's initialize validator accepts `matcher: null` (`st.matcher != null && typeof !== "string"` is the only rejection) and matches with `matcher ?? ""`, i.e. all tools. `permissionDecision` allow/deny, `permissionDecisionReason` and `updatedInput` are all present in the CLI. `dontAsk` resolves to `deny` for anything not explicitly allowed. WebSearch's input schema has `allowed_domains`/`blocked_domains` and the tool errors when both are set, which makes the hook's `pop("allowed_domains")` load-bearing and correct.
- Init report: the CLI's `system/init` carries `tools` (names), `mcp_servers`, `model`, `permissionMode`; `toolset_problem` reads exactly the first two. Pre-init tool calls are denied (`verified` flag); a session with no init or with extra tools/MCP servers fails `TOOLSET_MISMATCH` and the client is closed before the temp cwd is removed.
- `_fetchable` probes: port suffix allowed, uppercase host allowed (parsed lowercase), trailing-dot host denied, query/fragment carrying a blocked domain denied, local-network names denied. Boundary-anchoring and unquoted matching remain deferred as instructed.
- Result mapping: `error_max_budget_usd`, `error_max_turns`, `error_max_structured_output_retries`, `error_during_execution` are the CLI's four result subtypes; `success` with `is_error` or no `structured_output` fails closed (`NO_RESULT`); a hook exception fails closed (`HOOK_ERROR`); any SDK exception yields `SDK_ERROR` with the type name only.

Spend accounting
- Every paid call opens a `research_attempts` row before the runner is invoked; schema CHECKs force `cost_usd` on every finished row and `verify_ledger` ties a SUCCEEDED attempt's cost to its forecast.
- Unknown cost (no result, exception, mismatch) is charged at `per_forecast_usd`; a crash leaves STARTED and the next run's `recover_interrupted_attempts` charges the cap. `day_usage` counts STARTED rows at the cap and sums Decimal in Python over the UTC day of `started_at`, parsed. `budget_refusal` refuses when one more full-cost attempt could exceed the daily cap; `stop` is sticky across the rest of the run; VOLUME counts attempts (failed included), which is conservative.
- Overlap: `fcntl.flock` on `<db>.research.lock` around the whole run; the second process exits 2 before any attempt. RECORD_FAILED after another process closed the attempt is tolerated only when the row is no longer STARTED.
- `max_budget_usd` is a float operational stop; the SDK's reported cost (which includes the overshooting turn) is what the ledger stores.

Run correctness
- No double research: candidates exclude markets with an entry forecast for the cohort; attempts left STARTED are closed before new ones; `record_forecast` refuses a second entry forecast in its transaction.
- No double baseline: `attach_baseline` refuses when one exists; `resume_step` only returns NEEDS_BASELINE when none is attached; expired windows are marked NO_TIMELY_BASELINE, never given a late book.
- No double trade: `trade_ready` only visits undecided portfolios and `decide_portfolio` checks `already_traded` inside its transaction.
- Observation vs fetch: `snapshot_book` takes `fetched_at = now_fn()` after the HTTP call and `parse_book` sets `observed_at` from the server timestamp; the baseline window is enforced on `fetched_at` only.
- Stale discovery (>24h) refuses new research but still resumes and trades; aborts on TOOLSET_MISMATCH/HOOK_ERROR and on two consecutive SDK_ERRORs.

Tests and fakes
- Fake hook inputs use the real keys (`tool_name`, `tool_input`, `tool_response`, `error`); fake messages carry the fields the adapter reads; `receive_response` terminates after the result as the real one does.
- Full suite under Python 3.14 (venv) and the predict suite under the read-only 3.11 sdkenv (where the real SDK is importable and `test_real_sdk_accepts_every_option_name` runs) both pass.
- Spec coverage of §6/§7 matches the plan's self-review record; weekly updates, flagged-forecast reporting and the cron lock are explicitly Plan 5.

## Declined to judge
- Whether CLI 2.1.286 lists `StructuredOutput` (or anything else) in the init `tools` for `--tools WebSearch,WebFetch --json-schema`, and whether the StructuredOutput tool needs the hook's `allow` under `dontAsk`: fail-closed either way, pinned by the human's live smoke (Task 6).
- Whether WebFetch surfaces HTTP failures as tool errors (PostToolUseFailure) or as text results: out of scope with the redirect-notice item.
- Everything in the "ruled out of scope" list of the brief.

## Verification output
- `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -v`: **Ran 437 tests — OK (skipped=1)** (434 before; +3 new tests). The skip is the real-SDK option test, which passes under the 3.11 sdkenv.
- `ruff check src/predict_agent tests/predict`: All checks passed.
- `mypy src/predict_agent`: Success: no issues found in 31 source files.
- `bash -n scripts/daily.sh scripts/weekly.sh`: OK.
- `git diff --check`: OK.
- Not pushed; no PR created. Untracked `.superpowers/`, `AGENTS.md`, `docs/POLYMARKET-PIVOT-PLAN.md` untouched; `config/predict-policy.json` and `japan_agent` untouched.

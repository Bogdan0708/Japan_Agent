#!/usr/bin/env bash
# Weekly deep cycle (cron: 0 10 * * 0 Europe/London).
# Refreshes J-Quants historical context, runs a deep research pass, verifies the
# event ledger, rebuilds the JSONL mirror, and renders the public weekly post
# from a reconciled report file. Publishing to GitHub Pages stays a reviewed
# git push, not a cron job.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOG_DIR/weekly-$STAMP.log"

cd "$ROOT"
export PYTHONPATH="$ROOT/src"

exec 9>"$ROOT/data/.weekly.lock"
if ! flock -n 9; then
  echo "another weekly run holds the lock; exiting" >>"$LOG"
  exit 1
fi

{
  echo "== weekly cycle $STAMP =="

  # J-Quants free data is 12 weeks delayed; refresh historical context for the
  # mapped JPX codes at a ~13-week lag, shifted off weekends.
  codes_file="$ROOT/config/jquants-codes.json"
  if [[ -f "$codes_file" ]]; then
    jq_date="$(date -u -d '91 days ago' +%F)"
    case "$(date -u -d "$jq_date" +%u)" in
      6) jq_date="$(date -u -d "$jq_date - 1 day" +%F)" ;;
      7) jq_date="$(date -u -d "$jq_date - 2 days" +%F)" ;;
    esac
    while read -r code; do
      python3 -m japan_agent.cli ingest-jquants --code "$code" --date "$jq_date" \
        || echo "J-Quants refresh failed for code $code (possibly a JPX holiday)"
    done < <(python3 -c "import json,sys;print('\n'.join(json.load(open('$codes_file'))['codes'].values()))")
  else
    echo "no config/jquants-codes.json; skipping J-Quants refresh"
  fi

  # Fresh Sunday NEWS/TDnet digests must exist before the deep review: the
  # NEWS gate is 24 hours, so Friday's heartbeat is stale by Sunday morning.
  for kind in tdnet news; do
    digest="$ROOT/data/digests/$kind-$(date -u +%Y%m%d).json"
    if [[ -f "$digest" ]]; then
      python3 -m japan_agent.cli ingest-digest --source "${kind^^}" "$digest"
    else
      echo "no Sunday $kind digest at $digest"
    fi
  done

  # Refresh whitelist quotes so the PRICE window reaches into Sunday.
  python3 -m japan_agent.cli collect-prices

  # Deep weekly portfolio review against the reconciled portfolio, written
  # atomically. A refusal MUST fail this job: a green weekly run that skipped
  # its own research would be a false-green for monitoring.
  RESEARCH_FAILED=0
  decision_tmp="$ROOT/data/weekly-decision.json.tmp"
  if python3 -m japan_agent.cli research --mode weekly \
      --portfolio "$ROOT/data/portfolio.json" > "$decision_tmp"; then
    python3 -m json.tool "$decision_tmp" > /dev/null
    mv "$decision_tmp" "$ROOT/data/weekly-decision.json"
    echo "weekly review decision written to data/weekly-decision.json"
  else
    rm -f "$decision_tmp"
    RESEARCH_FAILED=1
    echo "weekly review refused (stale sources or missing data/portfolio.json)"
  fi

  python3 -m japan_agent.cli verify-chain
  python3 -m japan_agent.cli journal-sync

  report="$ROOT/data/weekly-report.json"
  if [[ -f "$report" ]]; then
    python3 -m japan_agent.cli journal-weekly --report "$report"
  else
    echo "no reconciled weekly report at $report; skipping post"
  fi

  if [[ "$RESEARCH_FAILED" -ne 0 ]]; then
    echo "WEEKLY JOB FAILED: the deep review did not run"
    exit 1
  fi
} >> "$LOG" 2>&1

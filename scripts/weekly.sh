#!/usr/bin/env bash
# Weekly journal cycle (cron: 0 10 * * 0 Europe/London).
# Rebuilds the JSONL event mirror and renders the public weekly post from a
# reconciled report file the human/agent review produces (config/weekly-report
# schema). Publishing to GitHub Pages stays a reviewed git push, not a cron job.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

cd "$ROOT"
export PYTHONPATH="$ROOT/src"

{
  echo "== weekly cycle $STAMP =="
  python3 -m japan_agent.cli journal-sync

  report="$ROOT/data/weekly-report.json"
  if [[ -f "$report" ]]; then
    python3 -m japan_agent.cli journal-weekly --report "$report"
  else
    echo "no reconciled weekly report at $report; skipping post"
  fi
} >> "$LOG_DIR/weekly-$STAMP.log" 2>&1

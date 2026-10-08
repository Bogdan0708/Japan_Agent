#!/usr/bin/env bash
# predict-agent daily cycle (cron example: 30 6 * * * in Europe/London).
# Paper only: collect Polymarket data, settle resolved paper tickets, research new
# markets and due weekly updates with Claude (budget-capped, never shown a price),
# trade on paper, and write the performance report. There is no execution code.
#
# PREDICT_PYTHON: an interpreter with the `predict` extra installed (research needs
#   the Claude Agent SDK); defaults to python3.
# PREDICT_DAILY_TIMEOUT: wall-clock cap for the whole cycle (default 3h). Each
#   research session is also capped (15 min) inside the CLI.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"
chmod 700 "$ROOT/data" "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOG_DIR/predict-daily-$STAMP.log"

cd "$ROOT"
export PYTHONPATH="$ROOT/src"
export PYTHONUNBUFFERED=1
PYTHON="${PREDICT_PYTHON:-python3}"

if ! command -v flock >/dev/null 2>&1 || ! command -v timeout >/dev/null 2>&1; then
  echo "predict-daily needs flock and timeout (util-linux, coreutils)" >>"$LOG"
  exit 1
fi

# One cycle at a time: cron and a manual invocation must never interleave.
exec 9>"$ROOT/data/.predict-daily.lock"
if ! flock -n 9; then
  echo "another predict-daily run holds the lock; exiting" >>"$LOG"
  exit 1
fi

status=0
{
  echo "== predict-daily $STAMP =="
  timeout --kill-after=60 "${PREDICT_DAILY_TIMEOUT:-3h}" \
    "$PYTHON" -m predict_agent.cli run-daily || status=$?
  echo "== exit $status =="
} >>"$LOG" 2>&1
exit "$status"

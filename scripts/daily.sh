#!/usr/bin/env bash
# Daily post-Tokyo-close cycle (cron: 15 7 * * 1-5 Europe/London).
# Collects fresh data and runs research. It never proposes or executes:
# tickets remain a deliberate human-initiated step (see docs/OPERATIONS.md),
# and the research command itself fails closed if any source is stale.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOG_DIR/daily-$STAMP.log"

cd "$ROOT"
export PYTHONPATH="$ROOT/src"

# One run at a time: cron and a manual invocation must never interleave.
exec 9>"$ROOT/data/.daily.lock"
if ! flock -n 9; then
  echo "another daily run holds the lock; exiting" >>"$LOG"
  exit 1
fi

{
  echo "== daily cycle $STAMP =="
  python3 -m japan_agent.cli collect-prices

  # EDINET publishes on Tokyo's calendar. At 07:15 London it is already
  # afternoon in Tokyo, so ingest today's Tokyo date (the current filing day)
  # and yesterday's (anything filed after the previous run).
  for tokyo_date in "$(TZ=Asia/Tokyo date +%F)" "$(TZ=Asia/Tokyo date -d yesterday +%F)"; do
    python3 -m japan_agent.cli ingest-edinet --date "$tokyo_date"
  done

  # A TDnet/news digest is produced by the research assistant session and
  # dropped here before this script runs; ingest it when present.
  for kind in tdnet news; do
    digest="$ROOT/data/digests/$kind-$(date -u +%Y%m%d).json"
    if [[ -f "$digest" ]]; then
      python3 -m japan_agent.cli ingest-digest --source "${kind^^}" "$digest"
    else
      echo "no $kind digest for today at $digest"
    fi
  done

  # Write the decision atomically: a failed research run must never leave a
  # truncated or empty latest-decision.json behind.
  decision_tmp="$ROOT/data/latest-decision.json.tmp"
  python3 -m japan_agent.cli research > "$decision_tmp"
  python3 -m json.tool "$decision_tmp" > /dev/null
  mv "$decision_tmp" "$ROOT/data/latest-decision.json"
  echo "research decision written to data/latest-decision.json"
} >> "$LOG" 2>&1

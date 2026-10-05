from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .collect import (
    discover,
    finish_run,
    poll_resolutions,
    record_geoblock,
    snapshot_eligible,
    start_run,
)
from .config import ConfigError, DiscoveryConfig, Settings, load_discovery_config
from .db import connect, record_refusal, verify_journal
from .gamma import ParseError
from .http import FetchError, JsonClient
from .invariants import verify_ledger
from .report import render_markdown, shortlist
from .settlement import settle_open_tickets
from .util import utc_now


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="predict-agent", description="Paper-only Polymarket data collector (Phase 1)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check configuration; fails closed")
    sub.add_parser("discover", help="discover eligible markets")
    sub.add_parser("snapshot", help="snapshot books for the latest discovery run")
    sub.add_parser("resolve", help="poll resolution state for known markets")
    sub.add_parser("settle", help="settle open paper tickets from stored resolutions (offline)")
    report = sub.add_parser("report", help="write the shortlist report")
    which = report.add_mutually_exclusive_group(required=True)
    which.add_argument("--run")
    which.add_argument("--latest", action="store_true")
    sub.add_parser("run-data", help="geoblock, discover, snapshot, resolve, report")
    return parser


def _latest_discovery_run(conn_path: Path) -> str | None:
    conn = connect(conn_path)
    try:
        row = conn.execute(
            "SELECT run_id FROM runs WHERE command IN ('discover', 'run-data') "
            "AND status = 'COMPLETED' ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row["run_id"] if row else None
    finally:
        conn.close()


def _write_report(settings: Settings, conn_path: Path, run_id: str) -> Path:
    conn = connect(conn_path)
    try:
        data = shortlist(conn, run_id)
    finally:
        conn.close()
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    markdown = settings.reports_dir / f"shortlist-{run_id}.md"
    markdown.write_text(render_markdown(data), encoding="utf-8")
    (settings.reports_dir / f"shortlist-{run_id}.json").write_text(
        json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
    )
    return markdown


def main(
    argv: list[str] | None = None,
    *,
    client: JsonClient | None = None,
    root: Path | None = None,
    now_fn: Callable[[], datetime] = utc_now,
) -> int:
    args = _parser().parse_args(argv)
    settings = Settings.from_root((root or Path.cwd()).resolve())
    try:
        config, policy_hash = load_discovery_config(settings.policy_path)
    except ConfigError as error:
        print(f"predict-agent: {error}", file=sys.stderr)
        return 2
    if args.command == "doctor":
        conn = connect(settings.database_path)
        try:
            journal_ok = verify_journal(conn)
            problems = verify_ledger(conn)
        finally:
            conn.close()
        print(f"policy {policy_hash[:12]} ok; journal chain {'ok' if journal_ok else 'BROKEN'}")
        for problem in problems:
            print(f"ledger: {problem}")
        print(f"ledger: {'ok' if not problems else f'{len(problems)} problem(s)'}")
        return 0 if journal_ok and not problems else 3
    if args.command == "settle":
        now = now_fn()
        conn = connect(settings.database_path)
        try:
            summary = settle_open_tickets(conn, now)
        finally:
            conn.close()
        print(f"settled {summary.settled}; pending {len(summary.pending)}")
        for item in summary.pending:
            age = item.age(now)
            since = "never resolved" if age is None else f"resolved {age} ago"
            print(f"pending ticket {item.ticket_id} {item.condition_id}: {item.reason} ({since})")
        return 0
    http = client or JsonClient()
    if args.command == "report":
        run_id = args.run or _latest_discovery_run(settings.database_path)
        if run_id is None:
            print("predict-agent: no completed discovery run yet", file=sys.stderr)
            return 2
        try:
            print(_write_report(settings, settings.database_path, run_id))
        except ValueError as error:
            print(f"predict-agent: {error}", file=sys.stderr)
            return 2
        return 0
    conn = connect(settings.database_path)
    try:
        run_id = start_run(conn, args.command, policy_hash, now_fn())
        try:
            code = _run_steps(args.command, conn, http, config, settings, run_id, now_fn)
        except (FetchError, ParseError) as error:
            reason = "FETCH_ERROR" if isinstance(error, FetchError) else "PARSE_ERROR"
            record_refusal(conn, run_id, None, args.command, reason, str(error), now_fn())
            finish_run(conn, run_id, "FAILED", now_fn())
            print(f"predict-agent: run {run_id} failed: {error}", file=sys.stderr)
            return 4
        finish_run(conn, run_id, "COMPLETED" if code == 0 else "FAILED", now_fn())
    finally:
        conn.close()
    if code == 0 and args.command == "run-data":
        print(_write_report(settings, settings.database_path, run_id))
    return code


def _run_steps(
    command: str,
    conn: sqlite3.Connection,
    http: JsonClient,
    config: DiscoveryConfig,
    settings: Settings,
    run_id: str,
    now_fn: Callable[[], datetime],
) -> int:
    geoblock = record_geoblock(conn, http, run_id)
    print(f"run {run_id}; geoblock (audit only): {geoblock}")
    if command in ("discover", "run-data"):
        summary = discover(conn, http, config, run_id, now_fn())
        print(f"seen {summary.markets_seen}, eligible {summary.eligible}")
    if command == "snapshot":
        latest = _latest_discovery_run(settings.database_path)
        if latest is None:
            print("predict-agent: no completed discovery run yet", file=sys.stderr)
            return 2
        conn.execute("UPDATE runs SET source_run_id = ? WHERE run_id = ?", (latest, run_id))
        print(f"snapshots {snapshot_eligible(conn, http, latest, run_id, now_fn)}")
    if command == "run-data":
        print(f"snapshots {snapshot_eligible(conn, http, run_id, run_id, now_fn)}")
    if command in ("resolve", "run-data"):
        print(f"resolution observations {poll_resolutions(conn, http, run_id, now_fn)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

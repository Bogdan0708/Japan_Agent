from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .cohorts import code_version
from .collect import (
    book_outage,
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
from .paper import trade_ready
from .performance import performance, render_performance
from .policy_params import load_policy_config
from .report import render_markdown, shortlist
from .research.config import load_research_config
from .research.sdk import ResearchUnavailable, load_sdk, run_research
from .research_run import ResearchRunner, ResearchSummary, run_research_day
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
    sub.add_parser("trade", help="decide forecasts with timely baselines; paper only (offline)")
    sub.add_parser("research", help="forecast eligible markets with Claude (no prices), then trade")
    report = sub.add_parser("report", help="write the shortlist or performance report")
    which = report.add_mutually_exclusive_group(required=True)
    which.add_argument("--run")
    which.add_argument("--latest", action="store_true")
    which.add_argument("--performance", action="store_true",
                       help="forecast scores and paper P&L per cohort (offline)")
    sub.add_parser("run-data", help="geoblock, discover, snapshot, resolve, report")
    sub.add_parser(
        "run-daily",
        help="the daily cycle: run-data, settle, research (and updates), trade, "
        "performance report; one run at a time",
    )
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
    _write_atomic(markdown, render_markdown(data))
    _write_atomic(
        settings.reports_dir / f"shortlist-{run_id}.json",
        json.dumps(data, indent=2, sort_keys=True),
    )
    return markdown


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temporary file and rename, so a crash never leaves a truncated report."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def _exclusive(settings: Settings, suffix: str) -> Iterator[bool]:
    """Hold a non-blocking lock on `<database>{suffix}`; yields False when another process
    holds it. The lock is released when the block exits (or the process dies)."""
    lock_path = settings.database_path.with_name(settings.database_path.name + suffix)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # BlockingIOError is a subclass
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _write_performance(settings: Settings, now: datetime) -> Path:
    """Write performance-<UTC stamp>.md and .json; offline and read-only on the ledger."""
    conn = connect(settings.database_path)
    try:
        data = performance(conn, now)
    finally:
        conn.close()
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stem = settings.reports_dir / f"performance-{now.strftime('%Y%m%dT%H%M%SZ')}"
    markdown = stem.with_suffix(".md")
    _write_atomic(markdown, render_performance(data))
    _write_atomic(stem.with_suffix(".json"), json.dumps(data, indent=2, sort_keys=True))
    return markdown


def main(
    argv: list[str] | None = None,
    *,
    client: JsonClient | None = None,
    root: Path | None = None,
    now_fn: Callable[[], datetime] = utc_now,
    runner: ResearchRunner | None = None,
) -> int:
    args = _parser().parse_args(argv)
    settings = Settings.from_root((root or Path.cwd()).resolve())
    try:
        config, policy_hash = load_discovery_config(settings.policy_path)
    except ConfigError as error:
        print(f"predict-agent: {error}", file=sys.stderr)
        return 2
    if args.command == "doctor":
        try:
            load_policy_config(settings.policy_path)
            research = load_research_config(settings.policy_path)
        except ConfigError as error:
            print(f"predict-agent: {error}", file=sys.stderr)
            return 2
        try:
            load_sdk()
            sdk_state = "installed"
        except ResearchUnavailable:
            sdk_state = "not installed (research disabled)"
        print(f"research: model {research.model}; Claude Agent SDK {sdk_state}")
        conn = connect(settings.database_path)
        try:
            journal_ok = verify_journal(conn)
            print(f"policy {policy_hash[:12]} ok; journal chain {'ok' if journal_ok else 'BROKEN'}")
            problems = verify_ledger(conn)
        finally:
            conn.close()
        for problem in problems:
            print(f"ledger: {problem}")
        print(f"ledger: {'ok' if not problems else f'{len(problems)} problem(s)'}")
        return 0 if journal_ok and not problems else 3
    if args.command == "trade":
        return _trade(settings, now_fn)
    if args.command == "settle":
        return _settle(settings, now_fn)
    if args.command == "report" and args.performance:
        return _report_performance(settings, now_fn)
    http = client or JsonClient()
    if args.command == "research":
        return _research(settings, http, policy_hash, runner, now_fn)
    if args.command == "run-daily":
        return _run_daily(settings, http, config, policy_hash, runner, now_fn)
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
    return _data(args.command, settings, http, config, policy_hash, now_fn)


def _data(
    command: str,
    settings: Settings,
    http: JsonClient,
    config: DiscoveryConfig,
    config_hash: str,
    now_fn: Callable[[], datetime],
) -> int:
    """discover | snapshot | resolve | run-data as one recorded run; 4 on a fetch or parse
    failure (recorded as the run's refusal)."""
    conn = connect(settings.database_path)
    try:
        run_id = start_run(conn, command, config_hash, now_fn())
        try:
            code = _run_steps(command, conn, http, config, settings, run_id, now_fn)
        except (FetchError, ParseError) as error:
            reason = "FETCH_ERROR" if isinstance(error, FetchError) else "PARSE_ERROR"
            record_refusal(conn, run_id, None, command, reason, str(error), now_fn())
            finish_run(conn, run_id, "FAILED", now_fn())
            print(f"predict-agent: run {run_id} failed: {error}", file=sys.stderr)
            return 4
        finish_run(conn, run_id, "COMPLETED" if code == 0 else "FAILED", now_fn())
    finally:
        conn.close()
    if code == 0 and command == "run-data":
        print(_write_report(settings, settings.database_path, run_id))
    return code


def _trade(settings: Settings, now_fn: Callable[[], datetime]) -> int:
    conn = connect(settings.database_path)
    try:
        trades = trade_ready(conn, now_fn)
    finally:
        conn.close()
    reasons = ", ".join(f"{code} {count}" for code, count in sorted(trades.refused.items()))
    refused = sum(trades.refused.values())
    print(
        f"traded {trades.traded}; refused {refused}"
        + (f" ({reasons})" if reasons else "")
        + f"; waiting {trades.waiting}"
        + (f"; skipped {trades.skipped} (decided by another run)" if trades.skipped else "")
    )
    return 0


def _settle(settings: Settings, now_fn: Callable[[], datetime]) -> int:
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


def _report_performance(settings: Settings, now_fn: Callable[[], datetime]) -> int:
    print(_write_performance(settings, now_fn()))
    return 0


def _run_daily(
    settings: Settings,
    http: JsonClient,
    config: DiscoveryConfig,
    config_hash: str,
    runner: ResearchRunner | None,
    now_fn: Callable[[], datetime],
) -> int:
    """The daily cycle (spec §3, §10 item 5): collect (run-data), settle what resolved,
    research new markets and due updates (which also trades on the fresh books), decide
    anything left, then write the performance report. One step failing never stops the
    later independent ones, but research needs today's data run: it is skipped when that
    failed. 0 when every step succeeded, 6 otherwise; 2 when another run-daily holds the
    lock."""
    with _exclusive(settings, ".daily.lock") as held:
        if not held:
            print("predict-agent: another run-daily is in progress", file=sys.stderr)
            return 2
        results: list[tuple[str, int | None]] = []

        def step(name: str, action: Callable[[], int]) -> int:
            print(f"== {name} ==", flush=True)
            try:
                code = action()
            except Exception as error:  # noqa: BLE001 — one failed step must not stop the rest
                print(f"run-daily: {name} failed: {type(error).__name__}: {error}",
                      file=sys.stderr)
                code = 1
            results.append((name, code))
            return code

        data = step("data", lambda: _data("run-data", settings, http, config, config_hash,
                                          now_fn))
        step("settle", lambda: _settle(settings, now_fn))
        if data == 0:
            step("research", lambda: _research(settings, http, config_hash, runner, now_fn))
        else:
            print("run-daily: research skipped: today's data run failed", file=sys.stderr)
            results.append(("research", None))
        step("trade", lambda: _trade(settings, now_fn))
        step("report", lambda: _report_performance(settings, now_fn))
    failed = [
        f"{name} ({'skipped' if code is None else code})"
        for name, code in results
        if code != 0
    ]
    print("run-daily: " + ("all steps succeeded" if not failed else "failed: "
                            + ", ".join(failed)))
    return 0 if not failed else 6


def _research(
    settings: Settings,
    http: JsonClient,
    config_hash: str,
    runner: ResearchRunner | None,
    now_fn: Callable[[], datetime],
) -> int:
    try:
        policy = load_policy_config(settings.policy_path)
        research = load_research_config(settings.policy_path)
    except ConfigError as error:
        print(f"predict-agent: {error}", file=sys.stderr)
        return 2
    if runner is None:
        try:
            load_sdk()
        except ResearchUnavailable as error:
            print(f"predict-agent: {error}", file=sys.stderr)
            return 2
        runner = run_research
    # One research run at a time: a second run would recover the first one's live attempts.
    with _exclusive(settings, ".research.lock") as held:
        if not held:
            print("predict-agent: another research run is in progress", file=sys.stderr)
            return 2
        conn = connect(settings.database_path)
        try:
            summary = run_research_day(
                conn,
                http,
                runner,
                policy=policy,
                research=research,
                config_hash=config_hash,
                code_version=code_version(settings.root),
                now_fn=now_fn,
            )
        finally:
            conn.close()
    print(_research_line(summary))
    broken = summary.operational_failures()
    if broken:
        listed = ", ".join(f"{code} {n}" for code, n in broken.items())
        print(f"predict-agent: research had operational failures: {listed}", file=sys.stderr)
        return 5
    return 0


def _research_line(summary: ResearchSummary) -> str:
    def counts(counter: dict[str, int]) -> str:
        return ", ".join(f"{code} {n}" for code, n in sorted(counter.items())) or "none"

    return (
        f"cohort {summary.cohort_id[:12]}; recovered attempts {summary.recovered}; "
        f"forecasts {summary.forecasts} (abstained {summary.abstentions}); "
        f"failed: {counts(summary.failed)}; skipped: {counts(summary.skipped)}; "
        f"baselines {summary.baselines}; no timely baseline {summary.no_timely_baseline}; "
        f"traded {summary.traded}; updates {summary.updates}"
    )


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
    outage: str | None = None
    if command == "snapshot":
        latest = _latest_discovery_run(settings.database_path)
        if latest is None:
            print("predict-agent: no completed discovery run yet", file=sys.stderr)
            return 2
        conn.execute("UPDATE runs SET source_run_id = ? WHERE run_id = ?", (latest, run_id))
        stored = snapshot_eligible(conn, http, latest, run_id, now_fn)
        print(f"snapshots {stored}")
        outage = book_outage(conn, run_id, stored)
    if command == "run-data":
        stored = snapshot_eligible(conn, http, run_id, run_id, now_fn)
        print(f"snapshots {stored}")
        outage = book_outage(conn, run_id, stored)
    if outage is not None:
        print(f"predict-agent: books unavailable: {outage}", file=sys.stderr)
        record_refusal(conn, run_id, None, command, "BOOKS_UNAVAILABLE", outage, now_fn())
    if command in ("resolve", "run-data"):
        print(f"resolution observations {poll_resolutions(conn, http, run_id, now_fn)}")
    return 0 if outage is None else 4


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from .approve.polling import TelegramPoller
from .approve.service import ApprovalService
from .approve.telegram import TelegramApprovalChannel, TelegramConfig
from .approve.webhook import serve_webhook
from .config import Settings, load_dotenv
from .execute.service import ExecutionService, assert_environment_allowed
from .execute.t212 import Trading212Client
from .identity import choose_persona_with_claude, write_persona
from .ingest.collect import WhitelistPriceCollector, load_data_symbols
from .ingest.prices import NormalizedPriceImporter, YFinanceDailySource
from .ingest.research_sources import EdinetV2Client, JQuantsV2Client, ResearchSourceIngester
from .journal import write_weekly_post
from .models import (
    Portfolio,
    PortfolioSnapshot,
    Position,
    ResearchDecision,
    canonical_json,
    decimal,
)
from .storage import Database
from .research import AgentSdkResearcher
from .research.snapshot import ResearchSnapshotAssembler
from .time import parse_datetime, utc_now
from .workflow import ProposalWorkflow


def _root(value: str | None) -> Path:
    return Path(value or Path.cwd()).resolve()


def _settings(root: Path) -> Settings:
    load_dotenv(root / ".env")
    settings = Settings.from_env(root)
    settings.ensure_directories()
    return settings


def _database(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database


def _read_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _portfolio(value: dict[str, Any]) -> Portfolio:
    positions = tuple(
        Position(
            ticker=str(item["ticker"]),
            quantity=decimal(item["quantity"]),
            market_value_gbp=decimal(item["market_value_gbp"]),
        )
        for item in value.get("positions", [])
    )
    return Portfolio(cash_gbp=decimal(value["cash_gbp"]), positions=positions)


def _broker(settings: Settings) -> Trading212Client:
    if not settings.t212_api_key or not settings.t212_api_secret:
        raise RuntimeError("T212_API_KEY and T212_API_SECRET are required")
    return Trading212Client(
        api_key=settings.t212_api_key,
        api_secret=settings.t212_api_secret,
        environment=settings.t212_environment,
    )


def _telegram(settings: Settings, database: Database) -> TelegramApprovalChannel:
    values = (
        settings.telegram_bot_token,
        settings.telegram_approval_chat_id,
        settings.telegram_approver_user_id,
        settings.telegram_webhook_secret,
    )
    if not all(values):
        raise RuntimeError("all Telegram settings are required")
    return TelegramApprovalChannel(
        TelegramConfig(
            bot_token=str(settings.telegram_bot_token),
            approval_chat_id=str(settings.telegram_approval_chat_id),
            approver_user_id=str(settings.telegram_approver_user_id),
            webhook_secret=str(settings.telegram_webhook_secret),
        ),
        ApprovalService(database),
    )


def command_init(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    _database(settings)
    print(f"initialized paper-first database at {settings.database_path}")


def command_identity(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    persona = asyncio.run(choose_persona_with_claude(settings.claude_model))
    paths = write_persona(settings.root, persona)
    print(f"{persona.name} chose its identity: {paths[0].name}, {paths[1].name}")


def command_ingest_prices(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    snapshots = NormalizedPriceImporter(database).import_file(Path(args.file))
    print(f"ingested {len(snapshots)} timestamped normalized price snapshots")


def command_collect_prices(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    whitelist = settings.load_whitelist()
    data_symbols = load_data_symbols(Path(args.symbols or settings.root / "config" / "data-symbols.json"))
    snapshots = WhitelistPriceCollector(database, YFinanceDailySource()).collect(
        whitelist, data_symbols, source_label="yfinance-daily"
    )
    print(f"collected {len(snapshots)} whitelist daily closes with matched FX")


def command_ingest_jquants(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    if not settings.jquants_api_key:
        raise RuntimeError("JQUANTS_API_KEY is required")
    trading_date = date.fromisoformat(args.date)
    retrieved_at = utc_now()
    response = JQuantsV2Client(settings.jquants_api_key).daily_bars(
        code=args.code, trading_date=trading_date
    )
    ResearchSourceIngester(database).ingest_jquants_daily_bars(
        response=response,
        code=args.code,
        trading_date=trading_date,
        retrieved_at=retrieved_at,
    )
    print("ingested local J-Quants historical context (free-plan data is 12 weeks delayed)")


def command_ingest_edinet(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    if not settings.edinet_api_key:
        raise RuntimeError("EDINET_API_KEY is required")
    filing_date = date.fromisoformat(args.date)
    retrieved_at = utc_now()
    response = EdinetV2Client(settings.edinet_api_key).document_list(filing_date)
    count = ResearchSourceIngester(database).ingest_edinet_list(
        response=response, filing_date=filing_date, retrieved_at=retrieved_at
    )
    print(f"ingested {count} EDINET filing headlines")


def command_ingest_digest(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    count = ResearchSourceIngester(database).ingest_digest_file(
        Path(args.file), expected_source=args.source
    )
    print(f"ingested {count} timestamped {args.source.upper()} digest items")


def command_research(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    tickers = list(settings.load_whitelist())
    if not tickers:
        raise RuntimeError("verified whitelist is empty")
    bundle = ResearchSnapshotAssembler(database).assemble(
        whitelist_tickers=tickers, now=utc_now()
    )
    result = asyncio.run(AgentSdkResearcher(settings.claude_model).decide(bundle))
    database.append_event(
        kind="RESEARCH_DECISION",
        aggregate_id=None,
        payload={
            "decision": result.__dict__,
            "snapshot_assembled_at": bundle["assembled_at"],
        },
    )
    print(canonical_json(result.__dict__))


def command_propose(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    decision = ResearchDecision.from_dict(_read_json(args.decision))
    portfolio = _portfolio(_read_json(args.portfolio))
    ticket = ProposalWorkflow(settings=settings, database=database).propose(
        decision=decision, portfolio=portfolio, now=utc_now()
    )
    print(canonical_json({"ticket": ticket.to_dict(), "ticket_hash": ticket.fingerprint}))


def command_decide(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    stored = database.get_proposal(args.proposal_id)
    if stored is None:
        raise KeyError(f"proposal {args.proposal_id!r} not found")
    approvals = ApprovalService(database)
    if args.action == "approve":
        result = approvals.approve(
            args.proposal_id,
            approver=args.approver,
            expected_hash=stored.ticket_hash,
            now=utc_now(),
        )
    else:
        result = approvals.reject(
            args.proposal_id,
            approver=args.approver,
            expected_hash=stored.ticket_hash,
            reason=args.reason,
            now=utc_now(),
        )
    print(f"{result.ticket.proposal_id}: {result.status.value}")


def command_execute(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    stored = database.get_proposal(args.proposal_id)
    if stored is None:
        raise KeyError(f"proposal {args.proposal_id!r} not found")
    snapshot = database.latest_snapshot(stored.ticket.ticker)
    if snapshot is None:
        raise RuntimeError("no execution quote exists")
    portfolio_value = _read_json(args.portfolio)
    portfolio_snapshot = PortfolioSnapshot(
        portfolio=_portfolio(portfolio_value),
        observed_at=parse_datetime(str(portfolio_value["observed_at"])),
        source=str(portfolio_value["source"]),
    )
    response = ExecutionService(
        database=database,
        broker=_broker(settings),
        settings=settings,
    ).execute(
        args.proposal_id,
        current_snapshot=snapshot,
        portfolio_snapshot=portfolio_snapshot,
        now=utc_now(),
    )
    print(canonical_json(response))


def command_reconcile(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    response = ExecutionService(
        database=database,
        broker=_broker(settings),
        settings=settings,
    ).reconcile(args.proposal_id, now=utc_now())
    print(canonical_json(response))


def command_discover(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    instruments = _broker(settings).instruments()
    terms = tuple(term.lower() for term in args.term)
    candidates = [
        item
        for item in instruments
        if not terms
        or any(
            term
            in (
                f"{item.get('ticker', '')} {item.get('name', '')} "
                f"{item.get('shortName', '')}"
            ).lower()
            for term in terms
        )
    ]
    # Discovery never mutates the whitelist. A human must verify exact account availability first.
    print(json.dumps(candidates, indent=2, sort_keys=True))


def command_send_ticket(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    stored = database.get_proposal(args.proposal_id)
    if stored is None:
        raise KeyError(f"proposal {args.proposal_id!r} not found")
    response = _telegram(settings, database).send_ticket(stored.ticket)
    print(f"sent Telegram ticket; message id {response.get('result', {}).get('message_id', '?')}")


def command_telegram_serve(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    print(f"serving authenticated Telegram webhook on {args.host}:{args.port}")
    serve_webhook(_telegram(settings, database), host=args.host, port=args.port)


def command_telegram_poll(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    print("long-polling Telegram for approval callbacks (no inbound port required)")
    TelegramPoller(_telegram(settings, database)).poll_forever(timeout=args.timeout)


def command_journal(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    count = _database(settings).sync_jsonl(settings.journal_path)
    print(f"synchronized {count} append-only events to {settings.journal_path}")


def command_weekly_post(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    report = _read_json(args.report)
    benchmarks = {
        str(name): decimal(value) for name, value in report.get("benchmark_returns", {}).items()
    }
    path = write_weekly_post(
        output_dir=settings.root / "pages" / "_posts",
        week_ending=date.fromisoformat(str(report["week_ending"])),
        agent_name=str(report["agent_name"]),
        start_nav_gbp=decimal(report["start_nav_gbp"]),
        end_nav_gbp=decimal(report["end_nav_gbp"]),
        cash_flows_gbp=decimal(report.get("cash_flows_gbp", "0")),
        decisions=report.get("decisions", []),
        benchmark_returns=benchmarks,
    )
    print(f"wrote reconciled weekly post to {path}")


def command_killswitch(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    if args.action == "engage":
        settings.killswitch_path.write_text(
            "Order execution disabled locally. Revoke the API key in Trading 212 if compromised.\n",
            encoding="utf-8",
        )
        os.chmod(settings.killswitch_path, 0o600)
        print(f"kill switch engaged at {settings.killswitch_path}")
    else:
        if settings.killswitch_path.exists():
            settings.killswitch_path.unlink()
        print("kill switch disengaged locally; broker credentials were not changed")


def command_status(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    database = _database(settings)
    with database.connect() as connection:
        proposals = connection.execute(
            "SELECT status, COUNT(*) AS count FROM proposals GROUP BY status ORDER BY status"
        ).fetchall()
        snapshots = connection.execute(
            "SELECT COUNT(*) AS count FROM snapshots"
        ).fetchone()["count"]
    print(f"environment: {settings.t212_environment}")
    print(f"kill switch: {'ENGAGED' if settings.killswitch_path.exists() else 'clear'}")
    print(f"verified whitelist entries: {len(settings.load_whitelist())}")
    print(f"price snapshots: {snapshots}")
    for row in proposals:
        print(f"proposals {row['status']}: {row['count']}")


def command_doctor(args: argparse.Namespace) -> None:
    settings = _settings(_root(args.root))
    checks = {
        "database_parent_writable": os.access(settings.database_path.parent, os.W_OK),
        "whitelist_verified_nonempty": bool(settings.load_whitelist()),
        "persona_exists": (settings.root / "PERSONA.md").is_file(),
        "mandate_exists": (settings.root / "MANDATE.md").is_file(),
        "t212_credentials_present": bool(settings.t212_api_key and settings.t212_api_secret),
        "killswitch_clear": not settings.killswitch_path.exists(),
    }
    if settings.t212_environment == "live":
        try:
            assert_environment_allowed(settings, utc_now())
        except Exception:
            checks["live_gate"] = False
        else:
            checks["live_gate"] = True
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    if not all(checks.values()):
        raise RuntimeError("doctor found blocking setup failures")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="japan-agent")
    parser.add_argument("--root", help="project root (defaults to current directory)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="initialize local storage").set_defaults(func=command_init)
    subparsers.add_parser("identity", help="run the one-time Claude identity ritual").set_defaults(
        func=command_identity
    )

    ingest = subparsers.add_parser("ingest-prices", help="import normalized timestamped prices")
    ingest.add_argument("file")
    ingest.set_defaults(func=command_ingest_prices)

    collect = subparsers.add_parser(
        "collect-prices", help="collect daily closes for the verified whitelist via yfinance"
    )
    collect.add_argument("--symbols", help="ticker-to-data-symbol map (default config/data-symbols.json)")
    collect.set_defaults(func=command_collect_prices)

    jquants = subparsers.add_parser("ingest-jquants", help="ingest J-Quants v2 daily bars")
    jquants.add_argument("--code", required=True)
    jquants.add_argument("--date", required=True, help="YYYY-MM-DD; free plan is 12 weeks delayed")
    jquants.set_defaults(func=command_ingest_jquants)

    edinet = subparsers.add_parser("ingest-edinet", help="ingest EDINET v2 filing headlines")
    edinet.add_argument("--date", required=True, help="YYYY-MM-DD")
    edinet.set_defaults(func=command_ingest_edinet)

    digest = subparsers.add_parser(
        "ingest-digest", help="import a timestamped TDnet or cited-news digest"
    )
    digest.add_argument("--source", required=True, choices=["TDNET", "NEWS"])
    digest.add_argument("file")
    digest.set_defaults(func=command_ingest_digest)

    subparsers.add_parser("research", help="run Claude on a complete fresh snapshot").set_defaults(
        func=command_research
    )

    propose = subparsers.add_parser("propose", help="risk-check one structured research decision")
    propose.add_argument("--decision", required=True)
    propose.add_argument("--portfolio", required=True)
    propose.set_defaults(func=command_propose)

    decide = subparsers.add_parser("decide", help="local approval fallback for a fixed ticket")
    decide.add_argument("action", choices=["approve", "reject"])
    decide.add_argument("proposal_id")
    decide.add_argument("--approver", required=True)
    decide.add_argument("--reason", default="Rejected by the human account holder")
    decide.set_defaults(func=command_decide)

    execute = subparsers.add_parser("execute", help="submit one already-approved fixed ticket")
    execute.add_argument("proposal_id")
    execute.add_argument(
        "--portfolio", required=True, help="fresh reconciled Trading 212 portfolio JSON"
    )
    execute.set_defaults(func=command_execute)

    reconcile = subparsers.add_parser("reconcile", help="refresh a known broker order")
    reconcile.add_argument("proposal_id")
    reconcile.set_defaults(func=command_reconcile)

    discover = subparsers.add_parser("discover-instruments", help="read broker instruments")
    discover.add_argument("--term", action="append", default=[])
    discover.set_defaults(func=command_discover)

    send_ticket = subparsers.add_parser("send-ticket", help="send one pending Telegram ticket")
    send_ticket.add_argument("proposal_id")
    send_ticket.set_defaults(func=command_send_ticket)

    telegram_serve = subparsers.add_parser(
        "telegram-serve", help="serve the authenticated approval webhook"
    )
    telegram_serve.add_argument("--host", default="127.0.0.1")
    telegram_serve.add_argument("--port", type=int, default=8080)
    telegram_serve.set_defaults(func=command_telegram_serve)

    telegram_poll = subparsers.add_parser(
        "telegram-poll", help="long-poll Telegram approvals without exposing a webhook"
    )
    telegram_poll.add_argument("--timeout", type=int, default=50)
    telegram_poll.set_defaults(func=command_telegram_poll)

    subparsers.add_parser("journal-sync", help="rebuild the local JSONL event mirror").set_defaults(
        func=command_journal
    )
    weekly_post = subparsers.add_parser(
        "journal-weekly", help="render a reconciled public weekly Markdown post"
    )
    weekly_post.add_argument("--report", required=True)
    weekly_post.set_defaults(func=command_weekly_post)

    killswitch = subparsers.add_parser("killswitch", help="control the local execution interlock")
    killswitch.add_argument("action", choices=["engage", "disengage"])
    killswitch.set_defaults(func=command_killswitch)

    subparsers.add_parser("status", help="show local state").set_defaults(func=command_status)
    subparsers.add_parser("doctor", help="fail closed on incomplete setup").set_defaults(
        func=command_doctor
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

"""Command-line entry point.

    python -m src.main status                 # mode, kill switch, broker connectivity (read-only)
    python -m src.main download-data          # fetch historical data into data_cache/ (research)
    python -m src.main research               # full study -> reports/research/<date>/report.md
    python -m src.main backtest --strategy C  # single backtest with base settings
    python -m src.main cycle                  # daily paper/live cycle (run BEFORE the US open)
    python -m src.main protect                # place missing protective stops (run after the open)
    python -m src.main reconcile              # reconciliation only (no orders)
    python -m src.main kill on|off            # emergency kill switch
    python -m src.main reset-daily-limit      # clear the daily-loss-limit block
"""
from __future__ import annotations

import argparse  # CLI parsing
import json  # output
import sys  # exit codes
from datetime import datetime, timedelta, timezone  # time windows

from .config import PROJECT_ROOT, ConfigError, build_runtime_context, load_dotenv, load_settings  # settings
from .monitoring.logger import get_logger, log_event  # logging


def _broker(ctx, settings):
    """Construct the Trading 212 adapter for the resolved environment (demo unless LIVE double opt-in)."""
    from .broker.trading212_client import Trading212Broker
    return Trading212Broker(ctx.base_url, timeout_s=float(settings["broker"]["request_timeout_seconds"]))


def _repo():
    """Open the SQLite state database."""
    from .storage.database import Database
    from .storage.repositories import Repo
    return Repo(Database(PROJECT_ROOT / "data_cache" / "state.sqlite"))


def _provider(settings):
    """Market-data provider (yahoo by default; see data/market_data.py for limitations)."""
    from .data.market_data import YahooProvider
    return YahooProvider()


def cmd_status(settings) -> int:
    from .monitoring.health import health_check
    ctx = build_runtime_context(settings)
    print(json.dumps(health_check(ctx, _broker(ctx, settings)), indent=2, default=str))
    return 0


def cmd_download(settings, args) -> int:
    from .data.research_data import download_research_data
    download_research_data(settings, start=args.start, refresh=args.refresh)
    return 0


def cmd_research(settings, args) -> int:
    from .data.research_data import load_research_data
    from .reporting.research_report import run_research
    md = load_research_data(settings)
    strategies = args.strategies.split(",") if args.strategies else None
    summary = run_research(md, settings, PROJECT_ROOT / "reports" / "research", strategies=strategies)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_backtest(settings, args) -> int:
    from .backtest.engine import run_backtest
    from .backtest.metrics import summarize
    from .data.research_data import load_research_data
    from .reporting.research_report import FACTORIES
    md = load_research_data(settings)
    res = run_backtest(md, FACTORIES[args.strategy](settings), settings, start=args.start, end=args.end)
    print(json.dumps(summarize(res), indent=2, default=str))
    print(res.rejections.head(10).to_string())
    return 0


def cmd_cycle(settings) -> int:
    from .execution.execution_engine import run_cycle
    from .reporting.research_report import FACTORIES
    ctx = build_runtime_context(settings)
    primary = settings.get("strategy", {}).get("primary", "C")
    summary = run_cycle(ctx, settings, _broker(ctx, settings), _provider(settings), _repo(), FACTORIES[primary])
    print(json.dumps(summary, indent=2, default=str))
    return 1 if summary.get("aborted") else 0


def cmd_protect(settings) -> int:
    from .execution.execution_engine import protect
    from .execution.order_manager import OrderManager
    ctx = build_runtime_context(settings)
    b, repo = _broker(ctx, settings), _repo()
    om = OrderManager(b, repo, dry_run=ctx.dry_run, kill_switch=ctx.kill_switch)
    print(json.dumps(protect(b, repo, om, str(datetime.now(timezone.utc).date())), indent=2))
    return 0


def cmd_reconcile(settings) -> int:
    from .backtest.costs import CostModel
    from .data.universe import broker_tradable_map
    from .execution.order_manager import OrderManager
    from .execution.reconciliation import reconcile
    ctx = build_runtime_context(settings)
    b, repo = _broker(ctx, settings), _repo()
    om = OrderManager(b, repo, dry_run=True, kill_switch=True)  # reconciliation never sends orders
    tradable = broker_tradable_map(b.instruments())
    rep = reconcile(repo, om, b.positions(), b.pending_orders(),
                    b.order_history(since=datetime.now(timezone.utc) - timedelta(days=10)), 1.3, settings,
                    CostModel.from_settings(settings), {i.broker_ticker: s for s, i in tradable.items()})
    print(json.dumps(rep.__dict__, indent=2, default=str))
    return 1 if rep.halt_new_orders else 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # local .env (never overrides real environment variables)
    get_logger(PROJECT_ROOT / "logs")  # structured logs to logs/bot.jsonl
    ap = argparse.ArgumentParser(prog="stocktradebot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    d = sub.add_parser("download-data")
    d.add_argument("--start", default="1998-01-01")
    d.add_argument("--refresh", action="store_true")
    r = sub.add_parser("research")
    r.add_argument("--strategies", default="")
    b = sub.add_parser("backtest")
    b.add_argument("--strategy", default="C")
    b.add_argument("--start", default=None)
    b.add_argument("--end", default=None)
    sub.add_parser("cycle")
    sub.add_parser("protect")
    sub.add_parser("reconcile")
    k = sub.add_parser("kill")
    k.add_argument("state", choices=["on", "off"])
    sub.add_parser("reset-daily-limit")
    args = ap.parse_args(argv)
    try:
        settings = load_settings()
        if args.cmd == "status":
            return cmd_status(settings)
        if args.cmd == "download-data":
            return cmd_download(settings, args)
        if args.cmd == "research":
            return cmd_research(settings, args)
        if args.cmd == "backtest":
            return cmd_backtest(settings, args)
        if args.cmd == "cycle":
            return cmd_cycle(settings)
        if args.cmd == "protect":
            return cmd_protect(settings)
        if args.cmd == "reconcile":
            return cmd_reconcile(settings)
        if args.cmd == "kill":
            from .monitoring import kill_switch
            kill_switch.activate("cli") if args.state == "on" else kill_switch.deactivate()
            print(f"kill switch {'ON' if kill_switch.is_active() else 'OFF'}")
            return 0
        if args.cmd == "reset-daily-limit":
            _repo().set_state("daily_limit_blocked", None)
            log_event("DAILY_LIMIT_REACHED", "daily loss limit reset by operator")
            print("daily loss limit reset")
            return 0
    except ConfigError as exc:  # unsafe configuration: refuse
        log_event("BOT_STOPPED", f"configuration error: {exc}")
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

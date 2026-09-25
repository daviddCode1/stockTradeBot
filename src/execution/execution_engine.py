"""Daily paper/live cycle. Same strategy, risk and portfolio code as the backtest; only the
execution destination differs (demo vs live Trading 212, or DRY_RUN = nowhere).

Run schedule (see README):
  * `cycle`   - once per trading day BEFORE the US open (e.g. 13:00 UK). Trading 212 DAY orders
                expire at midnight New York time, so orders placed the evening before would
                expire before the next session. Decisions use the last COMPLETED session's data.
  * `protect` - shortly after the open and again mid-session: places the resting GTC stop for
                any newly filled entry, and re-places missing stops.
Any doubt (stale data, broker error, unresolved order, limits) => NO new trades.
"""
from __future__ import annotations

import json  # reports
import math  # finite checks
from dataclasses import asdict  # serialise plans
from datetime import datetime, timedelta, timezone  # timestamps
from pathlib import Path  # reports / caches
from typing import Any, Mapping  # type hints

import pandas as pd  # data

from ..backtest.costs import CostModel  # costs
from ..broker.interface import Broker, BrokerError  # broker contract
from ..broker.models import OrderRequest  # orders
from ..config import PROJECT_ROOT, RuntimeContext  # settings
from ..data import calendar as cal  # trading calendar
from ..data.market_data import MarketDataProvider, build_market_data  # data layer
from ..data.universe import broker_tradable_map, load_membership_table  # universe
from ..monitoring.logger import log_event  # structured logs
from ..portfolio.constructor import DecisionContext, decide  # shared decision logic
from ..portfolio.exposure import portfolio_heat_gbp  # heat
from ..risk.risk_manager import validate_entry  # final pre-submission gate
from ..storage.repositories import Repo  # persistence
from ..strategy.interface import compute_features  # features
from .order_manager import OrderManager  # order protocol
from .reconciliation import reconcile  # startup reconciliation

CACHE = PROJECT_ROOT / "data_cache"  # local caches (git-ignored)


class CycleAbort(RuntimeError):
    """Raised to stop a cycle safely (no new orders)."""


def verify_account(broker: Broker, ctx: RuntimeContext) -> Any:
    """Account checks: reachable, expected id (if pinned), expected currency. Fail safe otherwise."""
    acct = broker.account_summary()  # raises BrokerError on failure
    if ctx.expected_account_id and acct.account_id != ctx.expected_account_id:
        raise CycleAbort("account id does not match T212_EXPECTED_ACCOUNT_ID - refusing to trade")
    if acct.currency != ctx.base_currency:
        raise CycleAbort(f"account currency {acct.currency} != expected {ctx.base_currency}")
    # The Public API only serves Invest and Stocks ISA accounts; ACCOUNT_TYPE was validated in config.
    return acct


def load_live_market_data(provider: MarketDataProvider, symbols: list[str], cfg: Mapping[str, Any],
                          lookback_days: int = 650) -> Any:
    """Download recent daily bars for the candidate universe + benchmark + FX, with sector/earnings caches."""
    start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()  # ~430 sessions
    bench = cfg["benchmark"]["symbol"]
    probe = provider.daily_bars([bench], start=start)  # fast connectivity check before the full download
    if probe.get("close") is None or probe["close"].empty:
        raise CycleAbort(f"market data provider '{provider.name}' unreachable (benchmark {bench} has no data)")
    panels = provider.daily_bars(sorted(set(symbols) | {bench}), start=start)  # vendor call
    if panels.get("close") is None or panels["close"].empty or bench not in panels["close"].columns:
        raise CycleAbort("market data unavailable")
    fx = provider.fx_usd_per_gbp(start=start)
    CACHE.mkdir(exist_ok=True)
    sec_path, earn_path = CACHE / "sectors.json", CACHE / "earnings.json"
    sectors = json.loads(sec_path.read_text()) if sec_path.exists() else {}
    missing = [s for s in symbols if s not in sectors]
    if missing:
        sectors.update(provider.sectors(missing))
        sec_path.write_text(json.dumps(sectors))
    earnings_raw = json.loads(earn_path.read_text()) if earn_path.exists() else {}
    fresh_cut = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    stale = [s for s in symbols if earnings_raw.get(s, {}).get("ts", "") < fresh_cut]
    if stale:
        got = provider.earnings_dates(stale)
        for s in stale:
            earnings_raw[s] = {"ts": datetime.now(timezone.utc).isoformat(), "dates": [str(d.date()) for d in got.get(s, [])]}
        earn_path.write_text(json.dumps(earnings_raw))
    earnings = {s: [pd.Timestamp(d) for d in v["dates"]] for s, v in earnings_raw.items() if v.get("dates")}
    return build_market_data(panels, bench, fx, None, sectors, earnings, notes=[f"provider={provider.name}"])


def _strategy_equity(acct: Any, managed_value_gbp: float, cfg: Mapping[str, Any]) -> float:
    """Capital the bot manages: free cash + its own positions (manual positions excluded), optionally capped."""
    eq = acct.cash_available + managed_value_gbp  # never counts manual holdings
    cap = cfg["account"].get("strategy_capital_gbp")
    return min(eq, float(cap)) if cap else eq


def protect(broker: Broker, repo: Repo, om: OrderManager, session: str) -> list[str]:
    """Ensure every managed position has a resting GTC stop. Exit at market if already below the stop."""
    actions = []
    pending = {o.order_id: o for o in broker.pending_orders()}
    live_pos = {p.broker_ticker: p for p in broker.positions()}
    for t, p in repo.open_positions().items():
        sid = p.extra.get("stop_order_id")
        if sid and sid in pending:
            continue  # protected
        bp = live_pos.get(p.broker_ticker)
        if bp is None:
            continue  # reconciliation will explain it
        qty = min(p.qty, bp.quantity_available)  # shares free to be sold
        if qty <= 0:
            continue
        if bp.current_price and bp.current_price <= p.stop:  # already through the stop: exit now
            o = om.submit(session=session, ticker=t, req=OrderRequest(p.broker_ticker, "SELL", qty, "MARKET"),
                          purpose="EXIT", protective=True, meta={"reason": "STOP_BREACHED"})
            actions.append(f"{t}: below stop -> market exit {'sent' if o else 'not sent'}")
            continue
        o = om.submit(session=session, ticker=t, protective=True, purpose="STOP", meta={"reason": "STOP"},
                      req=OrderRequest(p.broker_ticker, "SELL", qty, "STOP", stop_price=round(p.stop, 2),
                                       time_validity="GOOD_TILL_CANCEL"))
        if o:
            repo.set_stop_order(t, o.order_id)
            log_event("STOP_PLACED", "protective stop resting at broker", ticker=t, stop=p.stop, qty=qty)
            actions.append(f"{t}: stop placed @ {p.stop}")
    return actions


def run_cycle(ctx: RuntimeContext, cfg: Mapping[str, Any], broker: Broker, provider: MarketDataProvider,
              repo: Repo, strategy_factory, now: datetime | None = None) -> dict:
    """One full daily cycle (steps 1-24 of the specification). Returns a summary dict (also saved)."""
    now = now or datetime.now(timezone.utc)
    summary: dict[str, Any] = {"mode": ctx.mode, "dry_run": ctx.dry_run, "ts": now.isoformat(), "orders": []}
    log_event("BOT_STARTED", "daily cycle start", mode=ctx.mode, dry_run=ctx.dry_run, kill=ctx.kill_switch)
    costs = CostModel.from_settings(cfg)
    om = OrderManager(broker, repo, dry_run=ctx.dry_run, kill_switch=ctx.kill_switch)
    try:
        acct = verify_account(broker, ctx)  # steps 2-3
        session = cal.last_completed_session(now)  # step 4: data session we trade off
        s_key = str(session.date())
        summary["session"] = s_key
        if cal.market_is_open(now):
            summary["note"] = "market open: entries still placed as DAY limits for today"
        positions = broker.positions()  # step 5
        pending = broker.pending_orders()
        history = broker.order_history(since=now - timedelta(days=10))
        instruments = broker.instruments()  # universe (broker side)
        tradable = broker_tradable_map(instruments, cfg["universe"]["allowed_security_types"], cfg["universe"]["instrument_currency"])
        bt_to_sym = {ins.broker_ticker: sym for sym, ins in tradable.items()}
        fx_guess = 1.3  # placeholder until FX is loaded; only used if a fill lacks fxRate
        rep = reconcile(repo, om, positions, pending, history, fx_guess, cfg, costs, bt_to_sym)  # step 6
        summary["reconciliation"] = {"halt": rep.halt_new_orders, "opened": rep.opened, "closed": rep.closed,
                                     "unmanaged_positions": len(rep.unexpected_positions), "notes": rep.notes}
        summary["protect"] = protect(broker, repo, om, s_key)  # stops for any new fills

        # steps 7-9: market data for current index members that Trading 212 can trade + managed names
        members = load_membership_table(CACHE / "sp500_membership.csv")
        current = set(members[members["end_date"].isna()]["ticker"])
        managed = repo.open_positions()
        symbols = sorted((current & set(tradable)) | set(managed))
        repo.save_universe(s_key, {s: tradable[s].broker_ticker for s in symbols if s in tradable})
        log_event("UNIVERSE_UPDATED", "tradable universe", n=len(symbols))
        md = load_live_market_data(provider, symbols, cfg)
        if md.dates[-1].normalize() != session.normalize():  # stale data => no trading
            raise CycleAbort(f"stale market data: last bar {md.dates[-1].date()} != session {s_key}")
        i = len(md.dates) - 1

        # steps 10-16: indicators, momentum, RS, trend, regime, volatility, ranking
        f = compute_features(md, cfg)
        strat = strategy_factory(cfg)
        ent = strat.entries(md, f)

        # step 17-20: portfolio + risk
        fx = float(md.fx_usd_per_gbp.iloc[i])
        prices = {t: float(md.close[t].iloc[i]) for t in managed if t in md.close.columns}
        managed_val = sum(p.qty * prices.get(t, p.entry_px) / fx for t, p in managed.items())
        equity = _strategy_equity(acct, managed_val, cfg)
        prev = repo.last_daily_pnl(s_key)
        blocked, why = ctx.kill_switch, "kill_switch" if ctx.kill_switch else ""
        if prev and prev["equity_gbp"] and equity / prev["equity_gbp"] - 1 <= -float(cfg["risk"]["max_daily_loss"]):
            repo.set_state("daily_limit_blocked", s_key)
            log_event("DAILY_LIMIT_REACHED", "daily loss limit hit - no new buys", equity=equity, prev=prev["equity_gbp"])
        if repo.get_state("daily_limit_blocked") and (repo.get_state("daily_limit_blocked") == s_key
                                                      or cfg["risk"].get("daily_loss_requires_reset")):
            blocked, why = True, "daily_loss_limit"
        if rep.halt_new_orders:
            blocked, why = True, "reconciliation_needs_review"
        dctx = DecisionContext(equity_gbp=equity, cash_gbp=acct.cash_available, positions=managed,
                               new_buys_blocked=blocked, block_reason=why,
                               quantity_step=float(cfg["broker"]["quantity_step"]),
                               max_open_qty={s: tradable[s].max_open_quantity for s in symbols if s in tradable},
                               tradable=set(tradable), live_mode=True,
                               is_month_end=cal.next_session(session).month != session.month,
                               is_week_end=cal.next_session(session).isocalendar()[1] != session.isocalendar()[1])
        plan = decide(i, md, f, ent, strat, dctx, cfg, costs)
        heat = portfolio_heat_gbp(managed.values(), prices, fx, costs)
        repo.save_daily_pnl(s_key, equity, acct.cash_available, managed_val, heat, len(managed))
        repo.snapshot(ctx.mode, acct.account_id[-4:], acct.currency, acct.total_value, acct.cash_available,
                      acct.invested_value, equity)
        rows = [(t, float(f.rank.iloc[i].get(t, math.nan)), float(f.signal.iloc[i].get(t, math.nan)), "REJECT", r)
                for t, r in plan.rejections]
        rows += [(e.ticker, e.rank, float(f.signal.iloc[i].get(e.ticker, math.nan)), "ENTRY", "") for e in plan.entries]
        repo.save_signals(s_key, rows)
        for t, r in plan.rejections:
            log_event("SIGNAL_REJECTED", "candidate rejected", ticker=t, reason=r)

        # steps 21-22: orders (exits first, then stop raises, then entries)
        min_val = float(cfg["broker"].get("min_order_value_gbp", 1.0))
        for x in plan.exits:
            p = managed[x.ticker]
            sid = p.extra.get("stop_order_id")
            if sid and not om.cancel_and_verify(sid, x.ticker):  # shares are reserved by the stop
                log_event("TRADE_REJECTED", "exit skipped: stop cancel not verified", ticker=x.ticker)
                continue
            repo.set_stop_order(x.ticker, None)
            o = om.submit(session=s_key, ticker=x.ticker, purpose="EXIT", protective=True, meta={"reason": x.reason},
                          req=OrderRequest(p.broker_ticker, "SELL", x.qty, "MARKET"))
            if x.reason == "TARGET":
                log_event("TARGET_REACHED", "target reached at close - selling at open", ticker=x.ticker)
            summary["orders"].append({"exit": x.ticker, "reason": x.reason, "sent": bool(o)})
        for t, new_stop in plan.stop_updates:  # trailing stops: cancel + replace (only upward)
            p = managed[t]
            sid = p.extra.get("stop_order_id")
            if sid and not om.cancel_and_verify(sid, t):
                continue
            o = om.submit(session=s_key, ticker=t, purpose="STOP", protective=True, meta={"reason": "STOP"},
                          req=OrderRequest(p.broker_ticker, "SELL", p.qty, "STOP", stop_price=new_stop,
                                           time_validity="GOOD_TILL_CANCEL"))
            repo.set_stop_order(t, o.order_id if o else None, stop=new_stop)
        for e in plan.entries:
            ins = tradable.get(e.ticker)
            if ins is None or e.qty * e.limit_px / fx < min_val:
                log_event("TRADE_REJECTED", "not tradable or below minimum order value", ticker=e.ticker)
                continue
            ok, why_not = validate_entry(e, equity, fx, cfg, costs)  # defence in depth
            log_event("RISK_CHECK", "pre-submission risk gate", ticker=e.ticker, ok=ok, reason=why_not)
            if not ok:
                log_event("TRADE_REJECTED", "failed final risk gate", ticker=e.ticker, reason=why_not)
                continue
            log_event("POSITION_SIZED", "entry sized", ticker=e.ticker, qty=e.qty, limit=e.limit_px, stop=e.stop_px,
                      risk_gbp=round(e.risk_gbp, 2), equity=round(equity, 2))
            repo.risk_log(e.ticker, **asdict(e))
            o = om.submit(session=s_key, ticker=e.ticker, purpose="ENTRY",
                          req=OrderRequest(ins.broker_ticker, "BUY", e.qty, "LIMIT", limit_price=e.limit_px,
                                           time_validity="DAY"),
                          meta={"stop": e.stop_px, "target_r": e.target_r, "sector": e.sector, "risk_gbp": e.risk_gbp})
            summary["orders"].append({"entry": e.ticker, "qty": e.qty, "limit": e.limit_px, "stop": e.stop_px,
                                      "risk_gbp": round(e.risk_gbp, 2), "sent": bool(o)})
        summary.update({"equity_gbp": round(equity, 2), "cash_gbp": round(acct.cash_available, 2),
                        "managed_positions": len(managed), "heat_pct": round(heat / equity * 100, 2) if equity else None,
                        "regime_bull": bool(f.bull.iloc[i]), "blocked": blocked, "block_reason": why,
                        "entries": len(plan.entries), "exits": len(plan.exits), "notes": plan.notes,
                        "top_rejections": plan.rejections[:15]})
    except (CycleAbort, BrokerError) as exc:  # fail safe: no new trades
        log_event("API_ERROR" if isinstance(exc, BrokerError) else "DATA_ERROR", f"cycle aborted: {exc}")
        repo.event("DATA_ERROR", None, error=str(exc))
        summary["aborted"] = str(exc)
    finally:
        log_event("BOT_STOPPED", "daily cycle end")
        out = PROJECT_ROOT / "reports" / "daily"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{now.date()}_{ctx.mode.lower()}.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary

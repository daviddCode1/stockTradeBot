"""Event-driven daily backtester.

Timeline for each session i (no look-ahead: decisions use data <= close of i, fills use i+1):
  OPEN(i)      execute exits and entries decided at close(i-1)
                 - market sells fill at open*(1-slippage)
                 - buy limits: open<=L -> fill min(L, open*(1+slip)); else low<=L -> fill L; else expire
  INTRADAY(i)  resting stops: open<=stop -> fill open*(1-stop_slip) (gap); low<=stop -> stop*(1-stop_slip)
  DIVIDENDS(i) cash dividends (net of withholding) for shares held at the previous close
  CLOSE(i)     mark to market in GBP, daily loss limit, then `decide` for session i+1
Costs: FX fee both legs, slippage, SEC/FINRA on sells (see costs.py). Cash never goes negative.
"""
from __future__ import annotations

import math  # finite checks
from dataclasses import dataclass, field  # result container
from typing import Any, Mapping  # type hints

import numpy as np  # arrays
import pandas as pd  # results

from ..data.market_data import MarketData  # input data
from ..portfolio.constructor import DecisionContext, decide  # shared decision logic
from ..portfolio.exposure import EntryPlan, ExitPlan, HeldPosition, market_value_gbp, portfolio_heat_gbp  # records
from ..risk.daily_limits import DailyLossGuard  # -1.5% day guard
from ..risk.position_sizing import floor_to_step  # rounding
from ..risk.stops import net_risk_per_share_usd, target_price  # stop/target maths
from ..strategy.interface import Features, Strategy, compute_features  # signals
from .costs import CostModel  # costs


@dataclass
class BacktestResult:
    """Everything a report needs from one backtest run."""

    equity: pd.Series  # daily equity (GBP)
    cash: pd.Series  # daily cash (GBP)
    exposure: pd.Series  # invested value / equity
    heat: pd.Series  # open risk / equity
    positions_count: pd.Series  # number of open positions
    trades: pd.DataFrame  # one row per closed trade (or partial)
    rejections: pd.Series  # count of rejection reasons
    params: dict = field(default_factory=dict)  # settings used
    notes: list = field(default_factory=list)  # data / methodology notes


def run_backtest(md: MarketData, strategy: Strategy, cfg: Mapping[str, Any], costs: CostModel | None = None,
                 start: str | None = None, end: str | None = None, features: Features | None = None,
                 entries_panel: pd.DataFrame | None = None) -> BacktestResult:
    """Simulate the strategy over [start, end] and return equity, trades and diagnostics."""
    costs = costs or CostModel.from_settings(cfg)  # base-case costs unless stressed
    f = features or compute_features(md, cfg)  # indicator panels (can be shared across runs)
    ent = entries_panel if entries_panel is not None else strategy.entries(md, f)  # entry signals
    dates = md.dates
    i0 = int(dates.searchsorted(pd.Timestamp(start))) if start else 0  # first simulated session
    i1 = int(dates.searchsorted(pd.Timestamp(end), side="right")) - 1 if end else len(dates) - 1  # last session
    cols = {t: k for k, t in enumerate(md.close.columns)}  # ticker -> column index
    O, H, L, C = (x.to_numpy(dtype=float) for x in (md.open, md.high, md.low, md.close))  # fast arrays
    DIV = md.dividends.to_numpy(dtype=float)  # dividends per share
    FX = md.fx_usd_per_gbp.to_numpy(dtype=float)  # USD per GBP
    step = float(cfg["broker"]["quantity_step"])  # broker quantity increment (fractional shares => e.g. 0.01)

    cash = float(cfg["backtest"]["initial_equity_gbp"])  # starting cash
    positions: dict[str, HeldPosition] = {}  # open positions
    pend_entries: list[EntryPlan] = []  # to execute at next open
    pend_exits: list[ExitPlan] = []  # to execute at next open
    pend_stops: list[tuple[str, float]] = []  # stop raises effective next session
    guard = DailyLossGuard(float(cfg["risk"]["max_daily_loss"]), False)  # auto-clears next day in backtest
    prev_equity = cash
    trades: list[dict] = []  # closed trades
    rej: dict[str, int] = {}  # rejection counters
    eq_s, cash_s, expo_s, heat_s, npos_s = {}, {}, {}, {}, {}  # daily series

    def close_pos(t: str, qty: float, px: float, day: pd.Timestamp, fx: float, reason: str) -> None:
        """Sell qty of t at px (already slipped), book cash and the trade record."""
        nonlocal cash
        p = positions[t]
        qty = min(qty, p.qty)  # never sell more than held
        proceeds = costs.sell_proceeds_gbp(qty, px, fx)  # GBP after fees
        frac = qty / p.qty  # share of the position being closed
        basis = p.cost_basis_gbp * frac  # allocated GBP cost
        r_base = p.initial_risk_gbp * frac  # allocated 1R
        cash += proceeds
        trades.append({
            "ticker": t, "entry_date": p.entry_date, "exit_date": day, "qty": qty, "entry_px": p.entry_px,
            "exit_px": px, "pnl_gbp": proceeds - basis, "r": (proceeds - basis) / r_base if r_base > 0 else np.nan,
            "reason": reason, "days": int(dates.searchsorted(day) - dates.searchsorted(p.entry_date)),
            "sector": p.sector, "initial_risk_gbp": r_base, "cost_gbp": basis,
        })
        if qty >= p.qty - 1e-12:
            del positions[t]  # fully closed
        else:
            p.qty -= qty  # partial: keep the rest
            p.cost_basis_gbp -= basis
            p.initial_risk_gbp -= r_base

    for i in range(i0, i1 + 1):
        day, fx = dates[i], FX[i]
        held_at_prev_close = {t: p.qty for t, p in positions.items()}  # dividend eligibility

        # ---- OPEN: exits decided yesterday
        still_pending: list[ExitPlan] = []
        for x in pend_exits:
            if x.ticker not in positions:
                continue
            o = O[i, cols[x.ticker]]
            if not math.isfinite(o):  # no trading today (halt / missing bar): try again tomorrow
                still_pending.append(x); continue
            close_pos(x.ticker, x.qty, o * (1 - costs.slippage), day, fx, x.reason)
            if x.new_stop_after is not None and x.ticker in positions:  # partial: protect the remainder
                p = positions[x.ticker]
                p.stop, p.partial_done = max(p.stop, x.new_stop_after), True
        pend_exits = still_pending
        for t, s in pend_stops:  # stop raises (never lowered)
            if t in positions:
                positions[t].stop = max(positions[t].stop, s)
        pend_stops = []

        # ---- OPEN: entries decided yesterday (limit orders, DAY validity)
        for e in pend_entries:
            c = cols[e.ticker]
            o, lo = O[i, c], L[i, c]
            if not math.isfinite(o):
                continue  # DAY order expires unfilled
            if o <= e.limit_px:
                px = min(e.limit_px, o * (1 + costs.slippage))  # marketable at the open
            elif math.isfinite(lo) and lo <= e.limit_px:
                px = e.limit_px  # filled intraday at the limit
            else:
                continue  # gapped above the limit: no trade (no chasing)
            qty = e.qty
            cost = costs.buy_cost_gbp(qty, px, fx)
            if cost > cash:  # FX moved or cash changed: shrink, never borrow
                qty = floor_to_step(cash / (px * (1 + costs.fx_fee) / fx), step)
                cost = costs.buy_cost_gbp(qty, px, fx)
            if qty <= 0:
                continue
            cash -= cost
            risk_ps = net_risk_per_share_usd(px, e.stop_px, costs) / fx  # GBP per share at the stop
            tgt = target_price(px, e.stop_px, e.target_r, costs) if e.stop_active else float("inf")
            positions[e.ticker] = HeldPosition(
                ticker=e.ticker, qty=qty, entry_px=px, entry_date=day, stop=e.stop_px, initial_stop=e.stop_px,
                target=tgt, sector=e.sector, initial_risk_gbp=qty * risk_ps, highest_close=px,
                stop_active=e.stop_active, cost_basis_gbp=cost,
            )
        pend_entries = []

        # ---- INTRADAY: resting stop orders
        for t in list(positions):
            p = positions[t]
            if not p.stop_active:
                continue
            c = cols[t]
            o, lo = O[i, c], L[i, c]
            if math.isfinite(o) and o <= p.stop:
                close_pos(t, p.qty, o * (1 - costs.stop_slippage), day, fx, "STOP_GAP")  # gapped through the stop
            elif math.isfinite(lo) and lo <= p.stop:
                close_pos(t, p.qty, p.stop * (1 - costs.stop_slippage), day, fx, "STOP")

        # ---- DIVIDENDS: shares held at the previous close receive the ex-date dividend
        for t, q in held_at_prev_close.items():
            d = DIV[i, cols[t]]
            if math.isfinite(d) and d > 0:
                cash += q * d * (1 - costs.dividend_withholding) / fx  # net of 15% US withholding, into GBP

        # ---- CLOSE: mark to market
        prices = {t: C[i, cols[t]] for t in positions}
        for t, p in positions.items():
            if math.isfinite(prices[t]):
                p.highest_close = max(p.highest_close, prices[t])  # for trailing stops
        mv = market_value_gbp(positions.values(), prices, fx)
        equity = cash + mv
        guard.update(equity, prev_equity)  # daily loss limit
        prev_equity = equity
        eq_s[day], cash_s[day] = equity, cash
        expo_s[day] = mv / equity if equity > 0 else 0.0
        heat_s[day] = portfolio_heat_gbp([p for p in positions.values() if p.stop_active], prices, fx, costs) / equity
        npos_s[day] = len(positions)
        if i == i1:
            break  # no decisions after the last simulated day

        # ---- DECIDE for session i+1 (shared logic with live trading)
        ctx = DecisionContext(equity_gbp=equity, cash_gbp=cash, positions=positions,
                              new_buys_blocked=guard.blocked, block_reason="daily_loss_limit",
                              quantity_step=step)
        plan = decide(i, md, f, ent, strategy, ctx, cfg, costs)
        pend_exits = pend_exits + plan.exits
        pend_entries = plan.entries
        pend_stops = plan.stop_updates
        for _, reason in plan.rejections:
            rej[reason] = rej.get(reason, 0) + 1

    # close anything still open at the end at the last close (marked, not a signal)
    last = dates[i1]
    for t in list(positions):
        px = C[i1, cols[t]]
        if math.isfinite(px):
            close_pos(t, positions[t].qty, px * (1 - costs.slippage), last, FX[i1], "END_OF_TEST")
    tr = pd.DataFrame(trades)
    return BacktestResult(
        equity=pd.Series(eq_s, name="equity"), cash=pd.Series(cash_s), exposure=pd.Series(expo_s),
        heat=pd.Series(heat_s), positions_count=pd.Series(npos_s), trades=tr,
        rejections=pd.Series(rej, dtype=float).sort_values(ascending=False),
        params={"strategy": strategy.name}, notes=list(md.notes),
    )

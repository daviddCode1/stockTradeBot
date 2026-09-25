"""The shared decision function: given data up to the close of day i and the current
portfolio, decide exits, stop updates and new entries for the next session.

This is the ONLY place trading decisions are made. The backtester and the live/paper
runner both call `decide`, so paper trading exercises exactly the logic that was tested.
"""
from __future__ import annotations

import math  # finite checks
from dataclasses import dataclass  # state container
from typing import Any, Mapping  # type hints

import numpy as np  # arrays
import pandas as pd  # panels

from ..backtest.costs import CostModel  # cost assumptions
from ..data.earnings import next_earnings_within  # earnings blackout
from ..data.market_data import MarketData  # input data
from ..risk.position_sizing import size_position  # risk-based sizing
from ..risk.stops import initial_stop, planned_rr, stop_is_valid, target_price, trail_stop  # stop rules
from ..strategy.interface import Features, Strategy  # signals
from .correlation import correlation_with_holdings, passes_correlation_gate  # correlation gate
from .exposure import EntryPlan, ExitPlan, HeldPosition, Plan, portfolio_heat_gbp  # records + heat
from .sector_limits import sector_allows, sector_of  # sector cap


@dataclass
class DecisionContext:
    """Portfolio facts needed to decide (all money in GBP)."""

    equity_gbp: float  # strategy equity (cash + managed positions)
    cash_gbp: float  # free cash available for new buys
    positions: dict[str, HeldPosition]  # managed positions by ticker
    new_buys_blocked: bool = False  # daily loss limit or kill switch
    block_reason: str = ""  # why buys are blocked
    quantity_step: float = 1.0  # broker quantity increment
    max_open_qty: dict[str, float] | None = None  # broker caps per ticker (live)
    tradable: set[str] | None = None  # tickers the broker can trade (live); None = all
    live_mode: bool = False  # live/paper: unknown earnings => ineligible
    is_month_end: bool | None = None  # live: from the exchange calendar (data ends at the decision day)
    is_week_end: bool | None = None  # live: from the exchange calendar


def _is_month_end(dates: pd.DatetimeIndex, i: int) -> bool:
    """True on the last trading day of a month (or the last available bar)."""
    return i == len(dates) - 1 or dates[i].month != dates[i + 1].month


class _View:
    """Numpy views of the panels used by `decide` (pandas row lookups are ~100x slower)."""

    def __init__(self, md: MarketData, f: Features, ent: pd.DataFrame):
        self.cols = list(md.close.columns)  # ticker order
        self.col = {t: k for k, t in enumerate(self.cols)}  # ticker -> column index
        self.C = md.close.to_numpy(dtype=float)  # closes
        self.LOW = md.low.to_numpy(dtype=float)  # lows (swing-low stops)
        self.R = f.rank.to_numpy(dtype=float)  # momentum ranks
        self.A = f.atr.to_numpy(dtype=float)  # ATR
        self.AP = f.atr_pct.to_numpy(dtype=float)  # ATR %
        self.S50 = f.sma50.to_numpy(dtype=float)  # SMA50
        self.LR = f.log_ret.to_numpy(dtype=float)  # daily log returns
        self.E = ent.reindex(columns=self.cols).fillna(False).to_numpy(dtype=bool)  # entry signals
        self.bull = f.bull.to_numpy(dtype=bool)  # regime


_VIEW_CACHE: dict[tuple, _View] = {}  # keyed by object identity; small, cleared when full


def _view(md: MarketData, f: Features, ent: pd.DataFrame) -> _View:
    """Build (once per backtest) or reuse the numpy view."""
    key = (id(md), id(f), id(ent), md.close.shape, ent.shape)
    v = _VIEW_CACHE.get(key)
    if v is None:
        if len(_VIEW_CACHE) > 8:
            _VIEW_CACHE.clear()  # bounded memory
        v = _VIEW_CACHE[key] = _View(md, f, ent)
    return v


def decide(i: int, md: MarketData, f: Features, entries_panel: pd.DataFrame, strategy: Strategy,
           ctx: DecisionContext, cfg: Mapping[str, Any], costs: CostModel) -> Plan:
    """Produce the plan for session i+1 using only information available at the close of session i."""
    plan = Plan()  # output container
    dates = md.dates  # calendar
    day = dates[i]  # decision date
    fx = float(md.fx_usd_per_gbp.iloc[i])  # USD per GBP at today's close
    v = _view(md, f, entries_panel)  # fast numpy access
    col = v.col  # ticker -> column
    def close_of(t: str) -> float:  # today's close (NaN if unknown ticker)
        return float(v.C[i, col[t]]) if t in col else float("nan")
    ex, st, tg, rk = cfg["exits"], cfg["stops"], cfg["targets"], cfg["risk"]  # config sections
    addons = set(ex.get("addons") or [])  # optional exit rules being tested
    bull = bool(v.bull[i])  # market regime today
    month_end = ctx.is_month_end if ctx.is_month_end is not None else _is_month_end(dates, i)  # S0 rebalance day?

    # ------------------------------------------------------------------ 1. exits / stop management
    exiting: set[str] = set()  # tickers we will sell
    for t, p in ctx.positions.items():
        px = close_of(t)  # today's close
        if not math.isfinite(px):  # stale or missing price => do nothing (stop stays at broker)
            plan.notes.append(f"{t}: no price today, position left unchanged")
            continue
        held_days = int(dates.searchsorted(day) - dates.searchsorted(p.entry_date))  # sessions held
        rank = float(v.R[i, col[t]])  # current momentum rank
        if strategy.classic:  # S0: only rank-based monthly rebalance
            if month_end and not (rank >= cfg["signals"]["exit_rank_below"]):  # weak or unranked
                plan.exits.append(ExitPlan(t, p.qty, "CLASSIC_REBALANCE")); exiting.add(t)
            continue
        reason = None  # first matching exit rule wins
        if "PARTIAL" in addons and not p.partial_done and px >= p.target:  # take partial profit
            part = math.floor(p.qty * float(ex["partial_fraction"]) / ctx.quantity_step) * ctx.quantity_step
            if part > 0:
                plan.exits.append(ExitPlan(t, round(part, 10), "PARTIAL", new_stop_after=max(p.stop, p.entry_px)))
                continue  # the remainder is managed by the trailing stop
        if "TRAILING" not in addons and "PARTIAL" not in addons and px >= p.target:
            reason = "TARGET"  # fixed-R target reached at the close
        elif held_days >= int(ex["max_holding_days"]):
            reason = "TIME"  # maximum holding period
        elif "TREND" in addons and px < float(v.S50[i, col[t]]):
            reason = "TREND"  # trend deterioration
        elif "MOMENTUM" in addons and math.isfinite(rank) and rank < cfg["signals"]["exit_rank_below"]:
            reason = "MOMENTUM"  # momentum deterioration
        elif "REGIME" in addons and not bull:
            reason = "REGIME"  # market regime turned bearish
        if reason:
            plan.exits.append(ExitPlan(t, p.qty, reason)); exiting.add(t)
            continue
        if "TRAILING" in addons or ("PARTIAL" in addons and p.partial_done):  # ratchet the stop upward
            atr = float(v.A[i, col[t]])
            new_stop = trail_stop(p.stop, max(p.highest_close, px), atr, float(ex["trailing_atr_multiple"]))
            if new_stop > p.stop + 1e-9:
                plan.stop_updates.append((t, round(new_stop, 2)))

    # ------------------------------------------------------------------ 2. can we buy at all?
    if ctx.new_buys_blocked:  # daily loss limit / kill switch
        plan.notes.append(f"new buys blocked: {ctx.block_reason}")
        return plan
    risk_scale, max_pos = 1.0, int(rk["max_positions"])  # defaults
    if strategy.use_regime_filter and not bull:
        variant = cfg["regime"]["variant"]
        if variant == "R1":  # block new entries in a bear regime
            plan.notes.append("BEAR regime: new entries blocked (R1)")
            return plan
        if variant == "R2":  # halve risk and position count in a bear regime
            risk_scale, max_pos = 0.5, max(1, max_pos // 2)
    if strategy.classic and not month_end:
        return plan  # S0 trades only at month-end
    if cfg["portfolio"].get("rebalance", "DAILY") == "WEEKLY":  # weekly selection: last session of the week
        week_end = ctx.is_week_end if ctx.is_week_end is not None else (
            i == len(dates) - 1 or dates[i].isocalendar()[1] != dates[i + 1].isocalendar()[1])
        if not week_end:
            return plan

    # ------------------------------------------------------------------ 3. candidates
    sig_idx = np.flatnonzero(v.E[i])  # columns with an entry signal today
    cands = [v.cols[k] for k in sig_idx if v.cols[k] not in ctx.positions]  # new names only
    if ctx.tradable is not None:
        cands = [t for t in cands if t in ctx.tradable]  # must be available at the broker
    rank_row, atrp_row = v.R[i], v.AP[i]  # today's ranks and ATR%
    if cands:  # strongest first; calmer (lower ATR%) wins ties
        ks = np.array([col[t] for t in cands], dtype=int)
        order = np.lexsort((np.nan_to_num(atrp_row[ks], nan=1.0), -np.nan_to_num(rank_row[ks])))
        cands = [cands[j] for j in order]

    remaining = {t: p for t, p in ctx.positions.items() if t not in exiting}  # positions still held tomorrow
    prices = {t: close_of(t) for t in remaining}  # for heat
    protected = [p for p in remaining.values() if p.stop_active]  # S0 positions have no stop => no heat
    heat = portfolio_heat_gbp(protected, prices, fx, costs, rk.get("heat_definition", "current"))
    cash = ctx.cash_gbp  # cash we may commit
    held_sectors = [p.sector for p in remaining.values()]  # for the sector cap
    n_pos = len(remaining)  # open positions tomorrow
    win = int(cfg["portfolio"]["correlation_window_days"])
    ret_window = v.LR[max(0, i - win + 1): i + 1]  # last 60 days of returns
    hold_cols = [col[t] for t in remaining if t in col]  # holdings' columns
    blackout = int(cfg["events"]["earnings_blackout_days"])
    unknown_ok = not (ctx.live_mode or cfg["events"].get("unknown_earnings_is_ineligible_in_backtest", False))

    for t in cands:
        if n_pos >= max_pos:  # portfolio full
            plan.rejections.append((t, "max_positions")); break
        c_ = col[t]
        close, atr = float(v.C[i, c_]), float(v.A[i, c_])
        # earnings blackout (dates are published in advance, so this is not look-ahead)
        e = next_earnings_within(md.earnings.get(t), dates, i, blackout)
        if e is True:
            plan.rejections.append((t, "earnings_blackout")); continue
        if e is None and not unknown_ok:
            plan.rejections.append((t, "earnings_unknown")); continue
        # entry price cap and stop
        limit_px = round(close * (1.0 + float(cfg["entry"]["limit_offset_atr"]) * atr / close), 2)  # worst entry
        swing = float(np.nanmin(v.LOW[max(0, i - int(st["swing_low_days"]) + 1): i + 1, c_]))  # swing low
        stop = initial_stop(st["method"], close, atr, swing, cfg)
        ok, why = stop_is_valid(limit_px, stop, atr, cfg)
        if not ok:
            plan.rejections.append((t, why)); continue
        stop = round(stop, 2)  # broker price precision
        k = float(tg["r_multiple"])
        tgt = target_price(limit_px, stop, k, costs)  # planned target at the worst entry
        if planned_rr(limit_px, stop, tgt, costs) < float(tg["min_planned_rr"]) - 1e-9:  # R:R rule
            plan.rejections.append((t, "rr_below_min")); continue
        sector = sector_of(t, md.sectors)
        if not sector_allows(sector, held_sectors, int(cfg["portfolio"]["max_per_sector"])):
            plan.rejections.append((t, "sector_cap")); continue
        corrs = correlation_with_holdings(ret_window, c_, hold_cols)
        if not passes_correlation_gate(corrs, float(cfg["portfolio"]["correlation_threshold"]),
                                       int(cfg["portfolio"]["max_highly_correlated_holdings"])):
            plan.rejections.append((t, "correlation_cap")); continue
        sz = size_position(equity_gbp=ctx.equity_gbp, cash_gbp=cash, heat_gbp=heat, limit_px=limit_px, stop_px=stop,
                           fx_usd_per_gbp=fx, cfg=cfg, costs=costs, step=ctx.quantity_step,
                           max_open_qty=(ctx.max_open_qty or {}).get(t, float("inf")), risk_scale=risk_scale)
        if sz.qty <= 0:
            plan.rejections.append((t, sz.reason))
            if sz.reason == "portfolio_heat_full":
                break  # no budget left for anyone
            continue
        plan.entries.append(EntryPlan(t, sz.qty, limit_px, stop, k, sz.risk_gbp, float(rank_row[c_]), sector, close, atr,
                                      stop_active=not strategy.classic))
        cash -= sz.notional_gbp  # reserve cash for this order
        heat += sz.risk_gbp  # reserve risk budget
        held_sectors.append(sector)
        hold_cols.append(c_)  # later candidates are compared with this one too
        n_pos += 1
    return plan

"""Portfolio exposure and open-risk ("heat") calculations, in GBP."""
from __future__ import annotations

from dataclasses import dataclass, field  # records
from typing import Iterable, Optional  # type hints

import pandas as pd  # timestamps

from ..backtest.costs import CostModel  # costs


@dataclass
class HeldPosition:
    """A position managed by the strategy (the same record is used in backtest and live)."""

    ticker: str  # vendor symbol (e.g. AAPL)
    qty: float  # shares held
    entry_px: float  # average fill price (USD)
    entry_date: pd.Timestamp  # fill date (trading day)
    stop: float  # current protective stop (USD); only ever moves up
    initial_stop: float  # stop at entry (defines 1R)
    target: float  # profit target (USD); inf if none
    sector: str  # sector label
    initial_risk_gbp: float  # planned loss at the initial stop (1R in GBP)
    highest_close: float  # for trailing stops
    stop_active: bool = True  # False for the classic S0 benchmark (stop used only for sizing)
    partial_done: bool = False  # partial-profit variant state
    broker_ticker: str = ""  # Trading 212 instrument id (live only)
    cost_basis_gbp: float = 0.0  # GBP paid incl. fees (for P&L)
    extra: dict = field(default_factory=dict)  # free-form metadata


def position_heat_gbp(p: HeldPosition, price: float, fx: float, costs: CostModel) -> float:
    """Open risk: what we lose from `price` down to the (slipped) stop, incl. exit costs, in GBP."""
    if not (price == price) or price <= 0:  # NaN/invalid price: fall back to entry price (conservative)
        price = p.entry_px
    exit_px = p.stop * (1.0 - costs.stop_slippage)  # stop fill assumption
    loss_usd = max(0.0, (price - exit_px) * p.qty)  # price-to-stop loss
    exit_costs_usd = (costs.fx_fee + costs.sec_rate) * exit_px * p.qty + costs.finra_per_share * p.qty  # sell costs
    return (loss_usd + exit_costs_usd) / fx


def portfolio_heat_gbp(positions: Iterable[HeldPosition], prices: dict[str, float], fx: float, costs: CostModel,
                       definition: str = "current") -> float:
    """Total open risk. 'current' = price-to-stop (conservative); 'initial' = remaining initial risk."""
    total = 0.0
    for p in positions:
        if definition == "initial":
            total += p.initial_risk_gbp if p.stop < p.entry_px else 0.0  # risk-free once stop >= entry
        else:
            total += position_heat_gbp(p, prices.get(p.ticker, float("nan")), fx, costs)
    return total


def market_value_gbp(positions: Iterable[HeldPosition], prices: dict[str, float], fx: float) -> float:
    """Mark-to-market value of positions in GBP (uses entry price if today's price is missing)."""
    total = 0.0
    for p in positions:
        px = prices.get(p.ticker, float("nan"))  # today's close
        if not (px == px):  # NaN
            px = p.entry_px
        total += p.qty * px / fx
    return total


@dataclass
class Plan:
    """Container for one decision cycle's outputs."""

    exits: list = field(default_factory=list)  # ExitPlan objects
    entries: list = field(default_factory=list)  # EntryPlan objects
    stop_updates: list = field(default_factory=list)  # (ticker, new_stop)
    rejections: list = field(default_factory=list)  # (ticker, reason) for SIGNAL_REJECTED logs
    notes: list = field(default_factory=list)  # free-text decisions (regime, limits)


@dataclass(frozen=True)
class EntryPlan:
    """An approved new position: buy `qty` with a limit, then protect with `stop`."""

    ticker: str  # vendor symbol
    qty: float  # shares
    limit_px: float  # worst acceptable entry price (USD)
    stop_px: float  # protective stop (USD)
    target_r: float  # target multiple k (target set from the actual fill)
    risk_gbp: float  # planned loss at the stop
    rank: float  # momentum rank (for reporting)
    sector: str  # sector
    signal_close: float  # close on the decision day
    atr: float  # ATR on the decision day
    stop_active: bool = True  # False for S0


@dataclass(frozen=True)
class ExitPlan:
    """A position (or part) to sell at the next session's open."""

    ticker: str  # vendor symbol
    qty: float  # shares to sell
    reason: str  # TARGET / TIME / TREND / MOMENTUM / REGIME / PARTIAL / CLASSIC_REBALANCE
    new_stop_after: Optional[float] = None  # for partial exits: move the remaining stop here

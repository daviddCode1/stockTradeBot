"""Objective stop and target rules. A stop is fixed at decision time and may never be widened."""
from __future__ import annotations

import math  # isfinite
from typing import Any, Mapping  # type hints

from ..backtest.costs import CostModel  # cost assumptions


def initial_stop(method: str, close: float, atr: float, swing_low: float | None, cfg: Mapping[str, Any]) -> float | None:
    """Compute the initial stop price (USD). Returns None if it cannot be established."""
    s = cfg["stops"]
    if not (math.isfinite(close) and close > 0):  # no valid price => no stop => no trade
        return None
    if method == "ATR":
        if not (math.isfinite(atr) and atr > 0):
            return None
        stop = close - float(s["atr_multiple"]) * atr  # m x ATR below the close
    elif method == "PERCENT":
        stop = close * (1.0 - float(s.get("percent", s["percent_options"][1])))  # fixed % below
    elif method == "SWING_LOW":
        if swing_low is None or not math.isfinite(swing_low) or not math.isfinite(atr):
            return None
        stop = swing_low - float(s["swing_low_atr_buffer"]) * atr  # below the recent swing low
    else:
        raise ValueError(f"unknown stop method {method}")
    return stop if stop > 0 else None  # a non-positive stop is meaningless


def stop_is_valid(entry_px: float, stop: float | None, atr: float, cfg: Mapping[str, Any]) -> tuple[bool, str]:
    """Reject stops that are too wide (>20%) or too tight (<0.5 ATR, i.e. inside normal noise)."""
    s = cfg["stops"]
    if stop is None or not math.isfinite(stop):
        return False, "stop_unavailable"
    dist = entry_px - stop  # risk per share before costs
    if dist <= 0:
        return False, "stop_above_entry"
    if dist / entry_px > float(s["max_stop_distance_pct"]):
        return False, "stop_too_wide"
    if math.isfinite(atr) and dist < float(s["min_stop_distance_atr"]) * atr:
        return False, "stop_too_tight"
    return True, "ok"


def net_risk_per_share_usd(entry_px: float, stop: float, costs: CostModel) -> float:
    """Loss per share if stopped out: entry minus slipped stop fill plus round-trip costs (USD)."""
    exit_worst = stop * (1.0 - costs.stop_slippage)  # stop orders become market orders -> slippage
    return (entry_px - exit_worst) + costs.round_trip_cost_per_share_usd(entry_px, exit_worst)


def target_price(entry_px: float, stop: float, k: float, costs: CostModel) -> float:
    """Price at which the NET reward equals k x the NET risk (so planned R:R >= k after costs)."""
    net_risk = net_risk_per_share_usd(entry_px, stop, costs)  # USD per share at risk
    # Solve: (T - entry) - costs(entry, T) = k * net_risk, with costs linear in T.
    fx, sec = costs.fx_fee, costs.sec_rate
    return (k * net_risk + entry_px * (1 + fx) + costs.finra_per_share) / (1 - fx - sec)


def planned_rr(entry_px: float, stop: float, target: float, costs: CostModel) -> float:
    """Planned reward:risk after costs. Used to enforce the R:R >= 2 rule."""
    risk = net_risk_per_share_usd(entry_px, stop, costs)  # net loss if stopped
    reward = (target - entry_px) - costs.round_trip_cost_per_share_usd(entry_px, target)  # net gain at target
    return reward / risk if risk > 0 else 0.0


def trail_stop(current_stop: float, highest_close: float, atr: float, mult: float) -> float:
    """Chandelier trailing stop that can only move UP (never widened)."""
    if not (math.isfinite(highest_close) and math.isfinite(atr)):
        return current_stop  # missing data: keep the existing stop
    return max(current_stop, highest_close - mult * atr)  # ratchet upward only

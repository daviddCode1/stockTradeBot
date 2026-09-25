"""Risk-based position sizing in the account's base currency (GBP).

qty = floor_to_step( min(q_risk, q_weight, q_cash, q_broker) )
  q_risk   = risk_budget / risk_per_share_gbp            (0.5% of equity, capped by free heat)
  q_weight = max_weight * equity / cost_per_share_gbp    (no single-name concentration)
  q_cash   = free_cash / cost_per_share_gbp              (never borrow: no leverage)
  q_broker = broker max open quantity - existing
risk_per_share_gbp uses the WORST entry (limit price), a slipped stop fill, both FX fees,
US sell fees, and an FX-move buffer. Rounding is always DOWN, so risk can only fall.
"""
from __future__ import annotations

import math  # floor
from dataclasses import dataclass  # result type
from typing import Any, Mapping  # type hints

from ..backtest.costs import CostModel  # cost assumptions
from .stops import net_risk_per_share_usd  # per-share risk incl. costs


@dataclass(frozen=True)
class SizingResult:
    """Outcome of sizing one trade."""

    qty: float  # shares to buy (0 => do not trade)
    risk_gbp: float  # planned loss at the stop, GBP
    risk_per_share_gbp: float  # GBP at risk per share
    notional_gbp: float  # GBP cost of the position incl. FX fee
    reason: str  # "ok" or why the size is zero
    binding: str = ""  # which cap determined the size (risk/weight/cash/broker)


def floor_to_step(q: float, step: float) -> float:
    """Round DOWN to the broker's quantity step (1 = whole shares, 0.01 = fractional)."""
    if step <= 0:
        raise ValueError("quantity step must be positive")
    n = math.floor(q / step + 1e-9)  # tolerance guards against float artefacts (e.g. 2.9999999)
    return round(n * step, 10)  # clean float representation


def size_position(*, equity_gbp: float, cash_gbp: float, heat_gbp: float, limit_px: float, stop_px: float,
                  fx_usd_per_gbp: float, cfg: Mapping[str, Any], costs: CostModel, step: float,
                  max_open_qty: float = float("inf"), existing_qty: float = 0.0,
                  risk_scale: float = 1.0) -> SizingResult:
    """Return the largest safe quantity, or qty=0 with a reason. Pure function (no I/O)."""
    r = cfg["risk"]
    if not all(math.isfinite(x) for x in (equity_gbp, cash_gbp, limit_px, stop_px, fx_usd_per_gbp)):
        return SizingResult(0.0, 0.0, 0.0, 0.0, "non_finite_input")  # uncertain inputs => no trade
    if equity_gbp <= 0 or fx_usd_per_gbp <= 0 or limit_px <= 0 or stop_px <= 0 or stop_px >= limit_px:
        return SizingResult(0.0, 0.0, 0.0, 0.0, "invalid_input")
    fx_eff = fx_usd_per_gbp * (1.0 - float(r["fx_buffer"]))  # assume GBP weakens a little before the fill
    risk_ps_gbp = net_risk_per_share_usd(limit_px, stop_px, costs) / fx_eff  # GBP at risk per share
    per_trade = float(r["risk_per_trade"]) * risk_scale * equity_gbp  # e.g. 0.5% of equity
    free_heat = float(r["max_portfolio_heat"]) * equity_gbp - heat_gbp  # remaining portfolio risk budget
    budget = min(per_trade, free_heat)  # both limits must hold
    if budget <= 0:
        return SizingResult(0.0, 0.0, risk_ps_gbp, 0.0, "portfolio_heat_full")
    cost_ps_gbp = limit_px * (1.0 + costs.fx_fee) / fx_eff  # GBP needed per share (worst case)
    caps = {
        "risk": budget / risk_ps_gbp,  # risk-based size
        "weight": float(r["max_position_weight"]) * equity_gbp / cost_ps_gbp,  # concentration cap
        "cash": max(cash_gbp, 0.0) / cost_ps_gbp,  # no leverage
        "broker": max(max_open_qty - existing_qty, 0.0),  # broker position limit
    }
    binding = min(caps, key=caps.get)  # tightest constraint (for diagnostics)
    qty = floor_to_step(caps[binding], step)  # always round down
    if qty <= 0:
        return SizingResult(0.0, 0.0, risk_ps_gbp, 0.0, f"size_below_step:{binding}", binding)
    risk_gbp = qty * risk_ps_gbp  # planned loss
    if risk_gbp > per_trade * 1.000001:  # defensive re-check: never exceed the per-trade limit
        return SizingResult(0.0, 0.0, risk_ps_gbp, 0.0, "risk_recheck_failed", binding)
    return SizingResult(qty, risk_gbp, risk_ps_gbp, qty * cost_ps_gbp, "ok", binding)

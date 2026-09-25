"""Final pre-submission risk gate (defence in depth).

`decide` already sizes and filters every trade. This gate re-checks the finished order
immediately before it is sent, so a bug elsewhere cannot push through an over-sized or
malformed order. Any failure => the order is NOT sent.
"""
from __future__ import annotations

import math  # finite checks
from typing import Any, Mapping  # type hints

from ..backtest.costs import CostModel  # costs
from ..portfolio.exposure import EntryPlan  # entry record
from .stops import net_risk_per_share_usd, planned_rr, target_price  # risk maths


def validate_entry(e: EntryPlan, equity_gbp: float, fx_usd_per_gbp: float, cfg: Mapping[str, Any],
                   costs: CostModel) -> tuple[bool, str]:
    """Return (ok, reason). Checks finiteness, stop below entry, risk <= limit, R:R >= minimum."""
    vals = (e.qty, e.limit_px, e.stop_px, equity_gbp, fx_usd_per_gbp)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals):
        return False, "non_finite"  # uncertain numbers => no trade
    if e.qty <= 0 or e.limit_px <= 0 or e.stop_px <= 0 or e.stop_px >= e.limit_px:
        return False, "invalid_prices_or_qty"
    risk_gbp = e.qty * net_risk_per_share_usd(e.limit_px, e.stop_px, costs) / fx_usd_per_gbp  # at worst entry
    limit = float(cfg["risk"]["risk_per_trade"]) * equity_gbp * 1.01  # 1% tolerance for FX rounding only
    if risk_gbp > limit:
        return False, f"risk {risk_gbp:.2f} > limit {limit:.2f}"
    tgt = target_price(e.limit_px, e.stop_px, e.target_r, costs)
    if planned_rr(e.limit_px, e.stop_px, tgt, costs) < float(cfg["targets"]["min_planned_rr"]) - 1e-9:
        return False, "rr_below_min"
    return True, "ok"

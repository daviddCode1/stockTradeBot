"""Stops, R:R, position sizing (0.5% risk, FX, rounding), portfolio heat, limits."""
from __future__ import annotations

import math

import pytest

from src.backtest.costs import CostModel
from src.config import deep_update
from src.portfolio.correlation import passes_correlation_gate
from src.portfolio.exposure import EntryPlan, HeldPosition, portfolio_heat_gbp
from src.portfolio.sector_limits import sector_allows
from src.risk.daily_limits import DailyLossGuard
from src.risk.position_sizing import floor_to_step, size_position
from src.risk.risk_manager import validate_entry
from src.risk.stops import (initial_stop, net_risk_per_share_usd, planned_rr, stop_is_valid, target_price,
                            trail_stop)


def test_atr_stop(cfg):
    assert initial_stop("ATR", 100.0, 2.0, None, cfg) == pytest.approx(100 - 3.0 * 2.0)
    assert initial_stop("ATR", 100.0, float("nan"), None, cfg) is None  # cannot establish => None


def test_percent_and_swing_stops(cfg):
    assert initial_stop("PERCENT", 100.0, 2.0, None, cfg) == pytest.approx(90.0)
    assert initial_stop("SWING_LOW", 100.0, 2.0, 95.0, cfg) == pytest.approx(94.0)


def test_stop_validity(cfg):
    assert stop_is_valid(100, 94, 2, cfg)[0]
    assert stop_is_valid(100, 70, 2, cfg) == (False, "stop_too_wide")
    assert stop_is_valid(100, 99.5, 2, cfg) == (False, "stop_too_tight")
    assert stop_is_valid(100, None, 2, cfg)[0] is False


def test_target_gives_rr_at_least_k_after_costs(costs):
    for k in (2.0, 2.25, 2.5, 3.0):
        tgt = target_price(100.5, 94.0, k, costs)
        assert planned_rr(100.5, 94.0, tgt, costs) == pytest.approx(k, rel=1e-9)


def test_rr_below_two_rejected(cfg, costs):
    e = EntryPlan("X", 5, 100.5, 94.0, 1.5, 30.0, 0.9, "Tech", 100.0, 2.0)  # k = 1.5 < 2
    ok, why = validate_entry(e, 10000, 1.25, cfg, costs)
    assert not ok and why == "rr_below_min"


def test_trailing_stop_never_widens():
    assert trail_stop(95.0, 100.0, 5.0, 3.0) == 95.0  # 100-15=85 < 95 => keep 95
    assert trail_stop(95.0, 120.0, 5.0, 3.0) == 105.0  # ratchets up
    assert trail_stop(95.0, float("nan"), 5.0, 3.0) == 95.0  # missing data => unchanged


def test_worked_example_from_spec(cfg, costs):
    """GBP 10,000 account, fx 1.25, C=$100, ATR=$2, stop $94, limit $100.50 -> 8 whole shares, risk <= GBP 50."""
    c = deep_update(cfg, {"broker": {"quantity_step": 1.0}})
    r = size_position(equity_gbp=10000, cash_gbp=10000, heat_gbp=0, limit_px=100.5, stop_px=94.0, fx_usd_per_gbp=1.25,
                      cfg=c, costs=costs, step=1.0)
    assert r.qty == 8 and r.risk_gbp <= 50.0 and r.binding == "risk"


def test_half_percent_risk_never_exceeded_fractional(cfg, costs):
    for eq in (1000, 5000, 25000, 100000):
        for stop in (80.0, 90.0, 95.0, 98.0):
            r = size_position(equity_gbp=eq, cash_gbp=eq, heat_gbp=0, limit_px=100.0, stop_px=stop, fx_usd_per_gbp=1.3,
                              cfg=cfg, costs=costs, step=0.01)
            assert r.risk_gbp <= 0.005 * eq + 1e-9
            # a worst-case realised loss at the slipped stop, incl. costs, stays within budget at the fx used
            loss = r.qty * net_risk_per_share_usd(100.0, stop, costs) / 1.3
            assert loss <= 0.005 * eq * 1.01


def test_rounding_down_only():
    assert floor_to_step(8.999, 1.0) == 8.0
    assert floor_to_step(2.999999999, 1.0) == 3.0  # float artefact tolerance
    assert floor_to_step(0.567, 0.01) == 0.56


def test_heat_cap_blocks(cfg, costs):
    r = size_position(equity_gbp=10000, cash_gbp=10000, heat_gbp=500, limit_px=100, stop_px=94, fx_usd_per_gbp=1.25,
                      cfg=cfg, costs=costs, step=0.01)
    assert r.qty == 0 and r.reason == "portfolio_heat_full"  # 5% of 10k already used


def test_cash_and_weight_caps(cfg, costs):
    r = size_position(equity_gbp=10000, cash_gbp=300, heat_gbp=0, limit_px=100, stop_px=99.4, fx_usd_per_gbp=1.25,
                      cfg=cfg, costs=costs, step=0.01)
    assert r.binding == "cash" and r.notional_gbp <= 300 + 1e-6  # never borrows
    r2 = size_position(equity_gbp=10000, cash_gbp=10000, heat_gbp=0, limit_px=100, stop_px=99.4, fx_usd_per_gbp=1.25,
                       cfg=cfg, costs=costs, step=0.01)
    assert r2.binding == "weight" and r2.notional_gbp <= 0.2 * 10000 + 1e-6


def test_invalid_inputs_no_trade(cfg, costs):
    for kw in ({"limit_px": float("nan")}, {"stop_px": 101.0}, {"fx_usd_per_gbp": 0.0}):
        base = dict(equity_gbp=10000, cash_gbp=10000, heat_gbp=0, limit_px=100.0, stop_px=94.0, fx_usd_per_gbp=1.25)
        base.update(kw)
        assert size_position(**base, cfg=cfg, costs=costs, step=0.01).qty == 0


def test_fx_conversion_costs(costs):
    assert costs.buy_cost_gbp(10, 100, 1.25) == pytest.approx(10 * 100 / 1.25 * 1.0015)
    proceeds = costs.sell_proceeds_gbp(10, 100, 1.25)
    assert proceeds < 10 * 100 / 1.25 * (1 - 0.0015) + 1e-9  # FX fee + SEC/FINRA deducted


def test_fx_fee_matches_demo_fill():
    """Demo fill 2026-08-06: GBP 4545.68 notional charged GBP 6.81 FX fee (0.15%)."""
    assert abs(4545.68 * CostModel().fx_fee - 6.81) < 0.01  # model fee matches the broker's charge


def test_portfolio_heat(costs):
    p = HeldPosition("X", 10, 100, None, 94, 94, 112, "Tech", 60, 100)
    h = portfolio_heat_gbp([p], {"X": 100.0}, 1.25, costs)
    assert h == pytest.approx(((100 - 94 * 0.995) * 10 + (0.0015 + 0.0000278) * 94 * 0.995 * 10 + 0.000195 * 10) / 1.25)


def test_sector_and_correlation_limits():
    import numpy as np
    assert sector_allows("Tech", ["Tech", "Tech"], 3) and not sector_allows("Tech", ["Tech"] * 3, 3)
    assert passes_correlation_gate(np.array([0.9, 0.2]), 0.75, 1)
    assert not passes_correlation_gate(np.array([0.9, 0.8]), 0.75, 1)
    assert passes_correlation_gate(np.array([np.nan, np.nan]), 0.75, 1)  # unknown is not "high"


def test_daily_loss_guard():
    g = DailyLossGuard(0.015, requires_reset=True)
    assert g.update(98.4, 100.0) and g.blocked  # -1.6% day
    g.update(101, 98.4)
    assert g.blocked  # requires explicit reset
    g.reset()
    assert not g.blocked
    g2 = DailyLossGuard(0.015, requires_reset=False)
    g2.update(98.0, 100.0); g2.update(99.0, 98.0)
    assert not g2.blocked  # auto-clears next day

"""Backtest engine: fills, stops, gaps, costs, corporate actions, metrics, walk-forward, Monte Carlo."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.deflated_sharpe import deflated_sharpe, expected_max_sharpe
from src.backtest.engine import run_backtest
from src.backtest.metrics import drawdown, equity_metrics, max_consecutive, trade_metrics
from src.backtest.monte_carlo import bootstrap_trades
from src.backtest.walk_forward import walk_forward
from src.config import deep_update
from src.data.market_data import MarketData, reconstruct_unadjusted
from src.strategy.interface import Features, Strategy, compute_features
from src.strategy.momentum import ClassicMomentum
from src.strategy.momentum_trend import MomentumTrendRegime


class OneShot(Strategy):
    """Test strategy: a single entry signal for one ticker on one day."""

    name = "T"

    def __init__(self, cfg, ticker, day_idx):
        super().__init__(cfg)
        self.t, self.i = ticker, day_idx

    def entries(self, md, f):
        e = pd.DataFrame(False, index=md.dates, columns=md.close.columns)
        e.iloc[self.i, e.columns.get_loc(self.t)] = True
        return e


def _tiny_md(opens, highs, lows, closes, n_other=0):
    """Hand-built market data for exact fill tests (1 stock, flat benchmark)."""
    n = len(closes)
    idx = pd.bdate_range("2020-01-01", periods=n)
    mk = lambda v: pd.DataFrame({"X": v}, index=idx, dtype=float)
    bench = pd.Series(np.linspace(100, 120, n), index=idx)
    return MarketData(open=mk(opens), high=mk(highs), low=mk(lows), close=mk(closes), adj_close=mk(closes),
                      volume=mk(np.full(n, 1e7)), unadj_close=mk(closes), dividends=mk(np.zeros(n)),
                      benchmark_close=bench, benchmark_adj=bench, fx_usd_per_gbp=pd.Series(1.25, index=idx),
                      sectors={"X": "Tech"})


def _fake_features(md):
    idx, cols = md.dates, md.close.columns
    one = pd.DataFrame(1.0, index=idx, columns=cols)
    return Features(eligible=one.astype(bool), signal=one, rank=one, mom_12_1=one, rs_12_1=one, sma20=one * 0,
                    sma50=one * 0, sma200=one * 0, atr=one * 2.0, atr_pct=one * 0.02,
                    bull=pd.Series(True, index=idx), log_ret=one * 0)


def _run(md, cfg, day=0):
    f = _fake_features(md)
    s = OneShot(cfg, "X", day)
    return run_backtest(md, s, cfg, features=f, entries_panel=s.entries(md, f))


def test_signal_executes_next_open_not_same_close(cfg):
    closes = [100.0] * 10
    md = _tiny_md(opens=[100, 101] + [101] * 8, highs=[102] * 10, lows=[99.5] * 10, closes=closes)
    res = _run(md, cfg)
    t = res.trades.iloc[0]
    assert t["entry_date"] == md.dates[1]  # decided at close of day 0, filled day 1
    assert t["entry_px"] == pytest.approx(min(100 * (1 + 0.25 * 0.02), 101 * 1.0005))


def test_gap_above_limit_no_trade(cfg):
    md = _tiny_md(opens=[100, 110] + [110] * 8, highs=[111] * 10, lows=[109] * 10, closes=[100] + [110] * 9)
    res = _run(md, cfg)
    assert res.trades.empty  # opened 10% above the signal close: limit not reached, no chasing


def test_stop_hit_intraday_and_gap(cfg):
    # stop = 100 - 3*2 = 94. Day 2 low touches 93 -> stopped at 94*(1-0.5%)
    md = _tiny_md(opens=[100, 100, 99, 99], highs=[101] * 4, lows=[99.5, 99.5, 93, 93], closes=[100, 100, 95, 95])
    t = _run(md, cfg).trades.iloc[0]
    assert t["reason"] == "STOP" and t["exit_px"] == pytest.approx(94 * 0.995)
    md2 = _tiny_md(opens=[100, 100, 90, 90], highs=[101, 101, 91, 91], lows=[99.5, 99.5, 89, 89], closes=[100, 100, 90, 90])
    t2 = _run(md2, cfg).trades.iloc[0]
    assert t2["reason"] == "STOP_GAP" and t2["exit_px"] == pytest.approx(90 * 0.995)  # gap fills at the open
    assert t2["r"] < -1.0  # gaps can lose more than 1R - reported, not hidden


def test_target_exit_next_open(cfg):
    n = 12
    closes = [100, 100, 101, 130, 130] + [130] * (n - 5)
    md = _tiny_md(opens=[100, 100, 101, 128, 131] + [130] * (n - 5), highs=[131] * n, lows=[99.5] * n, closes=closes)
    t = _run(md, cfg).trades.iloc[0]
    assert t["reason"] == "TARGET" and t["exit_date"] == md.dates[4]  # target seen at close 3, sold at open 4
    assert t["r"] > 1.8  # about 2R after costs


def test_time_exit(cfg):
    c = deep_update(cfg, {"exits": {"max_holding_days": 3}})
    md = _tiny_md(opens=[100] * 10, highs=[100.5] * 10, lows=[99.5] * 10, closes=[100] * 10)
    t = _run(md, c).trades.iloc[0]
    assert t["reason"] == "TIME" and t["days"] == 4  # decided after 3 sessions held, sold next open


def test_costs_reduce_pnl(cfg):
    md = _tiny_md(opens=[100] * 10, highs=[100.5] * 10, lows=[99.5] * 10, closes=[100] * 10)
    c = deep_update(cfg, {"exits": {"max_holding_days": 3}})
    t = _run(md, c).trades.iloc[0]
    assert t["pnl_gbp"] < 0  # flat price => loss equals costs (FX both legs + slippage)


def test_dividends_credited_net_of_withholding(cfg):
    md = _tiny_md(opens=[100] * 6, highs=[100.5] * 6, lows=[99.5] * 6, closes=[100] * 6)
    md.dividends.iloc[3, 0] = 1.0  # $1 ex-dividend on day 3
    c = deep_update(cfg, {"exits": {"max_holding_days": 50}})
    res_div = _run(md, c)
    md.dividends.iloc[3, 0] = 0.0
    res_none = _run(md, c)
    qty = res_none.trades.iloc[0]["qty"]
    assert res_div.equity.iloc[-1] - res_none.equity.iloc[-1] == pytest.approx(qty * 1.0 * 0.85 / 1.25, rel=1e-6)


def test_split_unadjustment():
    idx = pd.bdate_range("2020-01-01", periods=4)
    close = pd.DataFrame({"A": [25.0, 25.0, 25.0, 25.0]}, index=idx)  # split-adjusted
    splits = pd.DataFrame({"A": [0.0, 0.0, 4.0, 0.0]}, index=idx)  # 4:1 split on day 2
    un = reconstruct_unadjusted(close, splits)
    assert list(un["A"]) == [100.0, 100.0, 25.0, 25.0]  # pre-split prices were 4x higher


def test_engine_no_negative_cash_and_limits(md, cfg):
    res = run_backtest(md, MomentumTrendRegime(cfg), cfg)
    assert (res.cash >= -1e-6).all()  # no leverage
    assert res.positions_count.max() <= cfg["risk"]["max_positions"]
    tr = res.trades
    assert tr.empty or (tr["initial_risk_gbp"] <= 0.005 * res.equity.max() * 1.01).all()


def test_classic_benchmark_runs(md, cfg):
    res = run_backtest(md, ClassicMomentum(cfg), cfg)
    assert not res.trades.empty and set(res.trades["reason"]) <= {"CLASSIC_REBALANCE", "END_OF_TEST"}


def test_metrics_basic():
    eq = pd.Series([100, 110, 99, 120], index=pd.bdate_range("2020-01-01", periods=4), dtype=float)
    assert drawdown(eq).min() == pytest.approx(99 / 110 - 1)
    m = equity_metrics(eq)
    assert m["total_return"] == pytest.approx(0.2)
    assert max_consecutive(np.array([1, 1, 0, 1, 1, 1], dtype=bool)) == 3
    t = pd.DataFrame({"pnl_gbp": [10, -5, 20, -5], "r": [1, -0.5, 2, -0.5], "days": [1, 2, 3, 4], "reason": ["X"] * 4})
    tm = trade_metrics(t)
    assert tm["profit_factor"] == pytest.approx(3.0) and tm["win_rate"] == 0.5 and tm["avg_r"] == pytest.approx(0.5)


def test_monte_carlo_and_dsr():
    rng = np.random.default_rng(0)
    r = rng.normal(0.1, 1.0, 300)
    mc = bootstrap_trades(r, 0.005, n_iter=300)
    assert mc["max_dd_worst5pct"] <= mc["max_dd_median"] <= 0
    assert expected_max_sharpe(100, 0.05) > expected_max_sharpe(2, 0.05)  # more trials => higher luck bar
    d1 = deflated_sharpe(rng.normal(0.001, 0.01, 2000), n_trials=1)
    d100 = deflated_sharpe(rng.normal(0.001, 0.01, 2000), n_trials=100, trial_sharpes_daily=rng.normal(0, 0.03, 100))
    assert d100["dsr"] < d1["dsr"]


def test_walk_forward_runs(cfg):
    from src.data.synthetic import make_synthetic
    md = make_synthetic(n_stocks=60, n_days=252 * 9, seed=3)
    c = deep_update(cfg, {"walk_forward": {"train_years": 3, "min_train_trades": 5}, "backtest": {"holdout_months": 12, "start": "2015-01-01"}})
    grid = {"stops.atr_multiple": [2.5, 3.0], "targets.r_multiple": [2.0, 3.0]}
    wf = walk_forward(md, MomentumTrendRegime, c, grid=grid)
    assert len(wf.folds) >= 2 and len(wf.oos_equity) > 200 and wf.n_trials == 4
    assert (wf.folds["test_start"].astype(str) > wf.folds["train_start"].astype(str)).all()

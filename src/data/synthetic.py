"""Deterministic synthetic market data for tests and offline engine checks.

This is NOT research data. It only exists so the engine, risk and execution code can be
tested without network access. Results on synthetic data say nothing about real markets.
"""
from __future__ import annotations

import numpy as np  # random numbers
import pandas as pd  # panels

from .market_data import MarketData  # container


def make_synthetic(n_stocks: int = 60, n_days: int = 900, seed: int = 7, start: str = "2015-01-02",
                   drift_spread: float = 0.0006) -> MarketData:
    """Geometric random walks with heterogeneous drifts (so momentum ranks are non-trivial)."""
    rng = np.random.default_rng(seed)  # reproducible
    dates = pd.bdate_range(start, periods=n_days)  # business days as a stand-in calendar
    tickers = [f"S{i:03d}" for i in range(n_stocks)]  # synthetic names
    drifts = rng.normal(0.0003, drift_spread, n_stocks)  # per-stock daily drift
    vols = rng.uniform(0.012, 0.03, n_stocks)  # per-stock daily volatility
    rets = rng.normal(drifts, vols, size=(n_days, n_stocks))  # daily log returns
    close = 50 * np.exp(np.cumsum(rets, axis=0))  # price paths starting near $50
    gap = rng.normal(0, 0.004, size=(n_days, n_stocks))  # overnight gap noise
    open_ = np.vstack([close[0], close[:-1]]) * np.exp(gap)  # open = prior close +/- gap
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, size=close.shape)))  # high above both
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, size=close.shape)))  # low below both
    vol = rng.uniform(1e6, 5e6, size=close.shape)  # shares traded
    mk = lambda a: pd.DataFrame(a, index=dates, columns=tickers)  # array -> panel
    bench = pd.Series(300 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n_days))), index=dates)  # benchmark path
    sectors = {t: f"SEC{i % 8}" for i, t in enumerate(tickers)}  # 8 fake sectors
    return MarketData(
        open=mk(open_), high=mk(hi), low=mk(lo), close=mk(close), adj_close=mk(close), volume=mk(vol),
        unadj_close=mk(close), dividends=mk(np.zeros_like(close)), benchmark_close=bench, benchmark_adj=bench,
        fx_usd_per_gbp=pd.Series(1.30, index=dates), membership=None, sectors=sectors, earnings={},
        notes=["SYNTHETIC DATA - engine test only"],
    )

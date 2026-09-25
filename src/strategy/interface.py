"""Strategy contract and shared feature computation.

`compute_features` builds every indicator panel once (vectorised, backward-looking).
A `Strategy` then turns those features into a boolean entry panel. The SAME features and
strategy objects are used by the backtester and by the live/paper runner.
"""
from __future__ import annotations

from abc import ABC, abstractmethod  # strategy base class
from dataclasses import dataclass  # feature bundle
from typing import Any, Mapping  # type hints

import numpy as np  # numerics
import pandas as pd  # panels

from ..data.market_data import MarketData  # input data
from . import indicators as ind  # indicator library
from .ranking import percentile_rank  # cross-sectional rank
from .regime import bull_regime  # market regime


@dataclass
class Features:
    """Indicator panels aligned with MarketData (dates x tickers)."""

    eligible: pd.DataFrame  # passes universe/tradability filters on day t
    signal: pd.DataFrame  # raw momentum value used for ranking
    rank: pd.DataFrame  # percentile rank of `signal` among eligible names
    mom_12_1: pd.DataFrame  # classic 12-1 momentum (absolute filter)
    rs_12_1: pd.DataFrame  # 12-1 momentum minus the benchmark's (relative strength filter)
    sma20: pd.DataFrame  # 20-day SMA
    sma50: pd.DataFrame  # 50-day SMA
    sma200: pd.DataFrame  # 200-day SMA
    atr: pd.DataFrame  # Wilder ATR(14)
    atr_pct: pd.DataFrame  # ATR / close
    bull: pd.Series  # market regime (True = BULL)
    log_ret: pd.DataFrame  # daily log returns (for correlation control)


def trend_mask(close: pd.DataFrame, sma50: pd.DataFrame, sma200: pd.DataFrame, variant: str) -> pd.DataFrame:
    """T0 none | T1 close>SMA200 | T2 SMA50>SMA200 | T3 both."""
    if variant == "T0":
        return pd.DataFrame(True, index=close.index, columns=close.columns)  # no filter
    t1 = close > sma200  # price above long-term trend
    t2 = sma50 > sma200  # medium-term trend above long-term
    if variant == "T1":
        return t1
    if variant == "T2":
        return t2
    if variant == "T3":
        return t1 & t2
    raise ValueError(f"unknown trend variant {variant}")


def compute_features(md: MarketData, cfg: Mapping[str, Any]) -> Features:
    """Compute all features from data <= t for every t (no look-ahead by construction)."""
    u, s, v = cfg["universe"], cfg["signals"], cfg["volatility"]  # config sections
    close = md.close  # split-adjusted close
    has_bar = close.notna()  # bar exists today (freshness)
    recent_complete = has_bar.astype(float).rolling(20, min_periods=20).sum() >= 20  # no gaps in last 20 bars
    history = has_bar.astype(float).cumsum() >= u["min_history_days"]  # enough history for 12-1 momentum
    adv = (md.unadj_close * md.volume).rolling(u["adv_window_days"], min_periods=u["adv_window_days"]).mean()  # $ volume
    eligible = (
        has_bar & recent_complete & history
        & (md.unadj_close >= u["min_price_usd"])  # no low-priced stocks (as-traded price)
        & (adv >= u["min_adv_usd"])  # liquid enough
    )
    if md.membership is not None:  # point-in-time index membership, if available
        eligible &= md.membership.reindex_like(eligible).fillna(False).astype(bool)
    lookbacks = s["lookbacks_days"]  # {"m3": 63, ...}
    signal = ind.momentum(md.adj_close, s["primary_signal"], lookbacks, s["skip_recent_days"])  # ranking signal
    mom_12_1 = ind.momentum(md.adj_close, "MOM_12_1", lookbacks, s["skip_recent_days"])  # absolute filter
    bench_12_1 = ind.momentum(md.benchmark_adj, "MOM_12_1", lookbacks, s["skip_recent_days"])  # benchmark
    rs_12_1 = mom_12_1.sub(bench_12_1, axis=0)  # relative strength vs benchmark
    eligible &= signal.notna()  # need a signal value
    rank = percentile_rank(signal, eligible, u["min_universe_size"])  # 0..1
    atr = ind.atr_wilder(md.high, md.low, close, v["atr_period"])  # volatility
    return Features(
        eligible=eligible.fillna(False).astype(bool), signal=signal, rank=rank, mom_12_1=mom_12_1, rs_12_1=rs_12_1,
        sma20=ind.sma(close, 20), sma50=ind.sma(close, cfg["trend"]["sma_fast"]), sma200=ind.sma(close, cfg["trend"]["sma_slow"]),
        atr=atr, atr_pct=atr / close, bull=bull_regime(md.benchmark_close, cfg["regime"]["sma_days"]),
        log_ret=np.log(close / close.shift(1)),
    )


def base_setup(md: MarketData, f: Features, cfg: Mapping[str, Any], trend_variant: str) -> pd.DataFrame:
    """Shared momentum setup: eligible, top-ranked, absolute/RS filters, trend filter, volatility cap."""
    s = cfg["signals"]
    setup = f.eligible & (f.rank >= s["entry_rank_min"])  # top of the cross-section
    if s.get("require_abs_momentum", True):
        setup &= f.mom_12_1 > 0  # absolute momentum positive
    if s.get("require_relative_strength", True):
        setup &= f.rs_12_1 > 0  # beat the benchmark
    setup &= trend_mask(md.close, f.sma50, f.sma200, trend_variant)  # trend confirmation
    setup &= f.atr_pct <= cfg["volatility"]["max_atr_pct"]  # skip extreme volatility
    return setup.fillna(False).astype(bool)


class Strategy(ABC):
    """A strategy variant = a rule producing a boolean entry panel from features."""

    name: str = "abstract"  # short id (S0, A, B, C, D, E)
    classic: bool = False  # True => monthly rank-based rebalance without stops/targets (S0)
    use_regime_filter: bool = False  # engine applies the regime rule when True

    def __init__(self, cfg: Mapping[str, Any]):
        self.cfg = cfg  # full settings dict

    @abstractmethod
    def entries(self, md: MarketData, f: Features) -> pd.DataFrame:
        """Boolean panel: True where a new long entry is signalled after the close of day t."""

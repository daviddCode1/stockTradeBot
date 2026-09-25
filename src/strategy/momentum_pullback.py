"""Strategy E: momentum + trend + regime, entered on a controlled pullback with confirmation.

P20 (default): touch the 20-DMA within the last 5 sessions, never close below the 50-DMA,
pullback depth <= 3 ATR, then confirm with a close above yesterday's high and above SMA20.
P50: touch the 50-DMA, floor is the 200-DMA.
"""
from __future__ import annotations

import pandas as pd  # panels

from ..data.market_data import MarketData  # input data
from .interface import Features, Strategy, base_setup  # contract + shared setup


class MomentumPullback(Strategy):
    """E: buy strength after a controlled dip rather than at the breakout."""

    name = "E"
    use_regime_filter = True  # same regime rule as C

    def __init__(self, cfg, ma: int | None = None):
        super().__init__(cfg)
        self.ma = int(ma or cfg["entry"]["pullback_ma_options"][0])  # 20 by default

    def entries(self, md: MarketData, f: Features) -> pd.DataFrame:
        close, high, low = md.close, md.high, md.low  # price panels
        touch_ma = f.sma20 if self.ma == 20 else f.sma50  # moving average to pull back to
        floor_ma = f.sma50 if self.ma == 20 else f.sma200  # must not close below this
        setup = base_setup(md, f, self.cfg, trend_variant=self.cfg["trend"]["variant"])  # strong + trending
        touched = (low <= touch_ma)  # low reached the moving average that day
        touch_recent = touched.shift(1).rolling(5, min_periods=1).max().fillna(0).astype(bool)  # any of t-5..t-1
        controlled = close.rolling(6, min_periods=6).min() >= floor_ma  # no close below the floor in t-5..t
        max_h20 = high.rolling(20, min_periods=20).max()  # recent swing high (incl. today)
        min_l5 = low.rolling(5, min_periods=5).min()  # pullback low over the last 5 sessions
        depth_ok = (max_h20 - min_l5) <= 3 * f.atr  # shallow, controlled dip (<= 3 ATR)
        confirm = (close > high.shift(1)) & (close > f.sma20)  # recovery confirmation today
        setup_before = setup.shift(5).fillna(False).astype(bool) | setup  # strong at start of the pullback or now
        return (setup_before & f.eligible & touch_recent & controlled & depth_ok & confirm).fillna(False).astype(bool)

"""Strategy D: momentum + trend + regime + breakout above the prior N-day high."""
from __future__ import annotations

import pandas as pd  # panels

from ..data.market_data import MarketData  # input data
from . import indicators as ind  # rolling high
from .interface import Features, Strategy, base_setup  # contract + shared setup


class MomentumBreakout(Strategy):
    """D: close today > highest high of the previous N sessions (today's high excluded)."""

    name = "D"
    use_regime_filter = True  # same regime rule as C

    def __init__(self, cfg, breakout_days: int | None = None):
        super().__init__(cfg)
        self.n = int(breakout_days or cfg["entry"]["breakout_days_options"][1])  # default 50

    def entries(self, md: MarketData, f: Features) -> pd.DataFrame:
        setup = base_setup(md, f, self.cfg, trend_variant=self.cfg["trend"]["variant"])  # momentum + trend
        breakout = md.close > ind.rolling_high(md.high, self.n)  # new N-day closing breakout
        return (setup & breakout).fillna(False).astype(bool)

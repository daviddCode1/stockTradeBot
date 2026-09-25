"""Strategies B (momentum + trend) and C (momentum + trend + market regime = MT-1 primary)."""
from __future__ import annotations

import pandas as pd  # panels

from ..data.market_data import MarketData  # input data
from .interface import Features, Strategy, base_setup  # contract + shared setup


class MomentumTrend(Strategy):
    """B: momentum setup + trend filter (variant from settings, default T3 = both conditions)."""

    name = "B"

    def entries(self, md: MarketData, f: Features) -> pd.DataFrame:
        return base_setup(md, f, self.cfg, trend_variant=self.cfg["trend"]["variant"])


class MomentumTrendRegime(MomentumTrend):
    """C / MT-1: B plus the market-regime rule (applied by the engine: block or reduce in BEAR)."""

    name = "C"
    use_regime_filter = True  # engine applies settings.regime.variant

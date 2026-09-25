"""Strategy A (pure momentum) and S0 (classic monthly 12-1 momentum benchmark)."""
from __future__ import annotations

import pandas as pd  # panels

from ..data.market_data import MarketData  # input data
from .interface import Features, Strategy, base_setup  # contract + shared setup


class PureMomentum(Strategy):
    """A: top-ranked momentum with absolute/RS filters, no trend filter, no regime filter."""

    name = "A"

    def entries(self, md: MarketData, f: Features) -> pd.DataFrame:
        return base_setup(md, f, self.cfg, trend_variant="T0")  # T0 = no trend filter


class ClassicMomentum(Strategy):
    """S0: hold the top-ranked names by 12-1 momentum, rebalanced at month-end, no stops/targets.

    Included as the academic benchmark: it tells us whether the swing overlay (stops,
    targets, time exits) adds or destroys value relative to plain momentum.
    """

    name = "S0"
    classic = True  # engine: disable resting stops/targets; exit on rank < exit threshold at month-end

    def entries(self, md: MarketData, f: Features) -> pd.DataFrame:
        dates = md.dates  # trading calendar
        month_end = pd.Series(dates.to_period("M"), index=dates)  # month of each date
        is_last = month_end != month_end.shift(-1)  # last trading day of each month
        top = f.eligible & (f.rank >= self.cfg["signals"]["entry_rank_min"])  # top-ranked names
        return top & is_last.to_numpy()[:, None]  # only on month-end rebalance days

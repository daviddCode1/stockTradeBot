"""Market regime: BULL when the benchmark closes above its 200-day SMA, else BEAR."""
from __future__ import annotations

import pandas as pd  # series

from .indicators import sma  # moving average


def bull_regime(benchmark_close: pd.Series, n: int = 200) -> pd.Series:
    """Boolean series; False (BEAR) until enough history exists - conservative default."""
    ma = sma(benchmark_close, n)  # 200-day SMA
    return (benchmark_close > ma).fillna(False)  # NaN (warm-up) => not bull

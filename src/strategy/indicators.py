"""Technical indicators. Every function is strictly backward-looking (value at t uses data <= t)."""
from __future__ import annotations

import numpy as np  # numerics
import pandas as pd  # rolling windows


def sma(close: pd.DataFrame | pd.Series, n: int) -> pd.DataFrame | pd.Series:
    """Simple moving average of the last n closes (NaN until n observations exist)."""
    return close.rolling(n, min_periods=n).mean()  # full window required


def total_return(adj_close: pd.DataFrame | pd.Series, a: int, b: int) -> pd.DataFrame | pd.Series:
    """R(t; a, b) = P[t-a] / P[t-b] - 1  (a < b). a=0 means 'up to today'."""
    return adj_close.shift(a) / adj_close.shift(b) - 1.0  # shift(k) = value k rows earlier


def momentum(adj_close: pd.DataFrame | pd.Series, kind: str, lookbacks: dict[str, int], skip: int = 21):
    """Momentum by name: 'MOM_63', 'MOM_126', 'MOM_189', 'MOM_252' or 'MOM_12_1'."""
    if kind == "MOM_12_1":  # classic: 12-month return skipping the most recent month
        return total_return(adj_close, skip, lookbacks["m12"])
    n = int(kind.split("_")[1])  # e.g. MOM_126 -> 126
    return total_return(adj_close, 0, n)


def true_range(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """TR = max(H-L, |H-C_prev|, |L-C_prev|)."""
    prev = close.shift(1)  # yesterday's close
    a = high - low  # intraday range
    b = (high - prev).abs()  # gap up component
    c = (low - prev).abs()  # gap down component
    return np.maximum(np.maximum(a, b), c)  # element-wise maximum (keeps NaN where inputs NaN)


def atr_wilder(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """Wilder's ATR: seeded with the n-day mean of TR, then ATR_t = ATR_{t-1} + (TR_t - ATR_{t-1})/n.

    Wilder smoothing is an exponential average with alpha = 1/n. We seed each column with
    the simple mean of its first n TR values so the result matches the textbook definition.
    """
    tr = true_range(high, low, close)  # daily true range
    out = pd.DataFrame(np.nan, index=tr.index, columns=tr.columns)  # result container
    alpha = 1.0 / n  # Wilder smoothing factor
    for col in tr.columns:  # per stock (different start dates)
        s = tr[col].to_numpy(dtype=float)  # raw TR values
        valid = np.flatnonzero(~np.isnan(s))  # indices with data
        if len(valid) < n:  # not enough history
            continue
        first = valid[0]  # first bar with a TR
        seed_end = first + n  # seed window [first, first+n)
        res = np.full(len(s), np.nan)
        res[seed_end - 1] = np.nanmean(s[first:seed_end])  # seed with simple mean
        for k in range(seed_end, len(s)):  # recursive smoothing
            x = s[k]
            res[k] = res[k - 1] if np.isnan(x) else res[k - 1] + alpha * (x - res[k - 1])  # carry through gaps
        out[col] = res
    return out


def rolling_high(high: pd.DataFrame, n: int) -> pd.DataFrame:
    """Highest high of the PREVIOUS n sessions (excludes today): max(H[t-n..t-1])."""
    return high.shift(1).rolling(n, min_periods=n).max()


def rolling_low(low: pd.DataFrame, n: int, include_today: bool = True) -> pd.DataFrame:
    """Lowest low over n sessions (optionally including today)."""
    src = low if include_today else low.shift(1)
    return src.rolling(n, min_periods=n).min()

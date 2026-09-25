"""Earnings calendar helpers.

Rule (settings: events.*): no NEW entry if a scheduled earnings date falls within the next
`blackout_days` trading days. In live/paper mode an unknown earnings date makes the stock
ineligible (fail safe). In backtests, historical dates from free sources are incomplete, so the
behaviour for unknown dates is configurable and always reported.
"""
from __future__ import annotations

import numpy as np  # vectorised date search
import pandas as pd  # timestamps


def next_earnings_within(earn_dates: np.ndarray | None, dates: pd.DatetimeIndex, i: int, blackout_days: int) -> bool | None:
    """True if an earnings date falls in trading days (i, i+blackout_days]; None if calendar unknown.

    `dates` is the trading calendar; only the *calendar* of future days is used (not prices),
    which is information available in advance - earnings dates are published beforehand.
    """
    if earn_dates is None or len(earn_dates) == 0:  # no calendar for this stock
        return None
    start = dates[i]  # decision day (after close)
    end_idx = min(i + blackout_days, len(dates) - 1)  # last trading day inside the window
    if i + blackout_days <= len(dates) - 1:  # window fully inside known calendar
        end = dates[end_idx]
    else:  # live: the window extends past the last bar -> use business days (conservative: holidays ignored)
        end = start + pd.offsets.BDay(blackout_days)
    pos = np.searchsorted(earn_dates, np.datetime64(start), side="right")  # first date after today
    if pos >= len(earn_dates):  # no known future date
        return False if earn_dates[-1] >= np.datetime64(start - pd.Timedelta(days=100)) else None
    return bool(earn_dates[pos] <= np.datetime64(end))  # inside the blackout window?

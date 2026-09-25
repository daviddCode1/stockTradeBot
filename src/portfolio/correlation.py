"""Correlation concentration control.

Momentum portfolios drift into one crowded cluster (e.g. several semiconductor names).
Reject a candidate whose 60-day daily-return correlation exceeds the threshold with
more than `max_highly_correlated` current holdings.
"""
from __future__ import annotations

import numpy as np  # correlation maths


def correlation_with_holdings(window: np.ndarray, cand_col: int, hold_cols: list[int], min_obs: int = 40) -> np.ndarray:
    """Pearson correlation between the candidate's returns and each holding's, NaN-aware.

    `window` is a (days x stocks) array of daily log returns ending at the decision day.
    """
    if not hold_cols:
        return np.array([])  # nothing held => nothing to compare
    c = window[:, cand_col]  # candidate return series
    out = np.full(len(hold_cols), np.nan)  # result per holding
    for k, h in enumerate(hold_cols):
        x = window[:, h]  # holding return series
        ok = np.isfinite(c) & np.isfinite(x)  # overlapping observations only
        if ok.sum() < min_obs:  # too little data => unknown
            continue
        cx, xx = c[ok] - c[ok].mean(), x[ok] - x[ok].mean()  # demeaned
        den = np.sqrt((cx ** 2).sum() * (xx ** 2).sum())  # normaliser
        out[k] = (cx * xx).sum() / den if den > 0 else np.nan
    return out


def passes_correlation_gate(corrs: np.ndarray, threshold: float, max_highly_correlated: int) -> bool:
    """True if the number of holdings with corr > threshold is within the allowed count."""
    if corrs.size == 0:
        return True
    n_high = int(np.sum(np.nan_to_num(corrs, nan=0.0) > threshold))  # unknown correlations don't count
    return n_high <= max_highly_correlated

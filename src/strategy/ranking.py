"""Cross-sectional percentile ranking (1.0 = strongest, 0.0 = weakest)."""
from __future__ import annotations

import pandas as pd  # panels


def percentile_rank(signal: pd.DataFrame, eligible: pd.DataFrame, min_names: int = 50) -> pd.DataFrame:
    """rank_pct = (r - 1) / (N - 1) among eligible names each day; ties get the average rank.

    Rows with fewer than `min_names` eligible stocks return NaN (ranks would be unreliable).
    """
    masked = signal.where(eligible)  # ineligible names do not participate
    r = masked.rank(axis=1, method="average", ascending=True)  # 1 = lowest value
    n = masked.notna().sum(axis=1)  # eligible count per day
    pct = r.sub(1.0).div((n - 1).where(n > 1), axis=0)  # scale to [0, 1]
    pct[n < min_names] = float("nan")  # too few names => no ranking that day
    return pct

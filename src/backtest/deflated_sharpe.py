"""Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014).

Testing many variants inflates the best Sharpe by luck. The DSR is the probability that the
observed Sharpe exceeds the Sharpe you would expect from the best of N skill-less trials.
"""
from __future__ import annotations

import math  # sqrt/log

import numpy as np  # arrays
from scipy.stats import norm  # normal distribution

EULER_GAMMA = 0.5772156649015329  # Euler-Mascheroni constant


def expected_max_sharpe(n_trials: int, sharpe_std: float) -> float:
    """Expected maximum of N independent Sharpe estimates with zero true skill (per-period units)."""
    if n_trials <= 1:
        return 0.0
    z1 = norm.ppf(1 - 1.0 / n_trials)  # quantile for the max of N normals
    z2 = norm.ppf(1 - 1.0 / (n_trials * math.e))
    return sharpe_std * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def probabilistic_sharpe(sr: float, sr_benchmark: float, n_obs: int, skew: float, kurt: float) -> float:
    """P(true SR > benchmark SR) given the estimation error of SR (per-period Sharpe, non-normal returns)."""
    if n_obs < 2:
        return float("nan")
    kurt_full = kurt + 3.0  # pandas kurt() is excess kurtosis
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt_full - 1) / 4.0 * sr ** 2))  # SR standard error factor
    return float(norm.cdf((sr - sr_benchmark) * math.sqrt(n_obs - 1) / denom))


def deflated_sharpe(daily_returns: np.ndarray, n_trials: int, trial_sharpes_daily: np.ndarray | None = None) -> dict[str, float]:
    """DSR for daily returns. `trial_sharpes_daily` = per-period Sharpes of all trials (for their dispersion)."""
    r = np.asarray(daily_returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 30 or r.std(ddof=1) == 0:
        return {"dsr": float("nan")}
    sr = r.mean() / r.std(ddof=1)  # per-period (daily) Sharpe
    skew = float(((r - r.mean()) ** 3).mean() / r.std() ** 3)
    kurt = float(((r - r.mean()) ** 4).mean() / r.std() ** 4) - 3.0
    if trial_sharpes_daily is not None and len(trial_sharpes_daily) > 1:
        sd = float(np.nanstd(trial_sharpes_daily, ddof=1))  # dispersion across trials
    else:
        sd = 1.0 / math.sqrt(len(r))  # fallback: sampling error of SR
    sr0 = expected_max_sharpe(max(1, n_trials), sd)  # luck benchmark
    return {"sharpe_daily": sr, "sharpe_annual": sr * math.sqrt(252), "sr0_daily": sr0, "n_trials": n_trials,
            "dsr": probabilistic_sharpe(sr, sr0, len(r), skew, kurt)}

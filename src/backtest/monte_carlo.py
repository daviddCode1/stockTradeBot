"""Monte Carlo resampling. A SIMULATION of alternative orderings - NOT a prediction of the future."""
from __future__ import annotations

import numpy as np  # random sampling
import pandas as pd  # results


def _max_dd(path: np.ndarray) -> float:
    """Max drawdown of a single equity path."""
    peak = np.maximum.accumulate(path)  # running maximum
    return float((path / peak - 1.0).min())


def _longest_losing(r: np.ndarray) -> int:
    """Longest streak of losing trades."""
    best = cur = 0
    for x in r:
        cur = cur + 1 if x <= 0 else 0
        best = max(best, cur)
    return best


def bootstrap_trades(r_multiples: np.ndarray, risk_per_trade: float = 0.005, n_iter: int = 10000,
                     block: int = 20, seed: int = 42) -> dict[str, float]:
    """Block-bootstrap trade R-multiples into equity paths (each trade risks `risk_per_trade` of equity)."""
    r = np.asarray(r_multiples, dtype=float)
    r = r[np.isfinite(r)]  # drop undefined R values
    n = len(r)
    if n < 10:  # too few trades for a meaningful simulation
        return {"n_trades": n}
    rng = np.random.default_rng(seed)
    dds, streaks, finals = np.empty(n_iter), np.empty(n_iter), np.empty(n_iter)
    for k in range(n_iter):
        idx = []  # stitched block indices
        while len(idx) < n:
            s = rng.integers(0, n)  # random block start
            idx.extend(((s + np.arange(block)) % n).tolist())  # circular block keeps streaks together
        sample = r[np.array(idx[:n])]
        path = np.cumprod(1.0 + risk_per_trade * sample)  # compounding equity (fraction of start)
        dds[k], streaks[k], finals[k] = _max_dd(np.concatenate([[1.0], path])), _longest_losing(sample), path[-1]
    q = lambda a, p: float(np.percentile(a, p))
    return {
        "n_trades": n, "iterations": n_iter,
        "max_dd_median": q(dds, 50), "max_dd_worst5pct": q(dds, 5),  # drawdowns are negative: p5 = worst 5%
        "losing_streak_p50": q(streaks, 50), "losing_streak_p95": q(streaks, 95),
        "final_equity_p5": q(finals, 5), "final_equity_p50": q(finals, 50), "final_equity_p95": q(finals, 95),
        "prob_loss": float((finals < 1.0).mean()),
    }


def stationary_bootstrap_returns(daily_returns: pd.Series, n_iter: int = 1000, mean_block: int = 20,
                                 seed: int = 43) -> dict[str, float]:
    """Politis-Romano stationary bootstrap of daily portfolio returns (keeps volatility clustering)."""
    r = daily_returns.dropna().to_numpy()
    n = len(r)
    if n < 60:
        return {"n_days": n}
    rng = np.random.default_rng(seed)
    p = 1.0 / mean_block  # probability of starting a new block
    dds, cagrs = np.empty(n_iter), np.empty(n_iter)
    for k in range(n_iter):
        out = np.empty(n)
        j = rng.integers(0, n)
        for t in range(n):
            if t > 0 and rng.random() < p:
                j = rng.integers(0, n)  # jump to a new random block
            out[t] = r[j]
            j = (j + 1) % n  # continue the block
        path = np.cumprod(1 + out)
        dds[k] = _max_dd(np.concatenate([[1.0], path]))
        cagrs[k] = path[-1] ** (252 / n) - 1
    return {
        "n_days": n, "iterations": n_iter,
        "max_dd_p50": float(np.percentile(dds, 50)), "max_dd_worst5pct": float(np.percentile(dds, 5)),
        "cagr_p5": float(np.percentile(cagrs, 5)), "cagr_p50": float(np.percentile(cagrs, 50)),
        "cagr_p95": float(np.percentile(cagrs, 95)),
    }

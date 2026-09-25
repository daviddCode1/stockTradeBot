"""Performance and risk metrics. Pure functions over an equity curve and a trade list."""
from __future__ import annotations

import math  # sqrt
from typing import Any  # type hints

import numpy as np  # numerics
import pandas as pd  # series

TRADING_DAYS = 252  # annualisation factor


def drawdown(equity: pd.Series) -> pd.Series:
    """Drawdown series: equity / running peak - 1 (0 at new highs, negative below)."""
    return equity / equity.cummax() - 1.0


def recovery_days(equity: pd.Series) -> int:
    """Longest time (trading days) spent below a previous peak."""
    under = (equity < equity.cummax()).to_numpy()  # True while in drawdown
    longest = cur = 0
    for u in under:  # count consecutive underwater days
        cur = cur + 1 if u else 0
        longest = max(longest, cur)
    return longest


def max_consecutive(flags: np.ndarray) -> int:
    """Longest run of True values (e.g. losing trades)."""
    best = cur = 0
    for x in flags:
        cur = cur + 1 if x else 0
        best = max(best, cur)
    return best


def equity_metrics(equity: pd.Series, rf: float = 0.0) -> dict[str, float]:
    """Return/risk statistics of a daily equity curve."""
    equity = equity.dropna()
    if len(equity) < 2:
        return {}
    rets = equity.pct_change().dropna()  # daily returns
    years = len(rets) / TRADING_DAYS  # elapsed years
    total = equity.iloc[-1] / equity.iloc[0] - 1.0  # total return
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else float("nan")  # compound growth
    vol = rets.std(ddof=1) * math.sqrt(TRADING_DAYS)  # annualised volatility
    downside = rets[rets < 0].std(ddof=1) * math.sqrt(TRADING_DAYS)  # downside deviation
    sharpe = (rets.mean() * TRADING_DAYS - rf) / vol if vol > 0 else float("nan")
    sortino = (rets.mean() * TRADING_DAYS - rf) / downside if downside and downside > 0 else float("nan")
    mdd = float(drawdown(equity).min())  # worst peak-to-trough
    weekly = equity.resample("W-FRI").last().pct_change().dropna()  # weekly returns
    monthly = equity.resample("ME").last().pct_change().dropna()  # monthly returns
    return {
        "total_return": total, "cagr": cagr, "volatility": vol, "sharpe": sharpe, "sortino": sortino,
        "max_drawdown": mdd, "calmar": cagr / abs(mdd) if mdd < 0 else float("nan"),
        "worst_day": float(rets.min()), "worst_week": float(weekly.min()) if len(weekly) else float("nan"),
        "worst_month": float(monthly.min()) if len(monthly) else float("nan"),
        "recovery_days_max": recovery_days(equity), "years": years,
        "daily_skew": float(rets.skew()), "daily_kurtosis": float(rets.kurt()),
    }


def trade_metrics(trades: pd.DataFrame) -> dict[str, float]:
    """Trade-level statistics (R multiples, win rate, profit factor, streaks)."""
    if trades is None or trades.empty:
        return {"n_trades": 0}
    t = trades[trades["reason"] != "END_OF_TEST"] if "reason" in trades else trades  # exclude forced closes
    if t.empty:
        return {"n_trades": 0}
    pnl, r = t["pnl_gbp"], t["r"].replace([np.inf, -np.inf], np.nan)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    return {
        "n_trades": int(len(t)),
        "win_rate": float((pnl > 0).mean()),
        "avg_win_gbp": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_gbp": float(losses.mean()) if len(losses) else 0.0,
        "expectancy_gbp": float(pnl.mean()),
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_r": float(r.mean()), "median_r": float(r.median()),
        "best_r": float(r.max()), "worst_r": float(r.min()),
        "largest_loss_gbp": float(pnl.min()),
        "max_consecutive_losses": max_consecutive((pnl <= 0).to_numpy()),
        "avg_holding_days": float(t["days"].mean()),
    }


def turnover(trades: pd.DataFrame, equity: pd.Series) -> float:
    """Annual one-way turnover: traded GBP value (buys) per year / average equity."""
    if trades is None or trades.empty or len(equity) < 2:
        return 0.0
    buys_gbp = trades["cost_gbp"].sum()  # GBP spent on entries (incl. fees)
    years = len(equity) / TRADING_DAYS
    return float(buys_gbp / years / equity.mean())


def period_returns(equity: pd.Series, freq: str) -> pd.Series:
    """Calendar-period returns ('ME' monthly, 'YE' yearly)."""
    return equity.resample(freq).last().pct_change().dropna()


def summarize(result: Any) -> dict[str, float]:
    """All headline metrics for one BacktestResult."""
    out = equity_metrics(result.equity)
    out.update(trade_metrics(result.trades))
    out["turnover"] = turnover(result.trades, result.equity)
    out["avg_exposure"] = float(result.exposure.mean())
    out["avg_heat"] = float(result.heat.mean())
    out["max_heat"] = float(result.heat.max())
    out["avg_positions"] = float(result.positions_count.mean())
    return out

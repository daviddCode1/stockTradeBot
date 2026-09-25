"""Walk-forward validation, parameter-stability and sensitivity testing.

Procedure (settings.walk_forward):
  1. Seal the final `holdout_months` - never used for any choice.
  2. Run every pre-registered parameter set once over the pre-holdout period.
  3. Rolling folds: TRAIN 5y -> VALIDATE 1y -> TEST 1y, step 1y.
     - TRAIN: pick the set with the best *neighbourhood median* Sharpe (rewards plateaus,
       not the single best result), requiring a minimum number of trades.
     - VALIDATE: keep the pick only if expectancy > 0 and PF > 1, else fall back to base params.
     - TEST: record only. Test years are chained into one out-of-sample equity curve.
  4. Holdout: run the final pick once on the sealed period.
Every run is appended to a trial log so the Deflated Sharpe uses the true number of trials.
"""
from __future__ import annotations

import itertools  # grids
import json  # trial log
from dataclasses import dataclass, field  # results
from pathlib import Path  # trial log path
from typing import Any, Callable, Mapping  # type hints

import numpy as np  # numerics
import pandas as pd  # series

from ..config import deep_update  # parameter overrides
from ..data.market_data import MarketData  # data
from ..strategy.interface import compute_features  # features
from .costs import CostModel  # costs
from .engine import BacktestResult, run_backtest  # simulator
from .metrics import equity_metrics, trade_metrics  # statistics

# Pre-registered grid (fixed before looking at results). Keys are dotted settings paths.
DEFAULT_GRID: dict[str, list] = {
    "signals.primary_signal": ["MOM_126", "MOM_252", "MOM_12_1"],
    "stops.atr_multiple": [2.0, 2.5, 3.0],
    "targets.r_multiple": [2.0, 2.5, 3.0],
}


def _nest(dotted: Mapping[str, Any]) -> dict:
    """{'a.b': 1} -> {'a': {'b': 1}} (for deep_update)."""
    out: dict = {}
    for k, v in dotted.items():
        cur = out
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    return out


def param_key(params: Mapping[str, Any]) -> str:
    """Stable string id for a parameter set."""
    return json.dumps(dict(sorted(params.items())), default=str)


@dataclass
class GridRun:
    """One parameter set's continuous backtest (sliced later by window)."""

    params: dict  # dotted-path overrides
    result: BacktestResult  # full-period result


@dataclass
class WalkForwardResult:
    """Outputs of the whole walk-forward study for one strategy."""

    strategy: str
    folds: pd.DataFrame  # one row per fold: chosen params and train/val/test metrics
    oos_equity: pd.Series  # chained out-of-sample equity (test years only)
    oos_trades: pd.DataFrame  # trades entered in test years
    grid_runs: list = field(default_factory=list)  # every GridRun
    n_trials: int = 0  # number of parameter sets evaluated
    final_params: dict = field(default_factory=dict)  # pick from the last fold (for the holdout)


def window_stats(res: BacktestResult, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, float]:
    """Metrics for [start, end) from a continuous run: equity slice + trades entered in the window."""
    eq = res.equity[(res.equity.index >= start) & (res.equity.index < end)]
    out = equity_metrics(eq) if len(eq) > 20 else {}
    tr = res.trades
    if tr is not None and not tr.empty:
        tr = tr[(tr["entry_date"] >= start) & (tr["entry_date"] < end)]
    out.update(trade_metrics(tr))
    out["expectancy_r"] = out.get("avg_r", float("nan"))
    return out


def neighbours(params: dict, grid: Mapping[str, list]) -> list[str]:
    """Keys of grid points that differ from `params` in exactly one dimension by one step."""
    keys = []
    for dim, values in grid.items():
        idx = values.index(params[dim])
        for j in (idx - 1, idx + 1):
            if 0 <= j < len(values):
                q = dict(params)
                q[dim] = values[j]
                keys.append(param_key(q))
    return keys


def run_grid(md: MarketData, strategy_factory: Callable[[Mapping], Any], cfg: Mapping[str, Any],
             grid: Mapping[str, list], end: pd.Timestamp, log_path: Path | None = None,
             progress: Callable[[str], None] | None = None) -> list[GridRun]:
    """Run every grid point once up to `end` (features are cached per signal definition)."""
    dims = list(grid)
    runs: list[GridRun] = []
    feat_cache: dict[str, Any] = {}  # features depend only on signal/trend/universe settings
    for combo in itertools.product(*(grid[d] for d in dims)):
        params = dict(zip(dims, combo))
        c = deep_update(cfg, _nest(params))
        fkey = json.dumps({k: c[k] for k in ("signals", "trend", "universe", "volatility", "regime")}, sort_keys=True, default=str)
        if fkey not in feat_cache:
            feat_cache[fkey] = compute_features(md, c)
        strat = strategy_factory(c)
        res = run_backtest(md, strat, c, CostModel.from_settings(c), end=str(end.date()), features=feat_cache[fkey])
        runs.append(GridRun(params, res))
        if progress:
            progress(f"{strat.name} {params}")
        if log_path is not None:  # append-only trial log (honest trial count)
            m = equity_metrics(res.equity)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"strategy": strat.name, "params": params, "sharpe": m.get("sharpe"),
                                     "cagr": m.get("cagr"), "mdd": m.get("max_drawdown")}, default=str) + "\n")
    return runs


def walk_forward(md: MarketData, strategy_factory: Callable[[Mapping], Any], cfg: Mapping[str, Any],
                 grid: Mapping[str, list] | None = None, base_params: dict | None = None,
                 log_path: Path | None = None, progress: Callable[[str], None] | None = None) -> WalkForwardResult:
    """Full walk-forward study for one strategy variant."""
    grid = dict(grid or DEFAULT_GRID)
    wf = cfg["walk_forward"]
    dates = md.dates
    holdout_start = dates[-1] - pd.DateOffset(months=int(cfg["backtest"]["holdout_months"]))  # sealed period
    runs = run_grid(md, strategy_factory, cfg, grid, end=holdout_start, log_path=log_path, progress=progress)
    by_key = {param_key(r.params): r for r in runs}
    base = base_params or {d: _get(cfg, d) for d in grid}  # pre-registered base
    if param_key(base) not in by_key:  # base must be a grid point
        base = {d: grid[d][len(grid[d]) // 2] for d in grid}
    first = max(pd.Timestamp(cfg["backtest"]["start"]), dates[0]) + pd.DateOffset(years=1)  # 1y warm-up for 12m signals
    rows, oos_parts, oos_trades = [], [], []
    tr0 = first
    final_params = base
    while True:
        tr1 = tr0 + pd.DateOffset(years=int(wf["train_years"]))
        va1 = tr1 + pd.DateOffset(years=int(wf["validate_years"]))
        te1 = va1 + pd.DateOffset(years=int(wf["test_years"]))
        if te1 > holdout_start:
            break
        train = {k: window_stats(r.result, tr0, tr1) for k, r in by_key.items()}  # train metrics per set
        def score(k: str) -> float:  # neighbourhood median Sharpe
            if train[k].get("n_trades", 0) < int(wf["min_train_trades"]):
                return -np.inf
            vals = [train[k].get("sharpe", np.nan)] + [train[n].get("sharpe", np.nan) for n in neighbours(by_key[k].params, grid) if n in train]
            return float(np.nanmedian(vals)) if len(vals) else -np.inf
        best = max(by_key, key=score)
        pick = by_key[best].params if np.isfinite(score(best)) else base
        val = window_stats(by_key[param_key(pick)].result, tr1, va1)
        if not (val.get("expectancy_r", -1) > 0 and val.get("profit_factor", 0) > 1):  # validation failed
            pick = base
        chosen = by_key[param_key(pick)].result
        test = window_stats(chosen, va1, te1)
        eq = chosen.equity[(chosen.equity.index >= va1) & (chosen.equity.index < te1)]
        oos_parts.append(eq.pct_change().fillna(0.0))  # daily returns of the test year
        if chosen.trades is not None and not chosen.trades.empty:
            t = chosen.trades
            oos_trades.append(t[(t["entry_date"] >= va1) & (t["entry_date"] < te1)])
        rows.append({"train_start": tr0.date(), "test_start": va1.date(), "test_end": te1.date(), "params": pick,
                     "train_sharpe": train[param_key(pick)].get("sharpe"), "val_expectancy_r": val.get("expectancy_r"),
                     "test_return": test.get("total_return"), "test_sharpe": test.get("sharpe"),
                     "test_trades": test.get("n_trades"), "test_expectancy_r": test.get("expectancy_r"),
                     "test_pf": test.get("profit_factor"), "test_mdd": test.get("max_drawdown")})
        final_params = pick
        tr0 = tr0 + pd.DateOffset(years=int(wf["step_years"]))
    rets = pd.concat(oos_parts) if oos_parts else pd.Series(dtype=float)
    oos_eq = (1 + rets).cumprod() * float(cfg["backtest"]["initial_equity_gbp"])
    trades = pd.concat(oos_trades, ignore_index=True) if oos_trades else pd.DataFrame()
    name = strategy_factory(cfg).name
    return WalkForwardResult(name, pd.DataFrame(rows), oos_eq, trades, runs, len(runs), final_params)


def _get(cfg: Mapping[str, Any], dotted: str) -> Any:
    """Read a dotted settings path."""
    cur: Any = cfg
    for p in dotted.split("."):
        cur = cur[p]
    return cur


def sensitivity(md: MarketData, strategy_factory: Callable[[Mapping], Any], cfg: Mapping[str, Any],
                variations: Mapping[str, list], end: pd.Timestamp | None = None,
                progress: Callable[[str], None] | None = None) -> pd.DataFrame:
    """One-factor-at-a-time sensitivity around the base settings (robustness, NOT optimisation)."""
    rows = []
    for dim, values in variations.items():
        for v in values:
            c = deep_update(cfg, _nest({dim: v}))
            res = run_backtest(md, strategy_factory(c), c, CostModel.from_settings(c),
                               end=str(end.date()) if end is not None else None)
            m = equity_metrics(res.equity)
            m.update(trade_metrics(res.trades))
            rows.append({"param": dim, "value": str(v), **{k: m.get(k) for k in
                         ("cagr", "sharpe", "max_drawdown", "n_trades", "avg_r", "profit_factor", "win_rate")}})
            if progress:
                progress(f"sensitivity {dim}={v}")
    return pd.DataFrame(rows)


def cost_stress(md: MarketData, strategy_factory: Callable[[Mapping], Any], cfg: Mapping[str, Any],
                end: pd.Timestamp | None = None) -> pd.DataFrame:
    """Re-run the base strategy with higher slippage and FX fees."""
    rows = []
    f = compute_features(md, cfg)
    for mult, fx in [(1, None), (2, None), (3, None), (2, cfg["costs"]["stress"]["fx_fee_per_side_stress"])]:
        costs = CostModel.from_settings(cfg, slippage_mult=mult, fx_fee=fx)
        res = run_backtest(md, strategy_factory(cfg), cfg, costs, end=str(end.date()) if end is not None else None, features=f)
        m = equity_metrics(res.equity)
        m.update(trade_metrics(res.trades))
        rows.append({"slippage_x": mult, "fx_fee": costs.fx_fee, **{k: m.get(k) for k in
                     ("cagr", "sharpe", "max_drawdown", "avg_r", "profit_factor", "n_trades")}})
    return pd.DataFrame(rows)

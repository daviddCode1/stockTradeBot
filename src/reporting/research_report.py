"""End-to-end research study -> reports/research/<date>/report.md (+ charts, JSON).

Runs, for strategies S0, A, B, C, D, E under identical universe/costs/risk/portfolio rules:
  in-sample backtest, walk-forward OOS, sensitivity (primary), cost stress, Monte Carlo,
  Deflated Sharpe, regime buckets, sealed holdout, benchmark comparison, and the
  pre-registered acceptance test. The verdict is mechanical: if nothing passes, DO NOT DEPLOY.
"""
from __future__ import annotations

import json  # machine-readable output
import math  # finite checks
from datetime import date  # folder naming
from pathlib import Path  # output paths
from typing import Any, Callable, Mapping  # type hints

import numpy as np  # numerics
import pandas as pd  # tables

from ..backtest.costs import CostModel  # costs
from ..backtest.deflated_sharpe import deflated_sharpe  # overfitting control
from ..backtest.engine import run_backtest  # simulator
from ..backtest.metrics import equity_metrics, period_returns, summarize, trade_metrics  # stats
from ..backtest.monte_carlo import bootstrap_trades, stationary_bootstrap_returns  # simulation
from ..backtest.walk_forward import DEFAULT_GRID, cost_stress, sensitivity, walk_forward  # validation
from ..config import deep_update  # overrides
from ..data.market_data import MarketData  # data
from ..strategy.interface import compute_features  # features
from ..strategy.momentum import ClassicMomentum, PureMomentum  # S0, A
from ..strategy.momentum_breakout import MomentumBreakout  # D
from ..strategy.momentum_pullback import MomentumPullback  # E
from ..strategy.momentum_trend import MomentumTrend, MomentumTrendRegime  # B, C

FACTORIES: dict[str, Callable[[Mapping], Any]] = {
    "S0": ClassicMomentum, "A": PureMomentum, "B": MomentumTrend, "C": MomentumTrendRegime,
    "D": MomentumBreakout, "E": MomentumPullback,
}
S0_GRID = {"signals.primary_signal": DEFAULT_GRID["signals.primary_signal"]}  # S0 has no stops/targets to vary


def _fmt(x: Any, pct: bool = False, nd: int = 2) -> str:
    """Pretty number for markdown tables."""
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    if isinstance(x, (int, np.integer)):
        return str(int(x))
    return f"{x * 100:.{nd - 1}f}%" if pct else f"{x:.{nd}f}"


def _table(rows: list[dict], cols: list[tuple[str, str, bool]]) -> str:
    """Markdown table from dict rows; cols = (key, header, is_pct)."""
    head = "| " + " | ".join(h for _, h, _ in cols) + " |\n|" + "---|" * len(cols) + "\n"
    body = "".join("| " + " | ".join(_fmt(r.get(k), p) if not isinstance(r.get(k), str) else r.get(k)
                                     for k, _, p in cols) + " |\n" for r in rows)
    return head + body


def benchmark_equity(md: MarketData, start: pd.Timestamp, end: pd.Timestamp, initial: float) -> pd.Series:
    """SPY total-return buy & hold, converted to GBP (a GBP investor bears the FX move too)."""
    px = md.benchmark_adj.loc[start:end] / md.fx_usd_per_gbp.loc[start:end]
    return px / px.iloc[0] * initial


def equal_weight_equity(md: MarketData, eligible: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp,
                        initial: float) -> pd.Series:
    """Equal-weight eligible universe, daily-rebalanced proxy (ignores costs), in GBP."""
    r = md.adj_close.pct_change()
    w = eligible.shift(1).astype(float)  # yesterday's eligibility (no look-ahead)
    ew = (r * w).sum(axis=1) / w.sum(axis=1).replace(0, np.nan)
    fxr = md.fx_usd_per_gbp.pct_change()
    gbp = (1 + ew.fillna(0)) / (1 + fxr.fillna(0)) - 1
    gbp = gbp.loc[start:end]
    return (1 + gbp).cumprod() * initial


def acceptance(oos: dict, stress: dict | None, mc: dict, dsr: dict, folds: pd.DataFrame, bench: dict,
               neighbour_frac: float | None, holdout: dict, regimes: pd.DataFrame | None, cfg: Mapping[str, Any]) -> list[dict]:
    """Evaluate every pre-registered acceptance criterion. Returns rows with PASS/FAIL."""
    a = cfg["acceptance"]
    rows = []
    def add(name: str, value: Any, ok: bool | None, rule: str):
        rows.append({"criterion": name, "value": value if isinstance(value, str) else _fmt(value), "rule": rule,
                     "result": "PASS" if ok else ("FAIL" if ok is not None else "N/A")})
    n = oos.get("n_trades", 0)
    add("OOS trades", n, n >= a["min_oos_trades"], f">= {a['min_oos_trades']}")
    exp_r = oos.get("avg_r", float("nan"))
    add("OOS expectancy (R)", exp_r, exp_r > a["min_expectancy_r"] if math.isfinite(exp_r) else False, f"> {a['min_expectancy_r']}")
    ci = oos.get("expectancy_ci_low", float("nan"))
    add("Expectancy 95% CI lower bound (R)", ci, ci > 0 if math.isfinite(ci) else False, "> 0")
    pf = oos.get("profit_factor", float("nan"))
    add("Profit factor (base costs)", pf, pf >= a["min_profit_factor"] if math.isfinite(pf) else False, f">= {a['min_profit_factor']}")
    spf = (stress or {}).get("profit_factor", float("nan"))
    add("Profit factor (2x slippage, 0.30% FX)", spf, spf >= a["min_profit_factor_stressed"] if math.isfinite(spf) else None,
        f">= {a['min_profit_factor_stressed']}")
    mdd = oos.get("max_drawdown", float("nan"))
    add("OOS max drawdown", _fmt(mdd, True), abs(mdd) <= a["max_drawdown"] if math.isfinite(mdd) else False, f"<= {a['max_drawdown']:.0%}")
    mcdd = mc.get("max_dd_worst5pct", float("nan"))
    add("Monte Carlo worst-5% drawdown", _fmt(mcdd, True), abs(mcdd) <= a["max_mc_p95_drawdown"] if math.isfinite(mcdd) else None,
        f"<= {a['max_mc_p95_drawdown']:.0%}")
    beat = (oos.get("sharpe", -9) > bench.get("sharpe", 9)) or (oos.get("calmar", -9) > bench.get("calmar", 9))
    add("Beats SPY buy&hold risk-adjusted (Sharpe or Calmar)",
        f"{_fmt(oos.get('sharpe'))} vs {_fmt(bench.get('sharpe'))} / {_fmt(oos.get('calmar'))} vs {_fmt(bench.get('calmar'))}",
        bool(beat), "strategy > benchmark")
    d = dsr.get("dsr", float("nan"))
    add("Deflated Sharpe probability", d, d >= a["min_deflated_sharpe_prob"] if math.isfinite(d) else False,
        f">= {a['min_deflated_sharpe_prob']}")
    if len(folds):
        pos = float((folds["test_return"].astype(float) > 0).mean())
        add("Share of positive test years", _fmt(pos, True), pos >= a["min_positive_test_year_fraction"],
            f">= {a['min_positive_test_year_fraction']:.0%}")
    share = oos.get("max_year_profit_share", float("nan"))
    add("Largest single-year share of OOS profit", _fmt(share, True),
        share <= a["max_single_year_profit_share"] if math.isfinite(share) else False, f"<= {a['max_single_year_profit_share']:.0%}")
    add("Parameter neighbourhood pass rate", _fmt(neighbour_frac, True) if neighbour_frac is not None else "n/a",
        (neighbour_frac >= a["min_neighbour_pass_fraction"]) if neighbour_frac is not None else None,
        f">= {a['min_neighbour_pass_fraction']:.0%}")
    drag = oos.get("cost_drag_share", float("nan"))
    add("Cost drag / gross return", _fmt(drag, True), (drag <= a["max_cost_drag_share"]) if math.isfinite(drag) else None,
        f"<= {a['max_cost_drag_share']:.0%}")
    if regimes is not None and len(regimes):
        bad = regimes[(regimes["n_trades"] >= 30) & (regimes["avg_r"] < -0.15)]
        add("No regime bucket with expectancy < -0.15R (n>=30)", f"{len(bad)} bad buckets", len(bad) == 0, "0 bad buckets")
    hx = holdout.get("avg_r", float("nan"))
    hdd = holdout.get("max_drawdown", float("nan"))
    add("Sealed holdout expectancy > 0 and DD <= 25%", f"{_fmt(hx)}R / {_fmt(hdd, True)}",
        (hx > 0 and abs(hdd) <= a["max_drawdown"]) if math.isfinite(hx) and math.isfinite(hdd) else False, "both")
    wr = oos.get("worst_r", float("nan"))
    add("Worst single trade >= -3R", wr, wr >= -3 if math.isfinite(wr) else False, ">= -3R")
    wm = oos.get("worst_month", float("nan"))
    add("Worst month >= -8%", _fmt(wm, True), wm >= -0.08 if math.isfinite(wm) else False, ">= -8%")
    return rows


def expectancy_ci(r: pd.Series, n_iter: int = 5000, block: int = 20, seed: int = 1) -> float:
    """Lower 2.5% bound of mean R via block bootstrap (keeps clustered losses together)."""
    x = r.replace([np.inf, -np.inf], np.nan).dropna().to_numpy()
    if len(x) < 20:
        return float("nan")
    rng = np.random.default_rng(seed)
    n = len(x)
    means = np.empty(n_iter)
    for k in range(n_iter):
        starts = rng.integers(0, n, size=math.ceil(n / block))
        idx = ((starts[:, None] + np.arange(block)[None, :]) % n).ravel()[:n]
        means[k] = x[idx].mean()
    return float(np.percentile(means, 2.5))


def regime_buckets(trades: pd.DataFrame, md: MarketData, bull: pd.Series) -> pd.DataFrame:
    """Expectancy by market regime at entry: bull/bear and high/low benchmark volatility."""
    if trades is None or trades.empty:
        return pd.DataFrame()
    vol = md.benchmark_close.pct_change().rolling(20).std()
    hi = vol > vol.expanding(252).median()  # 'high vol' relative to history known at the time
    t = trades.copy()
    t["regime"] = np.where(bull.reindex(t["entry_date"]).to_numpy(), "BULL", "BEAR")
    t["vol"] = np.where(hi.reindex(t["entry_date"]).fillna(False).to_numpy(), "HIGH_VOL", "LOW_VOL")
    rows = []
    for col in ("regime", "vol"):
        for k, g in t.groupby(col):
            rows.append({"bucket": f"{col}={k}", "n_trades": len(g), "avg_r": float(g["r"].mean()),
                         "win_rate": float((g["pnl_gbp"] > 0).mean())})
    return pd.DataFrame(rows)


def run_research(md: MarketData, cfg: Mapping[str, Any], out_root: Path, strategies: list[str] | None = None,
                 progress: Callable[[str], None] = print) -> dict:
    """Execute the full study and write the report. Returns the summary dict."""
    out = out_root / str(date.today())
    out.mkdir(parents=True, exist_ok=True)
    strategies = strategies or list(FACTORIES)
    init = float(cfg["backtest"]["initial_equity_gbp"])
    start = max(pd.Timestamp(cfg["backtest"]["start"]), md.dates[0])
    holdout_start = md.dates[-1] - pd.DateOffset(months=int(cfg["backtest"]["holdout_months"]))
    trial_log = out / "trials.jsonl"
    f = compute_features(md, cfg)
    results: dict[str, dict] = {}
    all_trial_sharpes: list[float] = []

    for name in strategies:
        fac = FACTORIES[name]
        progress(f"== strategy {name}: in-sample")
        ins = run_backtest(md, fac(cfg), cfg, start=str(start.date()), end=str(holdout_start.date()), features=f)
        progress(f"== strategy {name}: walk-forward")
        wf = walk_forward(md, fac, cfg, grid=S0_GRID if name == "S0" else DEFAULT_GRID, log_path=trial_log, progress=None)
        for gr in wf.grid_runs:
            rr = gr.result.equity.pct_change().dropna()
            if rr.std() > 0:
                all_trial_sharpes.append(float(rr.mean() / rr.std()))
        oos = equity_metrics(wf.oos_equity) if len(wf.oos_equity) > 20 else {}
        oos.update(trade_metrics(wf.oos_trades))
        if not wf.oos_trades.empty:
            oos["expectancy_ci_low"] = expectancy_ci(wf.oos_trades["r"])
            by_year = wf.oos_trades.groupby(pd.to_datetime(wf.oos_trades["exit_date"]).dt.year)["pnl_gbp"].sum()
            tot = by_year[by_year > 0].sum()
            oos["max_year_profit_share"] = float(by_year.max() / tot) if tot > 0 else float("nan")
        # holdout: final pick, run once over the whole history and sliced to the sealed period
        hcfg = deep_update(cfg, _nest(wf.final_params))
        hres = run_backtest(md, fac(hcfg), hcfg, start=str(start.date()), features=None)
        hold = equity_metrics(hres.equity.loc[holdout_start:])
        ht = hres.trades[hres.trades["entry_date"] >= holdout_start] if not hres.trades.empty else hres.trades
        hold.update(trade_metrics(ht))
        results[name] = {"in_sample": summarize(ins), "oos": oos, "folds": wf.folds, "wf": wf, "holdout": hold,
                         "in_sample_result": ins, "final_params": wf.final_params, "n_trials": wf.n_trials}

    n_trials_total = sum(r["n_trials"] for r in results.values())
    oos_start = min((r["wf"].oos_equity.index[0] for r in results.values() if len(r["wf"].oos_equity)), default=start)
    oos_end = max((r["wf"].oos_equity.index[-1] for r in results.values() if len(r["wf"].oos_equity)), default=holdout_start)
    spy = benchmark_equity(md, oos_start, oos_end, init)
    bench = equity_metrics(spy)
    ew = equity_metrics(equal_weight_equity(md, f.eligible, oos_start, oos_end, init))

    for name, r in results.items():  # robustness + acceptance per strategy
        progress(f"== strategy {name}: robustness")
        fac = FACTORIES[name]
        pcfg = deep_update(cfg, _nest(r["final_params"]))
        stress = cost_stress(md, fac, pcfg, end=holdout_start)
        r["stress"] = stress
        s2 = stress[(stress["slippage_x"] == 2) & (stress["fx_fee"] > cfg["costs"]["fx_fee_per_side"])]
        stress_row = s2.iloc[0].to_dict() if len(s2) else None
        base_row, zero = stress.iloc[0].to_dict(), None
        # cost drag: rerun with zero costs to get gross performance
        zc = CostModel(fx_fee=0.0, slippage=0.0, stop_slippage=0.0, sec_rate=0.0, finra_per_share=0.0,
                       dividend_withholding=float(cfg["costs"]["dividend_withholding"]))
        gross = equity_metrics(run_backtest(md, fac(pcfg), pcfg, zc, end=str(holdout_start.date()), features=None).equity)
        if gross.get("cagr") and gross["cagr"] > 0 and base_row.get("cagr") is not None:
            r["oos"]["cost_drag_share"] = (gross["cagr"] - base_row["cagr"]) / gross["cagr"]
        wf = r["wf"]
        runs = {json.dumps(dict(sorted(g.params.items())), default=str): g for g in wf.grid_runs}
        nb_ok = [trade_metrics(g.result.trades) for g in wf.grid_runs]
        r["neighbour_frac"] = float(np.mean([(m.get("profit_factor", 0) > 1 and m.get("avg_r", -1) > 0) for m in nb_ok])) if nb_ok else None
        r["mc"] = bootstrap_trades(wf.oos_trades["r"].to_numpy() if not wf.oos_trades.empty else np.array([]),
                                   float(cfg["risk"]["risk_per_trade"]), int(cfg["monte_carlo"]["iterations"]),
                                   int(cfg["monte_carlo"]["block_size_trades"]))
        r["mc_daily"] = stationary_bootstrap_returns(wf.oos_equity.pct_change(), mean_block=int(cfg["monte_carlo"]["block_size_days"]))
        r["dsr"] = deflated_sharpe(wf.oos_equity.pct_change().dropna().to_numpy(), n_trials_total,
                                   np.array(all_trial_sharpes))
        r["regimes"] = regime_buckets(wf.oos_trades, md, f.bull)
        r["acceptance"] = acceptance(r["oos"], stress_row, r["mc"], r["dsr"], r["folds"], bench, r["neighbour_frac"],
                                     r["holdout"], r["regimes"], cfg)
        r["passed"] = all(a["result"] != "FAIL" for a in r["acceptance"])
    primary = cfg.get("strategy", {}).get("primary", "C")
    if primary in results:
        progress("== sensitivity (primary)")
        results[primary]["sensitivity"] = sensitivity(md, FACTORIES[primary], cfg, {
            "signals.primary_signal": ["MOM_63", "MOM_126", "MOM_189", "MOM_252", "MOM_12_1"],
            "trend.variant": ["T0", "T1", "T2", "T3"], "stops.method": ["ATR", "PERCENT", "SWING_LOW"],
            "stops.atr_multiple": [2.0, 2.5, 3.0], "targets.r_multiple": [2.0, 2.25, 2.5, 3.0],
            "exits.max_holding_days": [20, 40, 60], "risk.max_positions": [8, 10, 12],
            "portfolio.max_per_sector": [2, 3, 4], "portfolio.rebalance": ["DAILY", "WEEKLY"],
            "exits.addons": [[], ["TREND"], ["MOMENTUM"], ["TRAILING"], ["PARTIAL"], ["REGIME"]],
            "events.earnings_blackout_days": [1, 2, 3, 5], "regime.variant": ["R0", "R1", "R2"],
        }, end=holdout_start)
    _write_charts(results, spy, out)
    summary = _write_markdown(results, bench, ew, md, cfg, out, n_trials_total, oos_start, oos_end, holdout_start)
    return summary


def _nest(dotted: Mapping[str, Any]) -> dict:
    """{'a.b': 1} -> {'a': {'b': 1}}."""
    outd: dict = {}
    for k, v in dotted.items():
        cur = outd
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    return outd


def _write_charts(results: dict, spy: pd.Series, out: Path) -> None:
    """OOS equity curves vs SPY, and drawdowns."""
    import matplotlib
    matplotlib.use("Agg")  # headless rendering
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(2, 1, figsize=(11, 8), sharex=True)
    for name, r in results.items():
        eq = r["wf"].oos_equity
        if len(eq):
            ax[0].plot(eq.index, eq.values, label=name, lw=1.2)
            ax[1].plot(eq.index, (eq / eq.cummax() - 1).values, lw=1.0, label=name)
    ax[0].plot(spy.index, spy.values, label="SPY (GBP)", color="black", lw=1.5, ls="--")
    ax[0].set_title("Walk-forward out-of-sample equity (GBP), chained test years")
    ax[0].legend(ncol=4, fontsize=8)
    ax[1].set_title("Drawdown")
    fig.tight_layout()
    fig.savefig(out / "oos_equity.png", dpi=110)
    plt.close(fig)


def _write_markdown(results: dict, bench: dict, ew: dict, md: MarketData, cfg: Mapping[str, Any], out: Path,
                    n_trials: int, oos_start, oos_end, holdout_start) -> dict:
    """Assemble the final research report (sections mirror the specification's section 57)."""
    L: list[str] = []
    passed = [n for n, r in results.items() if r["passed"]]
    L.append("# Research report - long-only US momentum/trend on Trading 212\n")
    L.append(f"Generated {date.today()} | data {md.dates[0].date()} -> {md.dates[-1].date()} | "
             f"walk-forward OOS {pd.Timestamp(oos_start).date()} -> {pd.Timestamp(oos_end).date()} | "
             f"sealed holdout from {pd.Timestamp(holdout_start).date()} | parameter sets tested: {n_trials}\n")
    L.append("> Backtests and simulations describe the past under stated assumptions. They are not forecasts, and "
             "nothing here implies the strategy is safe or profitable.\n")
    L.append("## Verdict\n")
    if passed:
        L.append(f"Strategies meeting ALL pre-registered criteria: **{', '.join(passed)}**. They qualify for PAPER "
                 "trading only (engineering + live-behaviour validation). Live trading remains disabled.\n")
    else:
        L.append("**No strategy met all pre-registered acceptance criteria. The research did NOT establish sufficient "
                 "evidence of an edge. Recommendation: DO NOT DEPLOY with real money.** Paper trading may continue "
                 "for engineering validation only.\n")
    L.append("## 1-4. Hypothesis, data, limitations, universe\n")
    L.append("Hypothesis: stocks with persistent relative strength and positive medium-term trends continue to "
             "outperform over intermediate horizons (Jegadeesh & Titman 1993; Asness et al. 2013).\n")
    L.append("Data notes:\n" + "".join(f"- {n}\n" for n in md.notes))
    L.append(f"- Universe columns with data: {md.close.shape[1]}; sessions: {md.close.shape[0]}\n")
    if md.membership is not None:
        cov = md.membership.sum(axis=1).mean()
        L.append(f"- Average point-in-time index members with price data per day: {cov:.0f} (of ~500). "
                 "Missing members are mostly delisted/renamed stocks absent from the vendor => residual survivorship bias.\n")
    L.append("\n## 5-9. Rules, risk, portfolio, costs\nSee docs/PHASE1_SPECIFICATION.md sections 5-7 and config/settings.yaml "
             f"(risk {cfg['risk']['risk_per_trade']:.1%}/trade, heat cap {cfg['risk']['max_portfolio_heat']:.0%}, "
             f"max {cfg['risk']['max_positions']} positions, FX fee {cfg['costs']['fx_fee_per_side']:.2%} per side, "
             f"slippage {cfg['costs']['slippage_per_side']:.2%} per side).\n")
    L.append("\n## 10-11. In-sample (pre-holdout, base parameters)\n")
    cols = [("strategy", "Strategy", False), ("cagr", "CAGR", True), ("volatility", "Vol", True), ("sharpe", "Sharpe", False),
            ("sortino", "Sortino", False), ("max_drawdown", "MaxDD", True), ("calmar", "Calmar", False),
            ("n_trades", "Trades", False), ("win_rate", "Win%", True), ("avg_r", "AvgR", False), ("median_r", "MedR", False),
            ("profit_factor", "PF", False), ("turnover", "Turnover/yr", False), ("avg_exposure", "Exposure", True),
            ("avg_holding_days", "Hold d", False)]
    L.append(_table([{"strategy": n, **r["in_sample"]} for n, r in results.items()], cols))
    L.append("\n## 12-13. Walk-forward out-of-sample (chained test years, after costs)\n")
    L.append(_table([{"strategy": n, **r["oos"]} for n, r in results.items()], cols[:-3]))
    L.append(f"\nBenchmarks over the same OOS window: SPY buy&hold (GBP) CAGR {_fmt(bench.get('cagr'), True)}, "
             f"Sharpe {_fmt(bench.get('sharpe'))}, MaxDD {_fmt(bench.get('max_drawdown'), True)}; equal-weight eligible "
             f"universe (no costs) CAGR {_fmt(ew.get('cagr'), True)}, Sharpe {_fmt(ew.get('sharpe'))}, "
             f"MaxDD {_fmt(ew.get('max_drawdown'), True)}. A benchmark comparison is not evidence of future performance.\n")
    L.append("\n![OOS equity](oos_equity.png)\n")
    for n, r in results.items():
        if len(r["folds"]):
            L.append(f"\n### Folds - {n}\n")
            L.append(r["folds"].to_markdown(index=False) + "\n")
    L.append("\n## 14. Robustness\n")
    for n, r in results.items():
        L.append(f"\n**{n} cost stress**\n\n" + r["stress"].to_markdown(index=False) + "\n")
        L.append(f"\nParameter-grid pass rate (PF>1 and expectancy>0): {_fmt(r['neighbour_frac'], True)} "
                 f"{'- FRAGILE' if (r['neighbour_frac'] or 0) < cfg['acceptance']['min_neighbour_pass_fraction'] else ''}\n")
    prim = cfg.get("strategy", {}).get("primary", "C")
    if "sensitivity" in results.get(prim, {}):
        L.append(f"\n**Sensitivity of primary strategy {prim} (one factor at a time, pre-holdout)**\n\n")
        L.append(results[prim]["sensitivity"].to_markdown(index=False) + "\n")
    L.append("\n## 15. Monte Carlo (simulation, not prediction)\n")
    for n, r in results.items():
        L.append(f"- {n}: trade bootstrap {json.dumps({k: round(v, 3) if isinstance(v, float) else v for k, v in r['mc'].items()})}\n")
        L.append(f"  daily bootstrap {json.dumps({k: round(v, 3) if isinstance(v, float) else v for k, v in r['mc_daily'].items()})}\n")
    L.append("\n## 16-18. Drawdowns, turnover, regimes, holdout\n")
    for n, r in results.items():
        h = r["holdout"]
        L.append(f"- {n}: OOS worst day {_fmt(r['oos'].get('worst_day'), True)}, worst week {_fmt(r['oos'].get('worst_week'), True)}, "
                 f"worst month {_fmt(r['oos'].get('worst_month'), True)}, longest underwater {r['oos'].get('recovery_days_max', 'n/a')} days, "
                 f"max consecutive losses {r['oos'].get('max_consecutive_losses', 'n/a')}, largest loss £{_fmt(r['oos'].get('largest_loss_gbp'))}. "
                 f"Holdout: return {_fmt(h.get('total_return'), True)}, MaxDD {_fmt(h.get('max_drawdown'), True)}, "
                 f"trades {h.get('n_trades', 0)}, avg R {_fmt(h.get('avg_r'))}. Deflated Sharpe prob {_fmt(r['dsr'].get('dsr'))}.\n")
        if len(r["regimes"]):
            L.append("\n" + r["regimes"].to_markdown(index=False) + "\n")
    L.append("\n## Acceptance test (pre-registered; all must pass)\n")
    for n, r in results.items():
        L.append(f"\n### {n} - {'PASS' if r['passed'] else 'FAIL'}\n\n")
        L.append(_table(r["acceptance"], [("criterion", "Criterion", False), ("value", "Value", False),
                                          ("rule", "Rule", False), ("result", "Result", False)]))
    L.append("\n## 19-21. Weaknesses, implementation risks, recommendation\n")
    L.append("- Survivorship bias remains if the data vendor lacks delisted stocks; results are likely optimistic.\n"
             "- Sectors are current classifications; earnings history from free sources is incomplete.\n"
             "- Momentum crash risk (sharp rebounds after bear markets) is not diversifiable in a long-only book.\n"
             "- Costs: 0.15% FX on both legs is a structural drag at swing-trading turnover.\n"
             "- Broker: API is beta, not idempotent; stops are market orders on trigger (gap risk beyond 1R).\n")
    L.append("\nRecommendation: " + ("paper trade the passing strategy for >= 3 months before any live decision.\n" if passed else
             "do not deploy real money; if paper trading continues, treat it as software validation only.\n"))
    (out / "report.md").write_text("".join(L), encoding="utf-8")
    summary = {n: {"passed": r["passed"], "oos": {k: v for k, v in r["oos"].items() if isinstance(v, (int, float))},
                   "final_params": r["final_params"]} for n, r in results.items()}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary

"""Build and cache the historical research dataset.

Pipeline: point-in-time S&P 500 membership (1996+) -> every symbol ever in the index since
`start` -> daily bars from the provider -> cached parquet panels + sectors + earnings.
"""
from __future__ import annotations

import json  # sector/earnings caches
from typing import Any, Mapping  # type hints

import pandas as pd  # panels

from ..config import PROJECT_ROOT  # cache location
from ..monitoring.logger import log_event  # logs
from .market_data import MarketData, ParquetCache, YahooProvider, build_market_data  # data layer
from .universe import all_member_symbols, load_membership_table, membership_panel  # universe

ROOT = PROJECT_ROOT / "data_cache" / "research"  # cache folder (git-ignored)


def download_research_data(cfg: Mapping[str, Any], start: str = "1998-01-01", refresh: bool = False,
                           with_sectors: bool = True, with_earnings: bool = True) -> None:
    """Download everything needed for the research study into data_cache/research/."""
    cache = ParquetCache(ROOT)
    table = load_membership_table(ROOT / "sp500_membership.csv")  # point-in-time membership
    symbols = all_member_symbols(table, start)  # includes later-removed names
    bench = cfg["benchmark"]["symbol"]
    prov = YahooProvider()
    if refresh or not cache.exists():
        panels = prov.daily_bars(symbols + [bench], start=start)  # may take several minutes
        cache.save(panels)
        prov.fx_usd_per_gbp(start=start).to_frame("GBPUSD").to_parquet(ROOT / "fx.parquet")
    have = list(cache.load()["close"].columns)
    log_event("UNIVERSE_UPDATED", "research prices cached", requested=len(symbols), with_data=len(have))
    if with_sectors and not (ROOT / "sectors.json").exists():
        (ROOT / "sectors.json").write_text(json.dumps(prov.sectors([s for s in have if s != bench])))
    if with_earnings and not (ROOT / "earnings.json").exists():
        e = prov.earnings_dates([s for s in have if s != bench])
        (ROOT / "earnings.json").write_text(json.dumps({k: [str(d.date()) for d in v] for k, v in e.items()}))


def load_research_data(cfg: Mapping[str, Any]) -> MarketData:
    """Load cached research data into a MarketData bundle (with coverage notes for the report)."""
    cache = ParquetCache(ROOT)
    if not cache.exists():
        raise FileNotFoundError("no research data cached - run: python -m src.main download-data")
    panels = cache.load()
    fx = pd.read_parquet(ROOT / "fx.parquet")["GBPUSD"]
    table = load_membership_table(ROOT / "sp500_membership.csv")
    bench = cfg["benchmark"]["symbol"]
    closes = panels["close"]
    dates = closes[bench].dropna().index
    stocks = [c for c in closes.columns if c != bench and closes[c].notna().any()]
    mem = membership_panel(table, dates, stocks)
    sectors = json.loads((ROOT / "sectors.json").read_text()) if (ROOT / "sectors.json").exists() else {}
    earn = json.loads((ROOT / "earnings.json").read_text()) if (ROOT / "earnings.json").exists() else {}
    notes = [
        "Prices: Yahoo Finance via yfinance (unofficial; split-adjusted OHLC, dividends explicit).",
        f"Universe: point-in-time S&P 500 membership (fja05680/sp500); {len(set(table['ticker']))} historical symbols, "
        f"{len(stocks)} with price data. Delisted/renamed members without data are missing => residual SURVIVORSHIP BIAS.",
        "Symbols reused by different companies over time may map to the wrong history (vendor limitation).",
        f"Sectors: current Yahoo classifications for {sum(1 for v in sectors.values() if v != 'UNKNOWN')} names "
        "(not point-in-time).",
        f"Earnings dates: Yahoo calendar for {len(earn)} names; unknown dates do NOT block entries in the backtest.",
        "GBPUSD: Yahoo 'GBPUSD=X'; before its first observation the earliest rate is carried backward.",
        "Broker availability: today's Trading 212 instrument list is NOT applied historically (small look-ahead for large caps).",
    ]
    return build_market_data({k: v for k, v in panels.items()}, bench, fx, mem, sectors,
                             {k: [pd.Timestamp(d) for d in v] for k, v in earn.items()}, notes)

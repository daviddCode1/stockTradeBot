"""Market-data layer (separate from the broker).

`MarketData` is the single in-memory bundle every other module reads. All panels are
pandas DataFrames indexed by trading date (rows) with one column per ticker.

Price conventions (important for look-ahead / corporate-action correctness):
  open/high/low/close : split-adjusted traded prices (used for fills, stops, ATR, SMAs)
  adj_close           : split + dividend adjusted (total return; used for momentum)
  unadj_close         : reconstructed as-traded price (used only for the $10 price filter)
  dividends           : cash dividend per share on the ex-date (split-adjusted like `close`)
Dividends are credited explicitly in the backtest (net of withholding), so P&L uses `close`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod  # provider interface
from dataclasses import dataclass, field  # container
from pathlib import Path  # cache paths
from typing import Iterable  # type hints

import numpy as np  # numerics
import pandas as pd  # time-series panels

from ..monitoring.logger import log_event  # structured logging

PANEL_FIELDS = ("open", "high", "low", "close", "adj_close", "volume", "unadj_close", "dividends")  # stored panels


@dataclass
class MarketData:
    """All data the strategy needs, aligned on one trading-day index."""

    open: pd.DataFrame  # split-adjusted open
    high: pd.DataFrame  # split-adjusted high
    low: pd.DataFrame  # split-adjusted low
    close: pd.DataFrame  # split-adjusted close
    adj_close: pd.DataFrame  # total-return adjusted close
    volume: pd.DataFrame  # split-adjusted volume
    unadj_close: pd.DataFrame  # as-traded close (for the price filter)
    dividends: pd.DataFrame  # dividend cash per share on ex-date
    benchmark_close: pd.Series  # benchmark split-adjusted close (regime filter)
    benchmark_adj: pd.Series  # benchmark total-return close (relative strength, buy&hold)
    fx_usd_per_gbp: pd.Series  # GBPUSD rate: how many USD one GBP buys
    membership: pd.DataFrame | None = None  # point-in-time index membership (bool)
    sectors: dict[str, str] = field(default_factory=dict)  # ticker -> sector
    earnings: dict[str, np.ndarray] = field(default_factory=dict)  # ticker -> sorted datetime64 array
    notes: list[str] = field(default_factory=list)  # data limitations to print in reports

    @property
    def dates(self) -> pd.DatetimeIndex:
        """Trading-day index shared by every panel."""
        return self.close.index  # type: ignore[return-value]

    @property
    def tickers(self) -> list[str]:
        """Column universe."""
        return list(self.close.columns)

    def slice(self, end: pd.Timestamp) -> "MarketData":
        """Return a copy truncated at `end` (inclusive). Used to prove no look-ahead in tests."""
        def cut(obj):  # truncate one panel/series
            return obj.loc[:end] if obj is not None else None
        return MarketData(
            open=cut(self.open), high=cut(self.high), low=cut(self.low), close=cut(self.close),
            adj_close=cut(self.adj_close), volume=cut(self.volume), unadj_close=cut(self.unadj_close),
            dividends=cut(self.dividends), benchmark_close=cut(self.benchmark_close),
            benchmark_adj=cut(self.benchmark_adj), fx_usd_per_gbp=cut(self.fx_usd_per_gbp),
            membership=cut(self.membership), sectors=dict(self.sectors), earnings=dict(self.earnings),
            notes=list(self.notes),
        )


class MarketDataProvider(ABC):
    """Interface every data vendor adapter implements."""

    name: str = "abstract"  # human-readable provider name

    @abstractmethod
    def daily_bars(self, symbols: list[str], start: str, end: str | None = None) -> dict[str, pd.DataFrame]:
        """Return per-field panels: open, high, low, close, adj_close, volume, dividends, splits."""

    @abstractmethod
    def fx_usd_per_gbp(self, start: str, end: str | None = None) -> pd.Series:
        """Daily GBPUSD rate (USD per 1 GBP)."""

    def sectors(self, symbols: Iterable[str]) -> dict[str, str]:  # optional capability
        return {}

    def earnings_dates(self, symbols: Iterable[str]) -> dict[str, list[pd.Timestamp]]:  # optional capability
        return {}


def reconstruct_unadjusted(close: pd.DataFrame, splits: pd.DataFrame) -> pd.DataFrame:
    """Undo split adjustment: price_as_traded(t) = split_adjusted_close(t) * product(splits after t).

    Example: a 4:1 split on day s (ratio 4.0) means every close *before* s was divided by 4
    in the split-adjusted series, so we multiply those back by 4.
    """
    ratios = splits.reindex_like(close).fillna(0.0)  # split ratio on its date, 0 elsewhere
    ratios = ratios.where(ratios > 0, 1.0)  # no split => multiplicative identity
    # cumulative product of all splits strictly after t: reverse cumprod, then shift by one day
    after = ratios.iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)
    return close * after  # as-traded price


def build_market_data(panels: dict[str, pd.DataFrame], benchmark: str, fx: pd.Series,
                      membership: pd.DataFrame | None = None, sectors: dict[str, str] | None = None,
                      earnings: dict[str, list[pd.Timestamp]] | None = None,
                      notes: list[str] | None = None) -> MarketData:
    """Assemble a MarketData bundle from raw provider panels, aligned on the benchmark calendar."""
    close = panels["close"]  # split-adjusted close
    if benchmark not in close.columns:  # the regime filter and RS need the benchmark
        raise ValueError(f"benchmark {benchmark} missing from market data")
    dates = close[benchmark].dropna().index  # benchmark trading days define the calendar
    def align(df: pd.DataFrame) -> pd.DataFrame:  # reindex to the calendar, float dtype
        return df.reindex(dates).astype(float)
    splits = panels.get("splits", pd.DataFrame(index=dates, columns=close.columns)).reindex(dates)
    unadj = reconstruct_unadjusted(align(close), splits.astype(float))  # as-traded price
    stocks = [c for c in close.columns if c != benchmark]  # tradable columns only
    fx_aligned = fx.reindex(dates).ffill().bfill()  # carry last known FX rate over holidays
    mem = membership.reindex(index=dates, columns=stocks).fillna(False).astype(bool) if membership is not None else None
    md = MarketData(
        open=align(panels["open"])[stocks], high=align(panels["high"])[stocks], low=align(panels["low"])[stocks],
        close=align(close)[stocks], adj_close=align(panels["adj_close"])[stocks],
        volume=align(panels["volume"])[stocks], unadj_close=unadj[stocks],
        dividends=align(panels.get("dividends", pd.DataFrame(0.0, index=dates, columns=close.columns)))[stocks].fillna(0.0),
        benchmark_close=align(close)[benchmark], benchmark_adj=align(panels["adj_close"])[benchmark],
        fx_usd_per_gbp=fx_aligned, membership=mem, sectors=dict(sectors or {}),
        earnings={k: np.array(sorted(pd.to_datetime(v)), dtype="datetime64[ns]") for k, v in (earnings or {}).items()},
        notes=list(notes or []),
    )
    return md


class ParquetCache:
    """Local on-disk cache of provider panels so research runs are repeatable and fast."""

    def __init__(self, root: Path):
        self.root = root  # e.g. data_cache/yahoo
        self.root.mkdir(parents=True, exist_ok=True)  # create folder on first use

    def save(self, panels: dict[str, pd.DataFrame]) -> None:
        for name, df in panels.items():  # one parquet file per field
            df.to_parquet(self.root / f"{name}.parquet")

    def load(self) -> dict[str, pd.DataFrame]:
        out = {}
        for p in self.root.glob("*.parquet"):  # read every cached field
            out[p.stem] = pd.read_parquet(p)
        return out

    def exists(self) -> bool:
        return (self.root / "close.parquet").exists()


class YahooProvider(MarketDataProvider):
    """Free Yahoo Finance data via `yfinance`.

    LIMITATIONS (reported in every backtest): current/recent tickers only (most delisted
    names are missing => survivorship bias), symbols reused by other companies may map to
    the wrong history, sectors are *current* classifications, and data is unofficial.
    """

    name = "yahoo"

    def __init__(self, chunk: int = 100):
        import yfinance  # imported lazily so tests don't need network access
        self._yf = yfinance
        self.chunk = chunk  # tickers per download request

    def daily_bars(self, symbols: list[str], start: str, end: str | None = None) -> dict[str, pd.DataFrame]:
        fields = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Adj Close": "adj_close",
                  "Volume": "volume", "Dividends": "dividends", "Stock Splits": "splits"}
        acc: dict[str, list[pd.DataFrame]] = {v: [] for v in fields.values()}
        for i in range(0, len(symbols), self.chunk):  # download in chunks
            batch = symbols[i:i + self.chunk]
            try:
                raw = self._yf.download(batch, start=start, end=end, auto_adjust=False, actions=True,
                                        group_by="column", threads=True, progress=False)
            except Exception as exc:  # vendor failure: log and continue (missing => not tradable)
                log_event("DATA_ERROR", "yahoo download failed", batch_start=batch[0], error=str(exc)[:200])
                continue
            if raw is None or raw.empty:
                continue
            for yf_name, our_name in fields.items():
                if yf_name in raw.columns.get_level_values(0):
                    part = raw[yf_name]
                    if isinstance(part, pd.Series):  # single-ticker download returns a Series
                        part = part.to_frame(batch[0])
                    acc[our_name].append(part)
        out = {}
        for k, parts in acc.items():
            out[k] = pd.concat(parts, axis=1) if parts else pd.DataFrame()
            out[k] = out[k].loc[:, ~out[k].columns.duplicated()]  # guard against duplicate columns
            out[k].index = pd.to_datetime(out[k].index).tz_localize(None)  # naive dates
        return out

    def fx_usd_per_gbp(self, start: str, end: str | None = None) -> pd.Series:
        raw = self._yf.download("GBPUSD=X", start=start, end=end, auto_adjust=False, progress=False)
        s = raw["Close"]
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        s.index = pd.to_datetime(s.index).tz_localize(None)
        return s.dropna().rename("GBPUSD")

    def sectors(self, symbols: Iterable[str]) -> dict[str, str]:
        out = {}
        for s in symbols:  # one request per symbol (slow; cached by caller)
            try:
                out[s] = (self._yf.Ticker(s).info or {}).get("sector") or "UNKNOWN"
            except Exception:
                out[s] = "UNKNOWN"  # unknown sector is treated conservatively (shared bucket)
        return out

    def earnings_dates(self, symbols: Iterable[str]) -> dict[str, list[pd.Timestamp]]:
        out: dict[str, list[pd.Timestamp]] = {}
        for s in symbols:
            try:
                df = self._yf.Ticker(s).get_earnings_dates(limit=100)  # ~25 years of quarterly dates
                if df is not None and len(df):
                    out[s] = sorted({pd.Timestamp(d).tz_localize(None).normalize() for d in df.index})
            except Exception:
                continue  # missing => unknown (live: ineligible; backtest: see settings)
        return out

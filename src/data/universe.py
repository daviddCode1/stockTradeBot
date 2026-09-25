"""Universe construction: research universe -> broker-available instruments -> tradable universe.

1. Research universe: point-in-time S&P 500 membership (public dataset fja05680/sp500,
   start/end dates per ticker since 1996). Using past membership - not today's list - reduces
   survivorship bias in *selection*; price coverage for delisted names depends on the vendor.
2. Broker availability: Trading 212 instruments that are US common stocks on NYSE/NASDAQ.
3. Tradable filters (price, liquidity, history, data freshness) computed from data up to day t.
"""
from __future__ import annotations

import io  # parse downloaded CSV text
from pathlib import Path  # cache file
from typing import Iterable  # type hints

import pandas as pd  # tables
import requests  # download membership CSV

from ..broker.models import Instrument  # broker instrument model

# Public, regularly updated point-in-time membership file (ticker,start_date,end_date).
MEMBERSHIP_URL = "https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv"
US_EXCHANGES = {"NYSE", "NASDAQ"}  # excludes "OTC Markets" and non-US venues


def to_vendor_symbol(symbol: str) -> str:
    """Convert exchange-style class shares (BRK.B) to Yahoo style (BRK-B)."""
    return symbol.strip().upper().replace(".", "-")


def load_membership_table(cache: Path | None = None, url: str = MEMBERSHIP_URL) -> pd.DataFrame:
    """Download (or read cached) S&P 500 membership intervals."""
    if cache is not None and cache.exists():  # reuse local copy
        text = cache.read_text(encoding="utf-8")
    else:
        resp = requests.get(url, timeout=30)  # public GitHub raw file
        resp.raise_for_status()  # fail loudly on HTTP errors
        text = resp.text
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text, encoding="utf-8")  # cache for repeatable runs
    df = pd.read_csv(io.StringIO(text))  # columns: ticker,start_date,end_date
    df["ticker"] = df["ticker"].map(to_vendor_symbol)  # vendor symbol style
    df["start_date"] = pd.to_datetime(df["start_date"])  # inclusion date
    df["end_date"] = pd.to_datetime(df["end_date"])  # removal date (NaT = still a member)
    return df


def membership_panel(table: pd.DataFrame, dates: pd.DatetimeIndex, tickers: Iterable[str]) -> pd.DataFrame:
    """Boolean panel: True where ticker was an index member on that date (point-in-time)."""
    cols = list(tickers)  # requested columns
    panel = pd.DataFrame(False, index=dates, columns=cols)  # default: not a member
    for row in table.itertuples(index=False):  # one membership interval per row
        if row.ticker not in panel.columns:  # no price data for this symbol
            continue
        end = row.end_date if pd.notna(row.end_date) else dates[-1] + pd.Timedelta(days=1)  # open interval
        mask = (dates >= row.start_date) & (dates < end)  # member from start (inclusive) to end (exclusive)
        panel.loc[mask, row.ticker] = True
    return panel


def all_member_symbols(table: pd.DataFrame, start: str) -> list[str]:
    """Every ticker that was a member at any time on/after `start` (for data download)."""
    s = pd.Timestamp(start)
    keep = table[(table["end_date"].isna()) | (table["end_date"] >= s)]  # overlaps the period
    return sorted(set(keep["ticker"]))


def broker_tradable_map(instruments: Iterable[Instrument], allowed_types: Iterable[str] = ("STOCK",),
                        currency: str = "USD") -> dict[str, Instrument]:
    """Map vendor symbol -> broker Instrument for US-listed common stocks on NYSE/NASDAQ."""
    allowed = set(allowed_types)
    out: dict[str, Instrument] = {}
    for ins in instruments:
        if ins.type not in allowed or ins.currency != currency:  # e.g. drop ETFs/warrants/non-USD
            continue
        if ins.exchange not in US_EXCHANGES:  # drop OTC and foreign venues
            continue
        if not ins.broker_ticker.endswith("_US_EQ"):  # Trading 212 US equity convention
            continue
        sym = to_vendor_symbol(ins.symbol)  # use shortName (exchange symbol), NOT the internal id
        if sym and sym not in out:  # first match wins; duplicates are rare
            out[sym] = ins
    return out

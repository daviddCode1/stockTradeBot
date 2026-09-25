"""Typed broker data models (independent of any specific broker's JSON format)."""
from __future__ import annotations

from dataclasses import dataclass, field  # simple typed records
from datetime import datetime  # timestamps
from typing import Any, Optional  # type hints

# Order status groups (Trading 212 enum values). Anything not listed is treated as ambiguous.
OPEN_STATUSES = {"LOCAL", "UNCONFIRMED", "CONFIRMED", "NEW", "PARTIALLY_FILLED", "CANCELLING", "REPLACING"}
FINAL_STATUSES = {"FILLED", "CANCELLED", "REJECTED", "REPLACED"}


@dataclass(frozen=True)
class AccountSummary:
    """Account-level numbers, all in the account's primary currency."""

    account_id: str  # broker account number
    currency: str  # ISO 4217, e.g. GBP
    total_value: float  # cash + investments
    cash_available: float  # free to trade
    cash_reserved: float  # held for pending orders
    invested_value: float  # current value of positions


@dataclass(frozen=True)
class Instrument:
    """One tradable instrument from the broker's metadata."""

    broker_ticker: str  # e.g. AAPL_US_EQ (broker-internal id; NOT always the exchange symbol)
    symbol: str  # exchange symbol (Trading 212 `shortName`, e.g. IONQ for DMYI_US_EQ)
    isin: str  # ISIN, the most reliable cross-provider identifier
    name: str  # company name
    type: str  # STOCK, ETF, WARRANT, ...
    currency: str  # instrument currency, e.g. USD
    exchange: str  # exchange name resolved from the working schedule (NYSE, NASDAQ, OTC Markets...)
    max_open_quantity: float  # broker cap on position size
    extended_hours: bool  # whether extended-hours trading is available


@dataclass(frozen=True)
class Position:
    """An open position as reported by the broker."""

    broker_ticker: str  # instrument id
    quantity: float  # shares held (fractional allowed)
    quantity_available: float  # shares not reserved by pending sell orders
    avg_price: float  # average price paid, instrument currency
    current_price: float  # latest price, instrument currency
    currency: str  # instrument currency
    value_account_ccy: float  # current value in account currency
    opened_at: Optional[datetime] = None  # when the position was opened


@dataclass(frozen=True)
class Order:
    """A broker order (pending or historical)."""

    order_id: str  # broker id
    broker_ticker: str  # instrument
    type: str  # MARKET, LIMIT, STOP, STOP_LIMIT
    side: str  # BUY or SELL
    quantity: float  # requested quantity (positive; side carries direction)
    filled_quantity: float  # executed so far
    status: str  # broker status enum
    limit_price: Optional[float] = None  # LIMIT / STOP_LIMIT
    stop_price: Optional[float] = None  # STOP / STOP_LIMIT
    created_at: Optional[datetime] = None  # submission time
    initiated_from: str = ""  # API, WEB, IOS, ... (tells bot orders from manual ones)
    fill_price: Optional[float] = None  # from history: executed price (instrument currency)
    filled_at: Optional[datetime] = None  # from history: execution time
    fx_rate: Optional[float] = None  # from history: conversion rate applied
    fees_account_ccy: float = 0.0  # from history: sum of taxes/fees (positive number)
    net_value_account_ccy: Optional[float] = None  # from history: wallet impact
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)  # original JSON for audit

    @property
    def is_open(self) -> bool:
        """True while the order can still execute."""
        return self.status in OPEN_STATUSES


@dataclass(frozen=True)
class OrderRequest:
    """What we want to send. Quantity is always positive here; side decides the sign on the wire."""

    broker_ticker: str  # instrument id
    side: str  # BUY or SELL
    quantity: float  # positive number of shares
    order_type: str  # MARKET, LIMIT, STOP, STOP_LIMIT
    limit_price: Optional[float] = None  # required for LIMIT / STOP_LIMIT
    stop_price: Optional[float] = None  # required for STOP / STOP_LIMIT
    time_validity: str = "DAY"  # DAY or GOOD_TILL_CANCEL (ignored for MARKET)
    extended_hours: bool = False  # MARKET only

    def signed_quantity(self) -> float:
        """Trading 212 convention: buys are positive, sells are negative."""
        return self.quantity if self.side == "BUY" else -self.quantity

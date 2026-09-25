"""Abstract broker interface: the execution engine talks only to this, never to HTTP directly.

This separation means Trading 212 (demo or live) or an in-memory simulated broker can be
swapped without touching strategy, risk or execution logic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod  # abstract base class tooling
from datetime import datetime  # history filters

from .models import AccountSummary, Instrument, Order, OrderRequest, Position  # typed models


class BrokerError(RuntimeError):
    """Any broker failure. `ambiguous=True` means we do NOT know whether an order was accepted."""

    def __init__(self, message: str, status: int | None = None, ambiguous: bool = False):
        super().__init__(message)  # standard exception message
        self.status = status  # HTTP status if any
        self.ambiguous = ambiguous  # True on timeouts/5xx for order placement


class Broker(ABC):
    """Execution-only broker contract."""

    @abstractmethod
    def account_summary(self) -> AccountSummary: ...  # cash / value / currency / id

    @abstractmethod
    def instruments(self) -> list[Instrument]: ...  # all tradable instruments

    @abstractmethod
    def positions(self) -> list[Position]: ...  # open positions

    @abstractmethod
    def pending_orders(self) -> list[Order]: ...  # working orders

    @abstractmethod
    def order_history(self, since: datetime | None = None, ticker: str | None = None,
                      max_pages: int = 20) -> list[Order]: ...  # filled/cancelled orders, newest first

    @abstractmethod
    def place_order(self, req: OrderRequest) -> Order: ...  # submit ONCE; never retried internally

    @abstractmethod
    def cancel_order(self, order_id: str) -> None: ...  # request cancellation (must be verified)

    @abstractmethod
    def get_order(self, order_id: str) -> Order | None: ...  # a pending order by id (None if not pending)

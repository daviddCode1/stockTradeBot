"""Trading 212 Public API (v0) adapter.

Endpoints used (official OpenAPI spec, verified against the demo environment 2026-09-25):
  GET    /equity/account/summary          1 req / 5 s
  GET    /equity/positions                1 req / 1 s
  GET    /equity/orders                   1 req / 5 s
  GET    /equity/orders/{id}              1 req / 1 s
  DELETE /equity/orders/{id}              50 req / 1 min
  POST   /equity/orders/market            50 req / 1 min   (NOT idempotent)
  POST   /equity/orders/limit             1 req / 2 s      (NOT idempotent)
  POST   /equity/orders/stop              1 req / 2 s      (NOT idempotent)
  POST   /equity/orders/stop_limit        1 req / 2 s      (NOT idempotent)
  GET    /equity/history/orders           6 req / 1 min    (cursor pagination via nextPagePath)
  GET    /equity/metadata/instruments     1 req / 50 s
  GET    /equity/metadata/exchanges       1 req / 30 s

Safety rules implemented here:
  * POST (order placement) is sent exactly once. It is never retried in this class.
    Timeouts / 5xx / connection errors raise BrokerError(ambiguous=True) so the order
    manager must reconcile before doing anything else.
  * GET requests may be retried (they do not change state).
  * Credentials are never logged. If T212_API_KEY/T212_API_SECRET are absent, no auth
    header is sent (supported for hosting environments whose egress proxy injects it).
"""
from __future__ import annotations

import base64  # HTTP Basic auth encoding
import os  # credentials from environment
import time  # rate-limit pacing
from datetime import datetime, timezone  # timestamps
from typing import Any, Callable  # type hints

import requests  # HTTP client

from ..monitoring.logger import log_event  # structured logs (secrets redacted)
from .interface import Broker, BrokerError  # abstract contract
from .models import AccountSummary, Instrument, Order, OrderRequest, Position  # typed models

# Minimum spacing between calls per endpoint "family", derived from the documented limits.
# Demo testing (2026-09-25) showed limit/stop/stop-limit placements share one rate bucket,
# so they use a single "order_post" family with a safety margin.
_MIN_INTERVAL_S = {
    "summary": 5.1, "positions": 1.05, "orders_list": 5.1, "order_get": 1.05, "cancel": 1.25,
    "market": 1.25, "order_post": 3.0, "history": 10.5,
    "instruments": 50.5, "exchanges": 30.5,
}


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from the API into an aware UTC datetime."""
    if not value:  # missing timestamps are allowed
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)  # normalise to UTC
    except ValueError:  # malformed => treat as unknown rather than crash
        return None


class Trading212Broker(Broker):
    """HTTP adapter implementing the Broker interface for Trading 212 Invest / Stocks ISA."""

    def __init__(self, base_url: str, timeout_s: float = 10.0, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.base_url = base_url.rstrip("/")  # e.g. https://demo.trading212.com/api/v0
        self.timeout_s = timeout_s  # per-request timeout
        self.session = session or requests.Session()  # connection pooling
        self._sleep = sleep  # injectable for tests (no real waiting)
        self._clock = clock  # injectable monotonic clock
        self._last_call: dict[str, float] = {}  # endpoint family -> last call time
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}  # JSON API
        key, secret = os.environ.get("T212_API_KEY"), os.environ.get("T212_API_SECRET")  # never logged
        if key and secret:  # standard HTTP Basic: key as user, secret as password
            token = base64.b64encode(f"{key}:{secret}".encode("utf-8")).decode("ascii")
            self._headers["Authorization"] = f"Basic {token}"
        # else: no header; the hosting proxy may inject credentials (verified for this cloud setup)
        self._exchange_cache: dict[int, str] | None = None  # workingScheduleId -> exchange name

    # ------------------------------------------------------------------ low-level HTTP
    def _pace(self, family: str) -> None:
        """Sleep just enough to respect the documented per-endpoint rate limit."""
        min_gap = _MIN_INTERVAL_S.get(family, 1.0)  # default 1 s spacing
        last = self._last_call.get(family)  # when we last called this family
        if last is not None:
            wait = min_gap - (self._clock() - last)  # remaining time in the window
            if wait > 0:
                self._sleep(wait)  # block until allowed
        self._last_call[family] = self._clock()  # record this call

    def _honour_headers(self, resp: requests.Response) -> None:
        """If the server says we have 0 requests left, wait until its reset time."""
        remaining = resp.headers.get("x-ratelimit-remaining")  # requests left in window
        reset = resp.headers.get("x-ratelimit-reset")  # unix timestamp of reset
        if remaining == "0" and reset:
            try:
                wait = float(reset) - time.time()  # seconds until reset
            except ValueError:
                return
            if 0 < wait < 90:  # sanity cap: never sleep absurdly long
                self._sleep(wait)

    def _get(self, path: str, family: str, params: dict[str, Any] | None = None, retries: int = 3) -> Any:
        """GET with pacing and bounded retries (GET is safe to repeat)."""
        url = path if path.startswith("http") else f"{self.base_url}{path}"  # absolute or relative path
        for attempt in range(retries + 1):  # first try + retries
            self._pace(family)  # respect rate limits
            try:
                resp = self.session.get(url, headers=self._headers, params=params, timeout=self.timeout_s)
            except requests.RequestException as exc:  # network problem
                log_event("API_ERROR", "GET failed", path=path, error=type(exc).__name__, attempt=attempt)
                if attempt == retries:
                    raise BrokerError(f"GET {path} failed: {type(exc).__name__}") from exc
                self._sleep(2 ** attempt)  # exponential backoff
                continue
            self._honour_headers(resp)  # slow down if the window is exhausted
            if resp.status_code == 200:  # success
                return resp.json()
            if resp.status_code in (429, 408) or resp.status_code >= 500:  # transient
                log_event("API_ERROR", "GET transient error", path=path, status=resp.status_code, attempt=attempt)
                if attempt == retries:
                    raise BrokerError(f"GET {path} -> {resp.status_code}", status=resp.status_code)
                self._sleep(2 ** attempt + 1)  # back off then retry
                continue
            if resp.status_code == 404:  # "not found" is a valid answer for single-order lookups
                return None
            log_event("API_ERROR", "GET rejected", path=path, status=resp.status_code, body=resp.text[:300])
            raise BrokerError(f"GET {path} -> {resp.status_code}", status=resp.status_code)  # 401/403/400
        raise BrokerError(f"GET {path} exhausted retries")  # unreachable safety net

    def _post_once(self, path: str, family: str, body: dict[str, Any]) -> Any:
        """POST exactly once. Ambiguous outcomes are reported, never retried."""
        self._pace(family)  # respect rate limits before the single attempt
        try:
            resp = self.session.post(f"{self.base_url}{path}", headers=self._headers, json=body, timeout=self.timeout_s)
        except requests.Timeout as exc:  # we don't know whether the broker accepted it
            raise BrokerError(f"POST {path} timed out", ambiguous=True) from exc
        except requests.RequestException as exc:  # connection dropped mid-flight: also unknown
            raise BrokerError(f"POST {path} failed: {type(exc).__name__}", ambiguous=True) from exc
        if resp.status_code == 200:  # accepted
            return resp.json()
        if resp.status_code == 408 or resp.status_code >= 500:  # server-side uncertainty
            raise BrokerError(f"POST {path} -> {resp.status_code}", status=resp.status_code, ambiguous=True)
        # 400 validation / 401 / 403 / 429: the order was not processed
        raise BrokerError(f"POST {path} -> {resp.status_code}: {resp.text[:300]}", status=resp.status_code)

    # ------------------------------------------------------------------ parsing helpers
    @staticmethod
    def _order_from_json(o: dict[str, Any], fill: dict[str, Any] | None = None) -> Order:
        """Convert Trading 212 order JSON (+ optional fill) into our Order model."""
        qty = float(o.get("quantity") or 0.0)  # sells may be negative on some payloads
        side = o.get("side") or ("SELL" if qty < 0 else "BUY")  # explicit side if present
        fees = 0.0  # sum of taxes/fees from the fill's wallet impact
        fx_rate = net_value = fill_price = None  # optional fill details
        filled_at = None
        if fill:  # history endpoint provides execution details
            wallet = fill.get("walletImpact") or {}
            fees = -sum(float(t.get("quantity") or 0.0) for t in wallet.get("taxes") or [])  # taxes are negative
            fx_rate = wallet.get("fxRate")  # conversion rate used
            net_value = wallet.get("netValue")  # account-currency value
            fill_price = fill.get("price")  # executed price in instrument currency
            filled_at = _parse_ts(fill.get("filledAt"))
        return Order(
            order_id=str(o.get("id")),
            broker_ticker=o.get("ticker") or (o.get("instrument") or {}).get("ticker", ""),
            type=o.get("type", ""),
            side=side,
            quantity=abs(qty),
            filled_quantity=abs(float(o.get("filledQuantity") or 0.0)),
            status=o.get("status", "UNKNOWN"),
            limit_price=o.get("limitPrice"),
            stop_price=o.get("stopPrice"),
            created_at=_parse_ts(o.get("createdAt")),
            initiated_from=o.get("initiatedFrom", ""),
            fill_price=fill_price,
            filled_at=filled_at,
            fx_rate=fx_rate,
            fees_account_ccy=fees,
            net_value_account_ccy=net_value,
            raw={"order": o, "fill": fill},
        )

    # ------------------------------------------------------------------ Broker interface
    def account_summary(self) -> AccountSummary:
        d = self._get("/equity/account/summary", "summary")  # documented shape (no account-type field)
        if not isinstance(d, dict) or "currency" not in d or "id" not in d:  # ambiguous => stop
            raise BrokerError("unexpected account summary shape")
        cash = d.get("cash") or {}  # cash breakdown
        inv = d.get("investments") or {}  # investment breakdown
        return AccountSummary(
            account_id=str(d["id"]),
            currency=str(d["currency"]),
            total_value=float(d.get("totalValue") or 0.0),
            cash_available=float(cash.get("availableToTrade") or 0.0),
            cash_reserved=float(cash.get("reservedForOrders") or 0.0),
            invested_value=float(inv.get("currentValue") or 0.0),
        )

    def _exchanges(self) -> dict[int, str]:
        """Map workingScheduleId -> exchange name (cached; the endpoint is slow-rate-limited)."""
        if self._exchange_cache is None:
            data = self._get("/equity/metadata/exchanges", "exchanges") or []
            mapping: dict[int, str] = {}
            for ex in data:  # each exchange has several working schedules
                for ws in ex.get("workingSchedules") or []:
                    mapping[int(ws["id"])] = ex.get("name", "")
            self._exchange_cache = mapping
        return self._exchange_cache

    def instruments(self) -> list[Instrument]:
        schedules = self._exchanges()  # needed to exclude OTC listings
        data = self._get("/equity/metadata/instruments", "instruments") or []
        out = []
        for x in data:
            out.append(Instrument(
                broker_ticker=x.get("ticker", ""),
                symbol=(x.get("shortName") or "").upper(),  # exchange symbol
                isin=x.get("isin", "") or "",
                name=x.get("name", "") or "",
                type=x.get("type", "") or "",
                currency=x.get("currencyCode", "") or "",
                exchange=schedules.get(int(x.get("workingScheduleId") or -1), "UNKNOWN"),
                max_open_quantity=float(x.get("maxOpenQuantity") or 0.0),
                extended_hours=bool(x.get("extendedHours")),
            ))
        return out

    def positions(self) -> list[Position]:
        data = self._get("/equity/positions", "positions") or []
        out = []
        for p in data:
            inst = p.get("instrument") or {}
            wallet = p.get("walletImpact") or {}
            out.append(Position(
                broker_ticker=inst.get("ticker", ""),
                quantity=float(p.get("quantity") or 0.0),
                quantity_available=float(p.get("quantityAvailableForTrading") or 0.0),
                avg_price=float(p.get("averagePricePaid") or 0.0),
                current_price=float(p.get("currentPrice") or 0.0),
                currency=inst.get("currency", ""),
                value_account_ccy=float(wallet.get("currentValue") or 0.0),
                opened_at=_parse_ts(p.get("createdAt")),
            ))
        return out

    def pending_orders(self) -> list[Order]:
        data = self._get("/equity/orders", "orders_list") or []
        return [self._order_from_json(o) for o in data]

    def get_order(self, order_id: str) -> Order | None:
        d = self._get(f"/equity/orders/{int(order_id)}", "order_get")  # 404 => None
        return self._order_from_json(d) if d else None

    def order_history(self, since: datetime | None = None, ticker: str | None = None,
                      max_pages: int = 20) -> list[Order]:
        params: dict[str, Any] | None = {"limit": 50}  # maximum page size
        if ticker:
            params["ticker"] = ticker
        path: str | None = "/equity/history/orders"
        out: list[Order] = []
        for _ in range(max_pages):  # bounded pagination
            if path is None:
                break
            url = path if path.startswith("/equity") else path.replace("/api/v0", "", 1)  # nextPagePath is absolute
            page = self._get(url, "history", params=params) or {}
            params = None  # nextPagePath already contains the query string
            items = page.get("items") or []
            for it in items:
                order = self._order_from_json(it.get("order") or {}, it.get("fill"))
                out.append(order)
            path = page.get("nextPagePath")  # None at the end
            if since and out and out[-1].created_at and out[-1].created_at < since:  # older than needed
                break
        if since:
            out = [o for o in out if o.created_at is None or o.created_at >= since]  # trim
        return out

    def place_order(self, req: OrderRequest) -> Order:
        """Send exactly one order. Returns the broker's order record."""
        body: dict[str, Any] = {"ticker": req.broker_ticker, "quantity": req.signed_quantity()}  # sign = side
        if req.order_type == "MARKET":
            path, family = "/equity/orders/market", "market"
            body["extendedHours"] = bool(req.extended_hours)
        elif req.order_type == "LIMIT":
            path, family = "/equity/orders/limit", "order_post"
            body.update(limitPrice=req.limit_price, timeValidity=req.time_validity)
        elif req.order_type == "STOP":
            path, family = "/equity/orders/stop", "order_post"
            body.update(stopPrice=req.stop_price, timeValidity=req.time_validity)
        elif req.order_type == "STOP_LIMIT":
            path, family = "/equity/orders/stop_limit", "order_post"
            body.update(stopPrice=req.stop_price, limitPrice=req.limit_price, timeValidity=req.time_validity)
        else:  # unknown type => refuse
            raise BrokerError(f"unsupported order type {req.order_type}")
        d = self._post_once(path, family, body)  # single attempt
        if not isinstance(d, dict) or "id" not in d:  # 200 but no id: we cannot track it => ambiguous
            raise BrokerError("order accepted with unexpected response shape", ambiguous=True)
        return self._order_from_json(d)

    def cancel_order(self, order_id: str) -> None:
        """Request cancellation. Callers must verify via pending_orders()/history afterwards."""
        self._pace("cancel")
        try:
            resp = self.session.delete(f"{self.base_url}/equity/orders/{int(order_id)}",
                                       headers=self._headers, timeout=self.timeout_s)
        except requests.RequestException as exc:  # unknown whether it was cancelled
            raise BrokerError(f"cancel {order_id} failed", ambiguous=True) from exc
        if resp.status_code in (200, 404):  # 404: already gone (filled/cancelled) - verify separately
            return
        raise BrokerError(f"cancel {order_id} -> {resp.status_code}", status=resp.status_code,
                          ambiguous=resp.status_code >= 500)

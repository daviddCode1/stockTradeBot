"""Broker adapter (mocked HTTP), duplicate-order protection, reconciliation, restart, config safety."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests
import responses

from src.backtest.costs import CostModel
from src.broker.interface import BrokerError
from src.broker.models import Order, OrderRequest, Position
from src.broker.trading212_client import Trading212Broker
from src.config import ConfigError, build_runtime_context, load_settings, resolve_trading_mode
from src.data.universe import broker_tradable_map, to_vendor_symbol
from src.broker.models import Instrument
from src.execution.order_manager import OrderManager
from src.execution.reconciliation import reconcile
from src.monitoring.logger import redact
from src.portfolio.exposure import HeldPosition
from src.storage.database import Database
from src.storage.repositories import Repo

BASE = "https://demo.trading212.com/api/v0"


def _broker():
    return Trading212Broker(BASE, sleep=lambda s: None)  # no real waiting in tests


# ------------------------------------------------------------------ config / safety
def test_live_requires_double_opt_in():
    assert resolve_trading_mode({}) == "PAPER"
    assert resolve_trading_mode({"TRADING_MODE": "LIVE"}) == "PAPER"
    assert resolve_trading_mode({"ENABLE_LIVE_TRADING": "YES"}) == "PAPER"
    assert resolve_trading_mode({"TRADING_MODE": "LIVE", "ENABLE_LIVE_TRADING": "yes"}) == "LIVE"


def test_account_type_enforced():
    s = load_settings()
    for bad in ("CFD", "SIPP", ""):
        with pytest.raises(ConfigError):
            build_runtime_context(s, {"ACCOUNT_TYPE": bad})
    ctx = build_runtime_context(s, {"ACCOUNT_TYPE": "ISA"})
    assert ctx.mode == "PAPER" and "demo." in ctx.base_url and ctx.dry_run


def test_live_requires_account_pin():
    s = load_settings()
    with pytest.raises(ConfigError):
        build_runtime_context(s, {"ACCOUNT_TYPE": "INVEST", "TRADING_MODE": "LIVE", "ENABLE_LIVE_TRADING": "YES"})


def test_kill_switch_env():
    s = load_settings()
    assert build_runtime_context(s, {"ACCOUNT_TYPE": "INVEST", "KILL_SWITCH": "true"}).kill_switch


def test_secrets_redacted(monkeypatch):
    monkeypatch.setenv("T212_API_SECRET", "supersecretvalue123")
    assert "supersecretvalue123" not in redact('{"x": "supersecretvalue123"}')
    assert "abc123xyz" not in redact("Authorization: Basic abc123xyz")


# ------------------------------------------------------------------ broker adapter (mocked)
@responses.activate
def test_account_summary_parsing():
    responses.get(f"{BASE}/equity/account/summary", json={"id": 123, "currency": "GBP", "totalValue": 1000,
                  "cash": {"availableToTrade": 400, "reservedForOrders": 0, "inPies": 0},
                  "investments": {"currentValue": 600}})
    a = _broker().account_summary()
    assert a.currency == "GBP" and a.cash_available == 400 and a.account_id == "123"


@responses.activate
def test_ambiguous_summary_raises():
    responses.get(f"{BASE}/equity/account/summary", json={"unexpected": True})
    with pytest.raises(BrokerError):
        _broker().account_summary()


@responses.activate
def test_sell_uses_negative_quantity_and_single_post():
    responses.post(f"{BASE}/equity/orders/stop", json={"id": 9, "ticker": "AAPL_US_EQ", "type": "STOP", "quantity": -2,
                   "status": "NEW", "side": "SELL", "stopPrice": 90})
    o = _broker().place_order(OrderRequest("AAPL_US_EQ", "SELL", 2, "STOP", stop_price=90, time_validity="GOOD_TILL_CANCEL"))
    body = json.loads(responses.calls[0].request.body)
    assert body == {"ticker": "AAPL_US_EQ", "quantity": -2, "stopPrice": 90, "timeValidity": "GOOD_TILL_CANCEL"}
    assert o.side == "SELL" and o.quantity == 2 and len(responses.calls) == 1


@responses.activate
def test_order_timeout_is_ambiguous_and_not_retried():
    responses.post(f"{BASE}/equity/orders/limit", body=requests.Timeout())
    with pytest.raises(BrokerError) as ei:
        _broker().place_order(OrderRequest("AAPL_US_EQ", "BUY", 1, "LIMIT", limit_price=100))
    assert ei.value.ambiguous and len(responses.calls) == 1  # exactly one attempt


@responses.activate
def test_order_500_ambiguous_400_definite():
    responses.post(f"{BASE}/equity/orders/market", status=503)
    with pytest.raises(BrokerError) as e1:
        _broker().place_order(OrderRequest("X_US_EQ", "BUY", 1, "MARKET"))
    assert e1.value.ambiguous
    responses.replace(responses.POST, f"{BASE}/equity/orders/market", status=400, json={"detail": "min-value"})
    with pytest.raises(BrokerError) as e2:
        _broker().place_order(OrderRequest("X_US_EQ", "BUY", 1, "MARKET"))
    assert not e2.value.ambiguous


@responses.activate
def test_get_retries_on_429_then_succeeds():
    responses.get(f"{BASE}/equity/positions", status=429)
    responses.get(f"{BASE}/equity/positions", json=[])
    assert _broker().positions() == [] and len(responses.calls) == 2


@responses.activate
def test_history_pagination():
    responses.get(f"{BASE}/equity/history/orders", json={"items": [{"order": {"id": 1, "ticker": "A_US_EQ", "side": "BUY",
                  "quantity": 1, "filledQuantity": 1, "status": "FILLED", "type": "MARKET", "createdAt": "2026-09-01T10:00:00Z"},
                  "fill": {"price": 10, "filledAt": "2026-09-01T13:30:00Z", "walletImpact": {"fxRate": 1.3, "netValue": 7.7,
                  "taxes": [{"name": "CURRENCY_CONVERSION_FEE", "quantity": -0.01}]}}}],
                  "nextPagePath": "/api/v0/equity/history/orders?limit=50&cursor=5"})
    responses.get(f"{BASE}/equity/history/orders?limit=50&cursor=5", json={"items": [], "nextPagePath": None})
    hist = _broker().order_history()
    assert len(hist) == 1 and hist[0].fill_price == 10 and hist[0].fees_account_ccy == pytest.approx(0.01)


def test_symbol_mapping_uses_short_name_not_internal_ticker():
    ins = [Instrument("DMYI_US_EQ", "IONQ", "US46222L1089", "IonQ", "STOCK", "USD", "NYSE", 1e5, True),
           Instrument("FOO_US_EQ", "FOO", "X", "Foo", "STOCK", "USD", "OTC Markets", 1e5, True),
           Instrument("BRK_B_US_EQ", "BRK.B", "Y", "Berkshire", "STOCK", "USD", "NYSE", 1e5, True),
           Instrument("SPY_US_EQ", "SPY", "Z", "SPY", "ETF", "USD", "NYSE", 1e5, True)]
    m = broker_tradable_map(ins)
    assert m["IONQ"].broker_ticker == "DMYI_US_EQ" and "FOO" not in m and "SPY" not in m and "BRK-B" in m
    assert to_vendor_symbol("brk.b") == "BRK-B"


# ------------------------------------------------------------------ duplicate protection
class FakeBroker:
    """In-memory broker double."""

    def __init__(self, fail_ambiguous=False):
        self.placed, self.pending, self.fail = [], [], fail_ambiguous

    def place_order(self, req):
        self.placed.append(req)
        if self.fail:
            raise BrokerError("timeout", ambiguous=True)
        o = Order(str(100 + len(self.placed)), req.broker_ticker, req.order_type, req.side, req.quantity, 0.0, "NEW",
                  req.limit_price, req.stop_price, datetime.now(timezone.utc), "API")
        self.pending.append(o)
        return o

    def pending_orders(self):
        return list(self.pending)

    def cancel_order(self, oid):
        self.pending = [o for o in self.pending if o.order_id != oid]


def _repo():
    return Repo(Database(":memory:"))


def test_no_duplicate_while_intent_open():
    b, repo = FakeBroker(), _repo()
    om = OrderManager(b, repo, dry_run=False, kill_switch=False)
    req = OrderRequest("A_US_EQ", "BUY", 1, "LIMIT", limit_price=10)
    assert om.submit(session="d", ticker="A", req=req, purpose="ENTRY") is not None
    assert om.submit(session="d", ticker="A", req=req, purpose="ENTRY") is None  # blocked: intent open
    assert len(b.placed) == 1


def test_ambiguous_order_marked_unknown_and_blocks_retry():
    b, repo = FakeBroker(fail_ambiguous=True), _repo()
    om = OrderManager(b, repo, dry_run=False, kill_switch=False)
    req = OrderRequest("A_US_EQ", "BUY", 1, "LIMIT", limit_price=10)
    assert om.submit(session="d", ticker="A", req=req, purpose="ENTRY") is None
    assert repo.intents_by_status("UNKNOWN")
    b.fail = False
    assert om.submit(session="d", ticker="A", req=req, purpose="ENTRY") is None  # still blocked
    assert len(b.placed) == 1


def test_unknown_resolved_from_broker_history():
    b, repo = FakeBroker(fail_ambiguous=True), _repo()
    om = OrderManager(b, repo, dry_run=False, kill_switch=False)
    om.submit(session="d", ticker="A", req=OrderRequest("A_US_EQ", "BUY", 1, "LIMIT", limit_price=10), purpose="ENTRY")
    hist = [Order("555", "A_US_EQ", "LIMIT", "BUY", 1, 1, "FILLED", 10, None, datetime.now(timezone.utc), "API")]
    om.resolve_unknown([], hist)
    it = repo.db.query("SELECT * FROM order_intents")[0]
    assert it["status"] == "FILLED" and it["broker_order_id"] == "555"


def test_kill_switch_blocks_new_but_not_protective():
    b, repo = FakeBroker(), _repo()
    om = OrderManager(b, repo, dry_run=False, kill_switch=True)
    assert om.submit(session="d", ticker="A", req=OrderRequest("A_US_EQ", "BUY", 1, "LIMIT", limit_price=10), purpose="ENTRY") is None
    assert om.submit(session="d", ticker="A", purpose="STOP", protective=True,
                     req=OrderRequest("A_US_EQ", "SELL", 1, "STOP", stop_price=9)) is not None


def test_dry_run_sends_nothing():
    b, repo = FakeBroker(), _repo()
    om = OrderManager(b, repo, dry_run=True, kill_switch=False)
    om.submit(session="d", ticker="A", req=OrderRequest("A_US_EQ", "BUY", 1, "LIMIT", limit_price=10), purpose="ENTRY")
    assert not b.placed and repo.intents_by_status("DRY_RUN")


# ------------------------------------------------------------------ reconciliation / restart
def _pos(bt, q, px=100.0):
    return Position(bt, q, q, px, px, "USD", q * px / 1.3)


def test_unexpected_position_flagged_not_closed(cfg):
    b, repo = FakeBroker(), _repo()
    om = OrderManager(b, repo, dry_run=False, kill_switch=False)
    rep = reconcile(repo, om, [_pos("MANUAL_US_EQ", 5)], [], [], 1.3, cfg, CostModel(), {})
    assert rep.unexpected_positions == ["MANUAL_US_EQ"] and not b.placed and not rep.halt_new_orders


def test_entry_fill_creates_position_after_restart(cfg):
    b, repo = FakeBroker(), _repo()
    om = OrderManager(b, repo, dry_run=False, kill_switch=False)
    om.submit(session="d", ticker="A", req=OrderRequest("A_US_EQ", "BUY", 2, "LIMIT", limit_price=100.5), purpose="ENTRY",
              meta={"stop": 94.0, "target_r": 2.0, "sector": "Tech"})
    oid = b.placed and "101"
    # --- simulate a restart: new OrderManager, same DB; broker shows the fill in history
    om2 = OrderManager(FakeBroker(), repo, dry_run=False, kill_switch=False)
    filled = Order(oid, "A_US_EQ", "LIMIT", "BUY", 2, 2, "FILLED", 100.5, None, datetime.now(timezone.utc), "API",
                   fill_price=100.2, filled_at=datetime.now(timezone.utc), fx_rate=1.3)
    rep = reconcile(repo, om2, [_pos("A_US_EQ", 2, 100.2)], [], [filled], 1.3, cfg, CostModel(), {"A_US_EQ": "A"})
    pos = repo.open_positions()["A"]
    assert rep.opened == ["A"] and pos.qty == 2 and pos.stop == 94.0 and pos.target > pos.entry_px


def test_stop_fill_closes_position(cfg):
    repo = _repo()
    repo.upsert_position(HeldPosition("A", 2, 100, pd.Timestamp("2026-09-01"), 94, 94, 112, "Tech", 10, 100,
                                      broker_ticker="A_US_EQ"))
    sold = Order("7", "A_US_EQ", "STOP", "SELL", 2, 2, "FILLED", None, 94, datetime.now(timezone.utc), "API", fill_price=93.8)
    rep = reconcile(repo, OrderManager(FakeBroker(), repo, False, False), [], [], [sold], 1.3, cfg, CostModel(), {})
    assert rep.closed == ["A"] and "A" not in repo.open_positions()


def test_missing_position_without_history_halts(cfg):
    repo = _repo()
    repo.upsert_position(HeldPosition("A", 2, 100, pd.Timestamp("2026-09-01"), 94, 94, 112, "Tech", 10, 100,
                                      broker_ticker="A_US_EQ"))
    rep = reconcile(repo, OrderManager(FakeBroker(), repo, False, False), [], [], [], 1.3, cfg, CostModel(), {})
    assert rep.halt_new_orders and "A" in repo.open_positions()  # not silently dropped


def test_unknown_api_order_halts(cfg):
    repo = _repo()
    stray = Order("999", "Z_US_EQ", "LIMIT", "BUY", 1, 0, "NEW", 10, None, datetime.now(timezone.utc), "API")
    rep = reconcile(repo, OrderManager(FakeBroker(), repo, False, False), [], [stray], [], 1.3, cfg, CostModel(), {})
    assert rep.halt_new_orders and rep.unexpected_orders == ["999"]


# ------------------------------------------------------------------ time zones / earnings
def test_timezone_sessions():
    from src.data import calendar as cal
    # 2026-09-25 12:30 UTC (Friday, before NY open) -> last completed session is Thursday 24th
    assert str(cal.last_completed_session(datetime(2026, 9, 25, 12, 30, tzinfo=timezone.utc)).date()) == "2026-09-24"
    # 2026-09-25 21:00 UTC (after 16:00 NY close in EDT) -> Friday 25th
    assert str(cal.last_completed_session(datetime(2026, 9, 25, 21, 0, tzinfo=timezone.utc)).date()) == "2026-09-25"
    assert str(cal.next_session(pd.Timestamp("2026-09-25")).date()) == "2026-09-28"  # weekend skipped


def test_earnings_blackout():
    import numpy as np
    from src.data.earnings import next_earnings_within
    dates = pd.bdate_range("2026-01-05", periods=20)
    e = np.array([np.datetime64("2026-01-09")], dtype="datetime64[ns]")
    assert next_earnings_within(e, dates, 2, 3) is True  # Jan 7 + 3 sessions covers Jan 9
    assert next_earnings_within(e, dates, 0, 3) is False  # Jan 5 + 3 sessions = Jan 8
    assert next_earnings_within(None, dates, 0, 3) is None  # unknown calendar


def test_settings_numeric_types():
    """Guard against YAML quirks (e.g. '2.0e7' parses as a string in YAML 1.1)."""
    s = load_settings()
    def walk(d, path=""):
        for k, v in d.items():
            if isinstance(v, dict):
                walk(v, f"{path}{k}.")
            elif isinstance(v, str) and ":" not in v:  # times like "13:00" are legitimately strings
                try:
                    float(v)
                except ValueError:
                    continue
                raise AssertionError(f"{path}{k} looks numeric but is a string: {v!r}")
    walk(s)
    assert isinstance(s["universe"]["min_adv_usd"], (int, float)) and s["universe"]["min_adv_usd"] == 20_000_000

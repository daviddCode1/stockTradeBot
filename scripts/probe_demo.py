"""Probe the Trading 212 DEMO environment to verify broker behaviour the design depends on.

Read-only checks (always):  account summary, positions, pending orders, instruments, exchanges.
Order checks (--orders, DEMO ONLY): places orders at prices that cannot fill, then cancels them:
  1. fractional LIMIT BUY 0.05 AAPL @ $1.00      -> is fractional quantity accepted? price in USD?
  2. STOP SELL on an existing position @ $0.50  -> does it reserve quantityAvailableForTrading?
  3. LIMIT SELL of the full position @ 100x     -> can a second sell rest on the same shares?
All test orders are cancelled and the cancellation is verified. Refuses to run against live.
"""
from __future__ import annotations

import argparse  # CLI flags
import json  # output
import sys  # exit codes
import time  # small waits
from pathlib import Path  # project path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python scripts/probe_demo.py`

from src.broker.interface import BrokerError  # errors
from src.broker.models import OrderRequest  # order model
from src.broker.trading212_client import Trading212Broker  # adapter
from src.config import load_dotenv, load_settings  # settings

DEMO_URL = "https://demo.trading212.com/api/v0"  # hard-coded: this script never touches live


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", action="store_true", help="also run the (non-filling) demo order tests")
    args = ap.parse_args()
    load_dotenv()
    settings = load_settings()
    assert "demo." in DEMO_URL  # belt and braces
    b = Trading212Broker(DEMO_URL, timeout_s=float(settings["broker"]["request_timeout_seconds"]))
    report: dict = {}
    acct = b.account_summary()  # 1. account
    report["account"] = {"currency": acct.currency, "total_value": acct.total_value, "cash": acct.cash_available,
                         "id_last4": acct.account_id[-4:]}
    pos = b.positions()  # 2. positions
    report["positions"] = [{"ticker": p.broker_ticker, "qty": p.quantity, "avail": p.quantity_available,
                            "ccy": p.currency} for p in pos]
    report["pending_orders"] = len(b.pending_orders())  # 3. orders
    ins = b.instruments()  # 4. instruments + exchanges
    us = [i for i in ins if i.type == "STOCK" and i.currency == "USD"]
    report["instruments"] = {"total": len(ins), "usd_stocks": len(us),
                             "usd_stock_exchanges": sorted({i.exchange for i in us})}
    if args.orders:
        tests = report["order_tests"] = {}
        try:  # fractional limit buy far below market
            o = b.place_order(OrderRequest("AAPL_US_EQ", "BUY", 0.55, "LIMIT", limit_price=5.00, time_validity="DAY"))
            tests["fractional_limit_buy"] = {"accepted": True, "status": o.status, "qty": o.quantity, "limit": o.limit_price}
            time.sleep(2)
            b.cancel_order(o.order_id)
            tests["fractional_limit_buy"]["cancelled"] = o.order_id not in {x.order_id for x in b.pending_orders()}
        except BrokerError as e:
            tests["fractional_limit_buy"] = {"accepted": False, "error": str(e)[:300]}
        usd_pos = sorted([p for p in pos if p.currency == "USD" and p.quantity >= 1 and p.current_price > 50], key=lambda p: -p.current_price * p.quantity)
        if usd_pos:
            p = usd_pos[0]
            stop_id = None
            try:  # resting stop far below market
                s = b.place_order(OrderRequest(p.broker_ticker, "SELL", 1.0, "STOP", stop_price=round(p.current_price * 0.5, 2),
                                               time_validity="GOOD_TILL_CANCEL"))
                stop_id = s.order_id
                time.sleep(2)
                after = {x.broker_ticker: x for x in b.positions()}[p.broker_ticker]
                tests["stop_sell"] = {"accepted": True, "qty_before_avail": p.quantity_available,
                                      "qty_after_avail": after.quantity_available}
            except BrokerError as e:
                tests["stop_sell"] = {"accepted": False, "error": str(e)[:300]}
            tests["stop_sell"]["ticker"] = p.broker_ticker if "stop_sell" in tests else None
            time.sleep(4)
            try:  # second sell on the full position far above market
                lim = b.place_order(OrderRequest(p.broker_ticker, "SELL", p.quantity, "LIMIT",
                                                 limit_price=round(p.current_price * 3, 2), time_validity="DAY"))
                tests["second_sell_full_qty"] = {"accepted": True}
                time.sleep(2)
                b.cancel_order(lim.order_id)
            except BrokerError as e:
                tests["second_sell_full_qty"] = {"accepted": False, "error": str(e)[:300]}
            if stop_id:
                time.sleep(2)
                b.cancel_order(stop_id)
            time.sleep(6)
            tests["pending_after_cleanup"] = [(x.broker_ticker, x.type, x.status) for x in b.pending_orders()]
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

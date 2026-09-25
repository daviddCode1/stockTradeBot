"""Startup / pre-cycle reconciliation of local state against the broker.

Order: account -> positions -> pending orders -> recent history -> compare -> classify.
  * Unknown broker positions (e.g. manual trades) are FLAGGED and IGNORED - never closed.
  * A managed position missing at the broker is explained from history (stop hit / sold);
    if it cannot be explained, the cycle is HALTED for new orders (NEEDS_REVIEW).
  * Unresolved order outcomes are resolved before any new order can be sent.
"""
from __future__ import annotations

import json  # intent meta
import math  # finite checks
from dataclasses import dataclass, field  # report
from datetime import datetime, timedelta, timezone  # windows
from typing import Any, Mapping  # type hints

import pandas as pd  # timestamps

from ..backtest.costs import CostModel  # target maths
from ..broker.models import Order, Position  # broker models
from ..monitoring.logger import log_event  # structured logs
from ..portfolio.exposure import HeldPosition  # managed position
from ..risk.stops import net_risk_per_share_usd, target_price  # risk maths
from ..storage.repositories import Repo  # persistence
from .order_manager import OrderManager  # unknown-intent resolution


@dataclass
class ReconcileReport:
    """Outcome of one reconciliation pass."""

    halt_new_orders: bool = False  # True => do not open anything new this cycle
    unexpected_positions: list = field(default_factory=list)  # broker tickers not managed by the bot
    unexpected_orders: list = field(default_factory=list)  # API orders we have no record of
    opened: list = field(default_factory=list)  # entries confirmed filled
    closed: list = field(default_factory=list)  # positions confirmed closed
    notes: list = field(default_factory=list)  # human-readable notes


def reconcile(repo: Repo, om: OrderManager, positions: list[Position], pending: list[Order], history: list[Order],
              fx_usd_per_gbp: float, cfg: Mapping[str, Any], costs: CostModel,
              broker_to_symbol: dict[str, str]) -> ReconcileReport:
    """Bring the local database in line with the broker. Never places or cancels orders."""
    rep = ReconcileReport()
    om.resolve_unknown(pending, history)  # 1. settle ambiguous submissions first
    pend_ids = {o.order_id for o in pending}
    hist_by_id = {o.order_id: o for o in history}
    by_bt = {p.broker_ticker: p for p in positions}  # broker positions by instrument id

    # 2. update SUBMITTED intents from broker state
    for it in repo.intents_by_status("SUBMITTED"):
        oid = it["broker_order_id"]
        if oid in pend_ids:
            continue  # still working
        h = hist_by_id.get(oid)
        if h is None:  # not visible yet (history can lag)
            age = datetime.now(timezone.utc) - datetime.fromisoformat(it["created_at"])
            if age > timedelta(days=2) and it["purpose"] != "STOP":
                repo.update_intent(it["intent_id"], "NEEDS_REVIEW", note="not pending and not in history")
                repo.event("RECONCILIATION", it["ticker"], issue="order vanished", broker_id=oid)
                rep.halt_new_orders = True
            continue
        filled = h.filled_quantity or 0.0
        status = "FILLED" if filled > 0 and h.status == "FILLED" else h.status
        if filled > 0 and h.status != "FILLED":
            status = "PARTIALLY_FILLED_FINAL"  # e.g. DAY limit partly filled then expired
        repo.update_intent(it["intent_id"], status)
        if filled > 0 and h.fill_price:
            repo.save_fill(oid, it["ticker"], h.side, filled, float(h.fill_price), h.fx_rate, h.fees_account_ccy,
                           h.net_value_account_ccy, str(h.filled_at), h.type)
            log_event("ORDER_FILLED", "fill confirmed", ticker=it["ticker"], qty=filled, price=h.fill_price,
                      purpose=it["purpose"], fees_gbp=h.fees_account_ccy)
        meta = json.loads(it.get("meta") or "{}")
        if it["purpose"] == "ENTRY" and filled > 0 and h.fill_price:  # new managed position
            px = float(h.fill_price)
            stop = float(meta.get("stop", it.get("stop_px") or 0.0))
            fx = float(h.fx_rate) if h.fx_rate else fx_usd_per_gbp  # actual conversion rate when available
            k = float(meta.get("target_r", cfg["targets"]["r_multiple"]))
            pos = HeldPosition(
                ticker=it["ticker"], qty=filled, entry_px=px,
                entry_date=pd.Timestamp(h.filled_at.date() if h.filled_at else datetime.now(timezone.utc).date()),
                stop=stop, initial_stop=stop, target=target_price(px, stop, k, costs), sector=meta.get("sector", "UNKNOWN"),
                initial_risk_gbp=filled * net_risk_per_share_usd(px, stop, costs) / fx, highest_close=px,
                broker_ticker=it["broker_ticker"],
            )
            repo.upsert_position(pos, stop_order_id=None, target_r=k)  # stop is placed by the protect step
            log_event("POSITION_OPENED", "entry filled", ticker=it["ticker"], qty=filled, entry=px, stop=stop,
                      target=round(pos.target, 2), risk_gbp=round(pos.initial_risk_gbp, 2))
            rep.opened.append(it["ticker"])
        if it["purpose"] in ("EXIT", "STOP") and filled > 0 and h.fill_price:
            managed = repo.open_positions().get(it["ticker"])
            if managed and filled >= managed.qty - 1e-9:
                repo.close_position(it["ticker"], meta.get("reason", it["purpose"]), float(h.fill_price))
                log_event("POSITION_CLOSED", "position closed", ticker=it["ticker"], reason=meta.get("reason", it["purpose"]),
                          exit=h.fill_price)
                rep.closed.append(it["ticker"])
            elif managed:
                repo.update_qty(it["ticker"], managed.qty - filled)  # partial exit

    # 3. managed positions vs broker positions
    for t, p in repo.open_positions().items():
        bp = by_bt.get(p.broker_ticker)
        if bp is None or bp.quantity <= 1e-9:  # gone at the broker
            sells = [o for o in history if o.broker_ticker == p.broker_ticker and o.side == "SELL" and o.filled_quantity > 0]
            if sells:
                last = sells[0]  # history is newest first
                reason = "STOP" if last.type == "STOP" else "SOLD"
                repo.close_position(t, reason, float(last.fill_price) if last.fill_price else None)
                if last.type == "STOP":
                    log_event("STOP_TRIGGERED", "protective stop executed", ticker=t, price=last.fill_price)
                log_event("POSITION_CLOSED", "closed per broker history", ticker=t, reason=reason, exit=last.fill_price)
                rep.closed.append(t)
            else:
                repo.event("RECONCILIATION", t, issue="managed position missing at broker, no sell in history")
                log_event("RECONCILIATION", "NEEDS_REVIEW: managed position missing", ticker=t)
                rep.halt_new_orders = True
        elif bp.quantity < p.qty - 1e-9:  # broker holds fewer shares than we think
            repo.update_qty(t, bp.quantity)
            log_event("RECONCILIATION", "quantity adjusted to broker", ticker=t, local=p.qty, broker=bp.quantity)
            rep.notes.append(f"{t}: qty {p.qty} -> {bp.quantity}")

    # 4. broker positions the bot does not manage (manual trades): flag, never close
    managed_bt = {p.broker_ticker for p in repo.open_positions().values()}
    for bp in positions:
        if bp.broker_ticker not in managed_bt:
            rep.unexpected_positions.append(bp.broker_ticker)
    if rep.unexpected_positions:
        log_event("RECONCILIATION", "unmanaged positions present (policy: ignore, never close)",
                  count=len(rep.unexpected_positions))

    # 5. API orders at the broker that we have no record of
    known = {i["broker_order_id"] for i in repo.open_intents() + repo.intents_by_status("SUBMITTED")}
    known |= {p.extra.get("stop_order_id") for p in repo.open_positions().values()}
    for o in pending:
        if o.initiated_from == "API" and o.order_id not in known:
            rep.unexpected_orders.append(o.order_id)
    if rep.unexpected_orders:
        repo.event("RECONCILIATION", None, issue="unknown API orders", ids=rep.unexpected_orders)
        log_event("RECONCILIATION", "NEEDS_REVIEW: unknown API orders pending", ids=rep.unexpected_orders)
        rep.halt_new_orders = True
    if any(i["status"] == "UNKNOWN" for i in repo.open_intents()):
        rep.halt_new_orders = True  # ambiguous submissions still unresolved
        rep.notes.append("unresolved UNKNOWN intents - no new orders this cycle")
    log_event("RECONCILIATION", "reconciliation complete", halt=rep.halt_new_orders, opened=rep.opened,
              closed=rep.closed, unmanaged=len(rep.unexpected_positions))
    return rep

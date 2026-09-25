"""Order submission with duplicate-order protection (Trading 212 order endpoints are NOT idempotent).

Protocol for every order:
  1. Refuse if an open intent already exists for the same ticker+purpose (one at a time).
  2. Write an intent row (PENDING_SUBMIT) BEFORE touching the network.
  3. Send ONCE.
       success          -> SUBMITTED + broker id
       definite failure -> REJECTED (400/401/403/429: the broker did not process it)
       ambiguous        -> UNKNOWN (timeout, 5xx, dropped connection) - NEVER retried here
  4. UNKNOWN intents are resolved by `resolve_unknown` using pending orders, order history
     and positions. Only if the broker clearly has no such order is it marked NOT_PLACED;
     a fresh intent can then be created in a LATER decision cycle, never in the same one.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone  # time windows
from typing import Optional  # type hints

from ..broker.interface import Broker, BrokerError  # broker contract
from ..broker.models import Order, OrderRequest  # models
from ..monitoring.logger import log_event  # structured logs
from ..storage.repositories import Repo  # persistence


class OrderManager:
    """Single entry point for placing and cancelling orders."""

    def __init__(self, broker: Broker, repo: Repo, dry_run: bool, kill_switch: bool):
        self.broker = broker  # execution venue
        self.repo = repo  # intent store
        self.dry_run = dry_run  # log only, never send
        self.kill_switch = kill_switch  # block all new orders

    def submit(self, *, session: str, ticker: str, req: OrderRequest, purpose: str,
               protective: bool = False, meta: dict | None = None) -> Optional[Order]:
        """Submit one order following the intent protocol. Returns the broker order or None."""
        if self.kill_switch and not protective:  # kill switch: nothing new (protective stops still allowed)
            log_event("KILL_SWITCH", "order blocked by kill switch", ticker=ticker, purpose=purpose)
            return None
        dupes = [i for i in self.repo.open_intents(ticker) if i["purpose"] == purpose]  # same thing in flight?
        if dupes:
            log_event("TRADE_REJECTED", "open intent already exists - not sending a duplicate",
                      ticker=ticker, purpose=purpose, status=dupes[0]["status"])
            return None
        iid = self.repo.create_intent(session=session, ticker=ticker, broker_ticker=req.broker_ticker, side=req.side,
                                      qty=req.quantity, order_type=req.order_type, purpose=purpose,
                                      limit_px=req.limit_price, stop_px=req.stop_price, meta=meta)  # durable BEFORE sending
        if self.dry_run:
            self.repo.update_intent(iid, "DRY_RUN", note="not sent (DRY_RUN)")
            log_event("ORDER_SUBMITTED", "DRY RUN - order not sent", ticker=ticker, purpose=purpose, side=req.side,
                      qty=req.quantity, type=req.order_type, limit=req.limit_price, stop=req.stop_price)
            return None
        try:
            order = self.broker.place_order(req)  # exactly one attempt
        except BrokerError as exc:
            if exc.ambiguous:
                self.repo.update_intent(iid, "UNKNOWN", note=str(exc)[:200])
                log_event("API_ERROR", "order outcome UNKNOWN - will reconcile, not retry", ticker=ticker, purpose=purpose)
            else:
                self.repo.update_intent(iid, "REJECTED", note=str(exc)[:300])
                log_event("ORDER_REJECTED", "broker rejected order", ticker=ticker, purpose=purpose,
                          status=exc.status, error=str(exc)[:200])
            return None
        self.repo.update_intent(iid, "SUBMITTED", broker_order_id=order.order_id)
        log_event("ORDER_SUBMITTED", "order accepted", ticker=ticker, purpose=purpose, side=req.side, qty=req.quantity,
                  type=req.order_type, limit=req.limit_price, stop=req.stop_price, broker_id=order.order_id)
        return order

    def cancel_and_verify(self, order_id: str, ticker: str) -> bool:
        """Cancel an order and confirm it is no longer pending. False => uncertain, caller must not proceed."""
        if self.dry_run:
            log_event("ORDER_CANCELLED", "DRY RUN - cancel not sent", ticker=ticker, broker_id=order_id)
            return True
        try:
            self.broker.cancel_order(order_id)
        except BrokerError as exc:
            log_event("API_ERROR", "cancel failed", ticker=ticker, broker_id=order_id, error=str(exc)[:200])
            return False
        still = [o for o in self.broker.pending_orders() if o.order_id == str(order_id)]  # verify
        if still and still[0].status not in ("CANCELLED",):
            log_event("API_ERROR", "order still pending after cancel", ticker=ticker, broker_id=order_id,
                      status=still[0].status)
            return False
        log_event("ORDER_CANCELLED", "cancel verified", ticker=ticker, broker_id=order_id)
        return True

    def resolve_unknown(self, pending: list[Order], history: list[Order]) -> None:
        """Match UNKNOWN intents to broker evidence (pending orders, then history)."""
        for it in self.repo.intents_by_status("UNKNOWN"):
            created = datetime.fromisoformat(it["created_at"])
            window_start = created - timedelta(minutes=2)  # broker clock skew allowance
            def matches(o: Order) -> bool:  # same instrument, side, qty, type, placed via API around then
                return (o.broker_ticker == it["broker_ticker"] and o.side == it["side"]
                        and abs(o.quantity - it["qty"]) < 1e-6 and o.type == it["order_type"]
                        and o.initiated_from in ("API", "") and (o.created_at is None or o.created_at >= window_start))
            hit = next((o for o in pending if matches(o)), None) or next((o for o in history if matches(o)), None)
            if hit:
                status = "SUBMITTED" if hit.is_open else hit.status
                self.repo.update_intent(it["intent_id"], status, broker_order_id=hit.order_id, note="resolved from broker state")
                log_event("RECONCILIATION", "UNKNOWN intent matched broker order", ticker=it["ticker"],
                          broker_id=hit.order_id, status=status)
            elif datetime.now(timezone.utc) - created > timedelta(minutes=10):  # enough time for it to appear
                self.repo.update_intent(it["intent_id"], "NOT_PLACED", note="no broker evidence after 10 minutes")
                log_event("RECONCILIATION", "UNKNOWN intent not found at broker - marked NOT_PLACED", ticker=it["ticker"])
            # else: too recent to decide; leave UNKNOWN (blocks duplicates for this ticker/purpose)

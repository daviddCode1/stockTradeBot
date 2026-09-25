"""Repositories: typed read/write helpers over the database (no SQL outside storage/)."""
from __future__ import annotations

import json  # serialise details
import uuid  # intent ids
from datetime import datetime, timezone  # timestamps
from typing import Any, Iterable  # type hints

import pandas as pd  # timestamps

from ..portfolio.exposure import HeldPosition  # position record
from .database import Database  # connection

# Intent lifecycle. UNKNOWN = we could not tell whether the broker accepted it (never retried blindly).
INTENT_OPEN = ("PENDING_SUBMIT", "SUBMITTED", "UNKNOWN")


def now_iso() -> str:
    """Current UTC time as ISO text."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repo:
    """All persistence used by the execution layer."""

    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------------- events / state
    def event(self, event: str, ticker: str | None = None, **detail: Any) -> None:
        self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (now_iso(), event, ticker, json.dumps(detail, default=str)))

    def get_state(self, key: str, default: str | None = None) -> str | None:
        rows = self.db.query("SELECT value FROM state WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_state(self, key: str, value: str | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, value))

    # ---------------------------------------------------------------- snapshots / pnl
    def snapshot(self, mode: str, account_ref: str, currency: str, total: float, cash: float, invested: float,
                 strategy_equity: float) -> None:
        self.db.execute("INSERT INTO account_snapshots VALUES (?,?,?,?,?,?,?,?)",
                        (now_iso(), mode, account_ref, currency, total, cash, invested, strategy_equity))

    def last_daily_pnl(self, before_session: str) -> dict | None:
        rows = self.db.query("SELECT * FROM daily_pnl WHERE session < ? ORDER BY session DESC LIMIT 1", (before_session,))
        return dict(rows[0]) if rows else None

    def save_daily_pnl(self, session: str, equity: float, cash: float, managed: float, heat: float, n: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO daily_pnl VALUES (?,?,?,?,?,?)", (session, equity, cash, managed, heat, n))

    def save_params(self, settings: dict) -> None:
        self.db.execute("INSERT INTO params VALUES (?,?)", (now_iso(), json.dumps(settings, default=str)))

    def save_signals(self, session: str, rows: Iterable[tuple]) -> None:
        self.db.conn.executemany("INSERT INTO signals VALUES (?,?,?,?,?,?)", [(session, *r) for r in rows])
        self.db.conn.commit()

    def save_universe(self, session: str, mapping: dict[str, str]) -> None:
        self.db.conn.executemany("INSERT OR REPLACE INTO universe VALUES (?,?,?)",
                                 [(session, t, b) for t, b in mapping.items()])
        self.db.conn.commit()

    def risk_log(self, ticker: str, **details: Any) -> None:
        self.db.execute("INSERT INTO risk_log VALUES (?,?,?)", (now_iso(), ticker, json.dumps(details, default=str)))

    # ---------------------------------------------------------------- intents (duplicate protection)
    def create_intent(self, *, session: str, ticker: str, broker_ticker: str, side: str, qty: float, order_type: str,
                      purpose: str, limit_px: float | None = None, stop_px: float | None = None,
                      meta: dict | None = None) -> str:
        """Record the intent BEFORE sending anything to the broker."""
        iid = uuid.uuid4().hex  # unique local id
        self.db.execute(
            "INSERT INTO order_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, now_iso(), session, ticker, broker_ticker, side, qty, order_type, limit_px, stop_px, purpose,
             "PENDING_SUBMIT", None, now_iso(), None, json.dumps(meta or {}, default=str)))  # meta: stop/target/sector
        return iid

    def update_intent(self, iid: str, status: str, broker_order_id: str | None = None, note: str | None = None) -> None:
        self.db.execute("UPDATE order_intents SET status=?, broker_order_id=COALESCE(?, broker_order_id), "
                        "updated_at=?, note=COALESCE(?, note) WHERE intent_id=?",
                        (status, broker_order_id, now_iso(), note, iid))

    def open_intents(self, ticker: str | None = None) -> list[dict]:
        sql = f"SELECT * FROM order_intents WHERE status IN {INTENT_OPEN}"
        rows = self.db.query(sql + (" AND ticker=?" if ticker else ""), (ticker,) if ticker else ())
        return [dict(r) for r in rows]

    def intents_by_status(self, status: str) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM order_intents WHERE status=?", (status,))]

    def save_fill(self, broker_order_id: str, ticker: str, side: str, qty: float, price: float, fx_rate: float | None,
                  fees: float, net_value: float | None, filled_at: str | None, order_type: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (broker_order_id, ticker, side, qty, price, fx_rate, fees, net_value, filled_at, order_type))

    # ---------------------------------------------------------------- positions
    def open_positions(self) -> dict[str, HeldPosition]:
        out = {}
        for r in self.db.query("SELECT * FROM positions WHERE status='OPEN'"):
            out[r["ticker"]] = HeldPosition(
                ticker=r["ticker"], qty=r["qty"], entry_px=r["entry_px"], entry_date=pd.Timestamp(r["entry_date"]),
                stop=r["stop"], initial_stop=r["initial_stop"], target=r["target"], sector=r["sector"] or "UNKNOWN",
                initial_risk_gbp=r["initial_risk_gbp"] or 0.0, highest_close=r["highest_close"] or r["entry_px"],
                partial_done=bool(r["partial_done"]), broker_ticker=r["broker_ticker"],
                extra={"stop_order_id": r["stop_order_id"], "target_r": r["target_r"]},
            )
        return out

    def upsert_position(self, p: HeldPosition, stop_order_id: str | None = None, target_r: float | None = None) -> None:
        """Update the OPEN row for this ticker, or insert a new one (closed rows are kept for audit)."""
        sid = stop_order_id if stop_order_id is not None else p.extra.get("stop_order_id")
        tr = target_r if target_r is not None else p.extra.get("target_r")
        vals = (p.broker_ticker, p.qty, p.entry_px, str(pd.Timestamp(p.entry_date).date()), p.stop, p.initial_stop,
                p.target, p.sector, p.initial_risk_gbp, p.highest_close, int(p.partial_done), sid, tr)
        existing = self.db.query("SELECT id FROM positions WHERE ticker=? AND status='OPEN'", (p.ticker,))
        if existing:  # update in place
            self.db.execute("UPDATE positions SET broker_ticker=?, qty=?, entry_px=?, entry_date=?, stop=?, initial_stop=?, "
                            "target=?, sector=?, initial_risk_gbp=?, highest_close=?, partial_done=?, stop_order_id=?, "
                            "target_r=? WHERE id=?", (*vals, existing[0]["id"]))
        else:  # new position
            self.db.execute("INSERT INTO positions (broker_ticker, qty, entry_px, entry_date, stop, initial_stop, target, "
                            "sector, initial_risk_gbp, highest_close, partial_done, stop_order_id, target_r, ticker, "
                            "status, opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?)",
                            (*vals, p.ticker, now_iso()))

    def set_stop_order(self, ticker: str, order_id: str | None, stop: float | None = None) -> None:
        if stop is None:
            self.db.execute("UPDATE positions SET stop_order_id=? WHERE ticker=? AND status='OPEN'", (order_id, ticker))
        else:
            self.db.execute("UPDATE positions SET stop_order_id=?, stop=? WHERE ticker=? AND status='OPEN'", (order_id, stop, ticker))

    def close_position(self, ticker: str, reason: str, exit_px: float | None) -> None:
        self.db.execute("UPDATE positions SET status='CLOSED', closed_at=?, exit_reason=?, exit_px=? "
                        "WHERE ticker=? AND status='OPEN'", (now_iso(), reason, exit_px, ticker))

    def update_qty(self, ticker: str, qty: float) -> None:
        self.db.execute("UPDATE positions SET qty=? WHERE ticker=? AND status='OPEN'", (qty, ticker))

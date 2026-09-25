"""Health check: configuration, kill switch, broker reachability (read-only)."""
from __future__ import annotations

from typing import Any  # type hints

from ..broker.interface import Broker, BrokerError  # broker contract
from ..config import RuntimeContext  # runtime context
from ..data import calendar as cal  # market hours


def health_check(ctx: RuntimeContext, broker: Broker) -> dict[str, Any]:
    """Return a dict describing whether the bot could run right now. Never places orders."""
    out: dict[str, Any] = {"mode": ctx.mode, "base_url": ctx.base_url, "dry_run": ctx.dry_run,
                           "kill_switch": ctx.kill_switch, "account_type_declared": ctx.account_type}
    try:
        a = broker.account_summary()  # read-only call
        out["broker"] = "OK"
        out["account"] = {"currency": a.currency, "total_value": a.total_value, "cash_available": a.cash_available,
                          "id_matches": (ctx.expected_account_id == a.account_id) if ctx.expected_account_id else "not pinned"}
    except BrokerError as exc:
        out["broker"] = f"ERROR: {exc}"
    try:
        out["market_open"] = cal.market_is_open()
        out["last_completed_session"] = str(cal.last_completed_session().date())
    except Exception as exc:  # calendar problems must not crash the health check
        out["calendar"] = f"ERROR: {exc}"
    return out

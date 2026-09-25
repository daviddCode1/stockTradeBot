"""Emergency kill switch.

Activate:   python -m src.main kill on      (creates the file ./KILL_SWITCH)
            or set KILL_SWITCH=true in the environment
Deactivate: python -m src.main kill off     (removes the file; env var must be unset manually)

While active: no new orders are submitted, protective stops stay in place at the broker,
and monitoring/reconciliation continue.
"""
from __future__ import annotations

from ..config import PROJECT_ROOT, kill_switch_active  # shared definition of "active"
from .logger import log_event  # structured logging

KILL_FILE = PROJECT_ROOT / "KILL_SWITCH"  # marker file location (git-ignored)


def activate(reason: str = "manual") -> None:
    """Turn the kill switch on by creating the marker file."""
    KILL_FILE.write_text(reason + "\n", encoding="utf-8")  # file content records why
    log_event("KILL_SWITCH", "kill switch activated", reason=reason)  # audit trail


def deactivate() -> None:
    """Turn the file-based kill switch off (an env-var kill switch must be removed by the operator)."""
    if KILL_FILE.exists():  # only delete if present
        KILL_FILE.unlink()
    log_event("KILL_SWITCH", "kill switch file removed")


def is_active() -> bool:
    """True if either the env var or the file says the kill switch is on."""
    return kill_switch_active()

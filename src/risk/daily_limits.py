"""Daily portfolio loss limit: after a -1.5% day, no new buys until the next day (and reset if required)."""
from __future__ import annotations

from dataclasses import dataclass  # state container


@dataclass
class DailyLossGuard:
    """Tracks whether new purchases are blocked by the daily loss limit."""

    max_daily_loss: float = 0.015  # 1.5% of equity
    requires_reset: bool = False  # True => stays blocked until reset() is called explicitly
    blocked: bool = False  # current state

    def update(self, equity_today: float, equity_prev: float) -> bool:
        """Evaluate today's P&L. Returns True if the limit was hit today."""
        if equity_prev <= 0:
            return False  # nothing to compare against
        change = equity_today / equity_prev - 1.0  # daily return
        hit = change <= -self.max_daily_loss  # breached?
        if hit:
            self.blocked = True  # block new purchases
        elif not self.requires_reset:
            self.blocked = False  # auto-clear on a normal day when no manual reset is required
        return hit

    def reset(self) -> None:
        """Operator reset (python -m src.main reset-daily-limit)."""
        self.blocked = False

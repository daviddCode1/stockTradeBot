"""Exchange calendar and timezone handling.

All trading-day logic uses the NYSE calendar in America/New_York. The operator's clock is
Europe/London; UK and US daylight-saving changes happen on different dates, so the gap
between them is 4 or 5 hours depending on the week. Never hard-code "21:00 UK = US close".
"""
from __future__ import annotations

from datetime import datetime, timezone  # aware datetimes
from zoneinfo import ZoneInfo  # IANA time zones

import pandas as pd  # timestamps

NY = ZoneInfo("America/New_York")  # exchange time zone
LONDON = ZoneInfo("Europe/London")  # operator time zone


def _calendar():
    """Lazily load the NYSE calendar (exchange_calendars)."""
    import exchange_calendars as xcals  # imported lazily: heavy module
    return xcals.get_calendar("XNYS")


def is_trading_day(day: pd.Timestamp) -> bool:
    """True if NYSE is open on this calendar date."""
    return bool(_calendar().is_session(pd.Timestamp(day).normalize()))


def last_completed_session(now_utc: datetime | None = None) -> pd.Timestamp:
    """Most recent NYSE session whose close has already happened (the session we may trade off)."""
    now = now_utc or datetime.now(timezone.utc)  # current instant
    cal = _calendar()
    today = pd.Timestamp(now.astimezone(NY).date())  # exchange-local date
    if cal.is_session(today) and pd.Timestamp(now) >= cal.session_close(today):  # today's close has passed
        return today
    return cal.previous_session(today) if cal.is_session(today) else cal.date_to_session(today, direction="previous")


def next_session(day: pd.Timestamp) -> pd.Timestamp:
    """The next NYSE session after `day` (the execution day for a signal on `day`)."""
    return _calendar().next_session(pd.Timestamp(day).normalize())


def market_is_open(now_utc: datetime | None = None) -> bool:
    """True during regular NYSE hours (09:30-16:00 New York, early closes respected)."""
    now = pd.Timestamp(now_utc or datetime.now(timezone.utc))
    return bool(_calendar().is_open_on_minute(now.floor("min")))


def to_london(ts: datetime) -> datetime:
    """Convert an aware datetime to UK local time (for operator-facing reports)."""
    return ts.astimezone(LONDON)

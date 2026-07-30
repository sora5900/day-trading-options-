"""Trading-calendar gating (spec §8): real NYSE calendar, not a weekday test.

Uses pandas_market_calendars. All timestamps in this codebase are ISO-8601 UTC
strings ("2026-07-30T14:35:00Z"); helpers here convert to/from US/Eastern.
"""

from datetime import datetime, timezone, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

ET = ZoneInfo("America/New_York")
_NYSE = mcal.get_calendar("NYSE")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_et(ts: str) -> datetime:
    return parse_ts(ts).astimezone(ET)


def trading_day_of(ts: str) -> str:
    """The ET calendar date a UTC timestamp belongs to (split unit is the day)."""
    return to_et(ts).strftime("%Y-%m-%d")


@lru_cache(maxsize=8)
def _schedule(start: str, end: str):
    return _NYSE.schedule(start_date=start, end_date=end)


def session_bounds(date_et: str):
    """(open_utc, close_utc) datetimes for an ET date, or None if not a session.

    Handles half days correctly because it comes from the exchange schedule.
    """
    sched = _schedule(date_et, date_et)
    if sched.empty:
        return None
    row = sched.iloc[0]
    return (row["market_open"].to_pydatetime(),
            row["market_close"].to_pydatetime())


def is_trading_day(date_et: str) -> bool:
    return session_bounds(date_et) is not None


def is_market_open(dt: datetime | None = None) -> bool:
    dt = dt or datetime.now(timezone.utc)
    bounds = session_bounds(dt.astimezone(ET).strftime("%Y-%m-%d"))
    if bounds is None:
        return False
    open_, close_ = bounds
    return open_ <= dt < close_


def minutes_since_open(ts: str) -> float | None:
    """Minutes since today's session open; None if not a session day."""
    dt = parse_ts(ts)
    bounds = session_bounds(dt.astimezone(ET).strftime("%Y-%m-%d"))
    if bounds is None:
        return None
    return (dt - bounds[0]).total_seconds() / 60.0


def et_time_reached(ts: str, hhmm: str) -> bool:
    """True if `ts` is at/after HH:MM ET on its own trading day."""
    et = to_et(ts)
    h, m = map(int, hhmm.split(":"))
    return (et.hour, et.minute) >= (h, m)


def trading_days_between(start_et: str, end_et: str) -> list[str]:
    sched = _NYSE.schedule(start_date=start_et, end_date=end_et)
    return [d.strftime("%Y-%m-%d") for d in sched.index]

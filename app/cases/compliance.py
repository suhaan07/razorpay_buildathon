"""Stopping rules: quiet hours (IST) and a max-touch cap that forces a case
to human review rather than escalating forever."""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _parse_hhmm(value: str) -> dt.time:
    hour, minute = value.split(":")
    return dt.time(int(hour), int(minute))


def _quiet_window() -> tuple[dt.time, dt.time]:
    start = _parse_hhmm(os.getenv("QUIET_HOURS_START", "21:00"))
    end = _parse_hhmm(os.getenv("QUIET_HOURS_END", "09:00"))
    return start, end


def max_touch_cap() -> int:
    return int(os.getenv("MAX_TOUCH_CAP", "6"))


def in_quiet_hours(now_utc: dt.datetime) -> bool:
    start, end = _quiet_window()
    now_ist = now_utc.replace(tzinfo=dt.timezone.utc).astimezone(IST).time()
    if start <= end:
        return start <= now_ist < end
    return now_ist >= start or now_ist < end  # window wraps midnight


def next_available_time(now_utc: dt.datetime) -> dt.datetime:
    """First UTC instant at/after now_utc that falls outside quiet hours."""
    if not in_quiet_hours(now_utc):
        return now_utc

    start, end = _quiet_window()
    now_ist = now_utc.replace(tzinfo=dt.timezone.utc).astimezone(IST)

    # Same-day window (start <= end): quiet hours end later today.
    # Overnight window (start > end): if we're in the evening leg (>= start),
    # quiet hours end tomorrow; if we're in the early-morning leg (< end),
    # they end later today.
    resume_date = now_ist.date()
    if start > end and now_ist.time() >= start:
        resume_date += dt.timedelta(days=1)

    resume_ist = dt.datetime.combine(resume_date, end, tzinfo=IST)
    return resume_ist.astimezone(dt.timezone.utc).replace(tzinfo=None)

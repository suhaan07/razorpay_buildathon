"""Turns a free-text date phrase from a WhatsApp promise-to-pay message into
a concrete date. Deliberately a small, hand-written, fully-enumerated set of
patterns rather than a general natural-language date parser (e.g. dateutil)
— a fuzzy parser can silently misread an ambiguous string (is "3/4" March 4
or April 3?) and produce a confidently wrong promised date, which is worse
than admitting "couldn't understand" and asking the customer to rephrase.

Every result is validated against `today` before being returned: a date in
the past, or implausibly far in the future (a likely typo, e.g. a wrong
year), is rejected rather than silently accepted."""

from __future__ import annotations

import datetime as dt
import re

_MAX_DAYS_OUT = 365

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_IN_DAYS_RE = re.compile(r"^in\s+(\d+)\s+days?$")
_IN_WEEKS_RE = re.compile(r"^in\s+(\d+)\s+weeks?$")
_NUMERIC_RE = re.compile(r"^(\d{1,2})[-/\s](\d{1,2})(?:[-/\s](\d{2,4}))?$")
_DAY_MONTH_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[-\s]([a-zA-Z]+)(?:[-\s](\d{2,4}))?$")
_MONTH_DAY_RE = re.compile(r"^([a-zA-Z]+)[-\s](\d{1,2})(?:st|nd|rd|th)?(?:[-\s,]+(\d{2,4}))?$")


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        first_of_next = dt.date(year + 1, 1, 1)
    else:
        first_of_next = dt.date(year, month + 1, 1)
    return (first_of_next - dt.timedelta(days=1)).day


def _resolve_weekday(weekday_index: int, today: dt.date) -> dt.date:
    delta = (weekday_index - today.weekday()) % 7  # 0 if it's today
    return today + dt.timedelta(days=delta)


def _try_numeric_date(day: int, month: int, year: str | None, today: dt.date) -> dt.date | None:
    if not (1 <= month <= 12):
        return None
    try:
        if year:
            y = int(year)
            if y < 100:
                y += 2000
        else:
            y = today.year
        candidate = dt.date(y, month, day)
    except ValueError:
        return None
    if year is None and candidate < today:
        # no year given and it's already passed this year -> assume next year
        try:
            candidate = dt.date(today.year + 1, month, day)
        except ValueError:
            return None
    return candidate


def parse_date_phrase(text: str, today: dt.date) -> dt.date | None:
    """Returns a validated future-or-today date, or None if the phrase
    isn't recognized or resolves to something implausible (past, or more
    than a year out)."""
    phrase = text.strip().lower().rstrip(".!?")
    if not phrase:
        return None

    candidate: dt.date | None = None

    if phrase == "today":
        candidate = today
    elif phrase == "tomorrow":
        candidate = today + dt.timedelta(days=1)
    elif phrase in ("next week",):
        candidate = today + dt.timedelta(days=7)
    elif phrase in ("end of month", "end of the month", "eom"):
        last_day = _last_day_of_month(today.year, today.month)
        candidate = dt.date(today.year, today.month, last_day)
    elif phrase in _WEEKDAYS:
        candidate = _resolve_weekday(_WEEKDAYS[phrase], today)
    elif phrase.startswith("next ") and phrase[5:] in _WEEKDAYS:
        same_week = _resolve_weekday(_WEEKDAYS[phrase[5:]], today)
        # "next <weekday>" always means the occurrence AFTER this week's,
        # even said on that weekday itself — distinct from bare "<weekday>".
        candidate = same_week if same_week != today else same_week + dt.timedelta(days=7)
    else:
        m = _IN_DAYS_RE.match(phrase)
        if m:
            candidate = today + dt.timedelta(days=int(m.group(1)))
        else:
            m = _IN_WEEKS_RE.match(phrase)
            if m:
                candidate = today + dt.timedelta(weeks=int(m.group(1)))
            else:
                m = _NUMERIC_RE.match(phrase)
                if m:
                    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
                    candidate = _try_numeric_date(day, month, year, today)
                else:
                    m = _DAY_MONTH_RE.match(phrase)
                    if m and m.group(2) in _MONTHS:
                        day, month, year = int(m.group(1)), _MONTHS[m.group(2)], m.group(3)
                        candidate = _try_numeric_date(day, month, year, today)
                    else:
                        m = _MONTH_DAY_RE.match(phrase)
                        if m and m.group(1) in _MONTHS:
                            month, day, year = _MONTHS[m.group(1)], int(m.group(2)), m.group(3)
                            candidate = _try_numeric_date(day, month, year, today)

    if candidate is None:
        return None
    if candidate < today:
        return None
    if (candidate - today).days > _MAX_DAYS_OUT:
        return None
    return candidate

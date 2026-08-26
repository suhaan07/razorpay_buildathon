"""Pure date/bucket math. No DB access, no I/O — always evaluated against
a `today` passed in by the caller (never cached, never hardcoded)."""

from __future__ import annotations

import datetime as dt

BUCKET_ORDER = ["90+", "61-90", "31-60", "16-30", "0-15", "Not Due", "Unclassified"]

_BOUNDARIES = [
    (90, "90+"),
    (60, "61-90"),
    (30, "31-60"),
    (15, "16-30"),
    (0, "0-15"),
]


def is_overdue(due_date: dt.date | None, today: dt.date) -> bool:
    if due_date is None:
        return False
    return due_date < today


def days_overdue(due_date: dt.date | None, today: dt.date) -> int:
    if due_date is None or not is_overdue(due_date, today):
        return 0
    return (today - due_date).days


def week_window(today: dt.date) -> tuple[dt.date, dt.date]:
    monday = today - dt.timedelta(days=today.weekday())
    friday = monday + dt.timedelta(days=4)
    return monday, friday


def is_due_this_week(due_date: dt.date | None, today: dt.date) -> bool:
    if due_date is None:
        return False
    monday, friday = week_window(today)
    return monday <= due_date <= friday


def bucket_for(due_date: dt.date | None, today: dt.date) -> str:
    if due_date is None:
        return "Unclassified"
    if due_date >= today:
        return "Not Due"
    overdue_days = (today - due_date).days
    for threshold, label in _BOUNDARIES:
        if overdue_days > threshold:
            return label
    return "0-15"

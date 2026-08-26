import datetime as dt

from app.reports.format_utils import format_ist


def test_format_ist_converts_utc_to_ist_offset():
    # IST is UTC+5:30 — 18:35 UTC lands on the next calendar day at 00:05 IST
    value = dt.datetime(2026, 8, 24, 18, 35)
    assert format_ist(value) == "25-Aug-2026 00:05 IST"


def test_format_ist_without_date_omits_the_date_portion():
    value = dt.datetime(2026, 8, 24, 10, 0)
    assert format_ist(value, with_date=False) == "24-Aug 15:30 IST"


def test_format_ist_none_returns_placeholder():
    assert format_ist(None) == "-"

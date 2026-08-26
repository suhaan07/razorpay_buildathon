from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def format_date(value: dt.date | None) -> str:
    """29-Jun-2026 style; N/A for missing dates."""
    return value.strftime("%d-%b-%Y") if value else "N/A"


def format_ist(value: dt.datetime | None, *, with_date: bool = True) -> str:
    """Every stored timestamp is naive UTC (dt.datetime.utcnow()) — this
    localizes it to UTC first, then converts to IST for display, since
    every schedule/timeline time shown in the portal should read in the
    timezone the team actually operates in, not the server's UTC clock."""
    if value is None:
        return "-"
    aware = value.replace(tzinfo=dt.timezone.utc).astimezone(IST)
    fmt = "%d-%b-%Y %H:%M IST" if with_date else "%d-%b %H:%M IST"
    return aware.strftime(fmt)


def assemble_messages(headline: str, invoice_list: str, invoice_count: int, inline_limit: int = 8) -> list[str]:
    """<=inline_limit invoices: append the list to the headline so it reads
    as one message. More than that: send the invoice list as a second,
    separate WhatsApp message so the headline numbers stay scannable on a
    phone screen."""

    if invoice_count <= inline_limit:
        return [f"{headline}\n\n{invoice_list}"]
    return [headline, invoice_list]


def format_inr(value: float) -> str:
    """Indian digit grouping, e.g. 495009.38 -> ₹4,95,009.38"""
    negative = value < 0
    value = abs(value)
    whole = int(value)
    frac = round((value - whole) * 100)
    if frac == 100:
        whole += 1
        frac = 0

    whole_str = str(whole)
    if len(whole_str) <= 3:
        grouped = whole_str
    else:
        last3, rest = whole_str[-3:], whole_str[:-3]
        parts: list[str] = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ",".join(parts) + "," + last3

    result = f"₹{grouped}.{frac:02d}"
    return f"-{result}" if negative else result

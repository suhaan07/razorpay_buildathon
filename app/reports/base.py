"""The single seam between "get data" and "format data" for the WhatsApp
Q&A bot — computes every number both report types need. Formatters never
touch the DB or do date math; they only format an already-built
CustomerReportData (ported from the ProcWing pattern, see CONTEXT.md)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.billing import get_or_create_consolidated_link
from app.data.ageing import bucket_for, is_due_this_week, is_overdue, week_window
from app.data.models import Customer
from app.integrations.razorpay_client import is_oversized_stub
from app.matching.resolver import resolve

AGEING_LABELS = ["90+", "61-90", "31-60", "16-30", "0-15"]
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


@dataclass
class InvoiceLine:
    invoice_no: str
    due_date: dt.date | None
    outstanding: float


@dataclass
class CustomerReportData:
    customer_name: str
    spoc: str
    overdue_amount: float
    due_this_week_amount: float
    due_this_week_start: dt.date
    due_this_week_end: dt.date
    total_outstanding: float
    ageing_breakdown: dict[str, float]
    monday_baseline_overdue: float
    day_amounts: dict[str, float]
    friday_total: float
    invoices: list[InvoiceLine]
    unclassified_count: int
    unclassified_amount: float
    pay_link_url: str | None
    pay_link_unavailable: bool  # True when owed too much to auto-link (see razorpay_client.is_oversized_stub)


@dataclass
class ReportLookupResult:
    status: str  # matched | ambiguous | not_found
    data: CustomerReportData | None = None
    candidates: list[str] = field(default_factory=list)


def get_customer_report_data(session: Session, customer_name_query: str, today: dt.date | None = None) -> ReportLookupResult:
    today = today or dt.date.today()
    all_customers = session.query(Customer).all()

    match = resolve(customer_name_query, [c.name for c in all_customers])
    if match.status != "matched":
        return ReportLookupResult(status=match.status, candidates=match.candidates or [])

    customer = next(c for c in all_customers if c.name == match.customer_name)
    invoices = [inv for inv in customer.invoices if inv.outstanding > 0]

    overdue_amount = sum(inv.outstanding for inv in invoices if is_overdue(inv.due_date, today))
    due_this_week_amount = sum(inv.outstanding for inv in invoices if is_due_this_week(inv.due_date, today))
    total_outstanding = sum(inv.outstanding for inv in invoices)
    monday, friday = week_window(today)

    ageing_breakdown = {
        label: sum(inv.outstanding for inv in invoices if bucket_for(inv.due_date, today) == label)
        for label in AGEING_LABELS
    }

    monday_baseline_overdue = sum(inv.outstanding for inv in invoices if inv.due_date and inv.due_date < monday)

    day_amounts = {}
    for i, name in enumerate(WEEKDAY_NAMES):
        day = monday + dt.timedelta(days=i)
        day_amounts[name] = sum(inv.outstanding for inv in invoices if inv.due_date == day)

    friday_total = monday_baseline_overdue + sum(day_amounts.values())

    unclassified = [inv for inv in invoices if inv.due_date is None]

    sorted_invoices = sorted(invoices, key=lambda inv: (inv.due_date is None, inv.due_date or dt.date.max))
    invoice_lines = [InvoiceLine(inv.invoice_no, inv.due_date, inv.outstanding) for inv in sorted_invoices]

    # So anyone querying their account status also gets a way to pay right
    # there — reuses whatever link the escalation engine already has for
    # this customer, or mints one if this is the first time either surface
    # has needed it (see app/billing.py). A stub from an oversized amount
    # must never be shown as if it were a working link.
    link = get_or_create_consolidated_link(session, customer, total_outstanding)
    link_unavailable = link is not None and is_oversized_stub(link["id"])

    data = CustomerReportData(
        customer_name=customer.name,
        spoc=customer.spoc or "the accounts team",
        overdue_amount=overdue_amount,
        due_this_week_amount=due_this_week_amount,
        due_this_week_start=monday,
        due_this_week_end=friday,
        total_outstanding=total_outstanding,
        ageing_breakdown=ageing_breakdown,
        monday_baseline_overdue=monday_baseline_overdue,
        day_amounts=day_amounts,
        friday_total=friday_total,
        invoices=invoice_lines,
        unclassified_count=len(unclassified),
        unclassified_amount=sum(inv.outstanding for inv in unclassified),
        pay_link_url=link["short_url"] if (link and not link_unavailable) else None,
        pay_link_unavailable=link_unavailable,
    )
    return ReportLookupResult(status="matched", data=data)

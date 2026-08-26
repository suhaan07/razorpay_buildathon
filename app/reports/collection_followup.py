"""Task 2 — Weekly Collection Follow-up. Internal, direct/action tone. Pure
string formatting over an already-computed CustomerReportData."""

from __future__ import annotations

from app.reports.base import CustomerReportData, WEEKDAY_NAMES
from app.reports.format_utils import assemble_messages, format_date, format_inr


def format_collection_followup(data: CustomerReportData) -> list[str]:
    lines = [
        f"Weekly Collection Follow-up — {data.customer_name}",
        "",
        f"Overdue: {format_inr(data.overdue_amount)}",
        f"Due This Week: {format_inr(data.due_this_week_amount)}",
        f"Total Collection Target: {format_inr(data.total_outstanding)}",
        "",
        f"Overdue as of Monday: {format_inr(data.monday_baseline_overdue)}",
    ]
    for name in WEEKDAY_NAMES:
        lines.append(f"{name}: {format_inr(data.day_amounts[name])}")
    lines.append(f"Total Dues By Friday: {format_inr(data.friday_total)}")

    lines += ["", f"Customer: {data.customer_name}", f"SPOC: {data.spoc}"]

    if data.unclassified_count:
        lines += ["", f"Note: {data.unclassified_count} invoice(s) totaling {format_inr(data.unclassified_amount)} have no due date on file."]

    if data.pay_link_url:
        lines += ["", f"Pay now: {data.pay_link_url}"]
    elif data.pay_link_unavailable:
        lines += ["", "Note: this amount currently exceeds what we can auto-generate a payment link for — please contact us directly to arrange payment."]

    headline = "\n".join(lines)

    invoice_lines = ["Invoices:"] + [
        f"  {inv.invoice_no} — {format_inr(inv.outstanding)} — Due {format_date(inv.due_date)}" for inv in data.invoices
    ]
    invoice_list = "\n".join(invoice_lines)

    return assemble_messages(headline, invoice_list, len(data.invoices))

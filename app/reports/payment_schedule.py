"""Task 1 — Weekly Payment Schedule. Customer-facing, polite tone. Pure
string formatting over an already-computed CustomerReportData — no DB
access, no date math."""

from __future__ import annotations

from app.reports.base import AGEING_LABELS, CustomerReportData
from app.reports.format_utils import assemble_messages, format_date, format_inr


def format_payment_schedule(data: CustomerReportData) -> list[str]:
    lines = [
        f"Weekly Payment Schedule — {data.customer_name}",
        "",
        f"Overdue Amount: {format_inr(data.overdue_amount)}",
        f"Due This Week ({format_date(data.due_this_week_start)} to {format_date(data.due_this_week_end)}): {format_inr(data.due_this_week_amount)}",
        f"Total Outstanding: {format_inr(data.total_outstanding)}",
    ]

    breakdown_lines = [
        f"  {label} days: {format_inr(data.ageing_breakdown[label])}"
        for label in AGEING_LABELS
        if data.ageing_breakdown[label] > 0
    ]
    if breakdown_lines:
        lines += ["", "Ageing Breakdown:"] + breakdown_lines

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

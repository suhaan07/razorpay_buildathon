"""Rolls every currently-outstanding invoice up into a "when will we
actually see this cash" forecast — no ML, no new data collection: reuses
the reliability score's avg_days_late (already computed from paid-invoice
history, see app/scoring/reliability.py) and any active promise-to-pay.

predicted_date for one invoice =
  - the customer's pending promise date, if one exists and hasn't itself
    already passed (a stale, not-yet-resolved promise is not trusted —
    falls back to the heuristic below instead), else
  - invoice.due_date + customer.avg_days_late (rounded to the nearest day)

Invoices with no due_date on file can't get a predicted_date at all and
are tallied separately (no_due_date_amount/count) rather than silently
dropped — the sum of every bucket plus that tally always equals the total
outstanding amount, by construction (see test_cash_flow_forecast.py's
reconciliation test).

A customer with zero paid-invoice history ever gets avg_days_late=0.0 by
default (see reliability.py) — meaning "predicted right on the due date",
which is a false-confident guess for someone with no track record at all.
Those invoices are flagged low_confidence rather than hidden, so the UI
can show the number without pretending it's as reliable as one backed by
real history."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.data.models import Invoice, PromiseToPay, ReliabilityScore

_BUCKET_ORDER = ["overdue", "this_week", "next_week", "week_3_4", "beyond_30"]
_BUCKET_LABELS = {
    "overdue": "Overdue / no clear signal",
    "this_week": "This week",
    "next_week": "Next week",
    "week_3_4": "2-4 weeks out",
    "beyond_30": "Beyond 30 days",
}


@dataclass
class ForecastBucket:
    key: str
    label: str
    total_amount: float
    count: int
    low_confidence_amount: float


@dataclass
class CashFlowForecast:
    buckets: list[ForecastBucket] = field(default_factory=list)
    no_due_date_amount: float = 0.0
    no_due_date_count: int = 0
    total_outstanding: float = 0.0


def build_cash_flow_forecast(session: Session, today: dt.date | None = None) -> CashFlowForecast:
    today = today or dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    this_week_end = monday + dt.timedelta(days=6)
    next_week_end = this_week_end + dt.timedelta(days=7)
    week_3_4_end = next_week_end + dt.timedelta(days=14)

    totals = {k: 0.0 for k in _BUCKET_ORDER}
    counts = {k: 0 for k in _BUCKET_ORDER}
    low_conf = {k: 0.0 for k in _BUCKET_ORDER}
    no_due_date_amount = 0.0
    no_due_date_count = 0

    invoices = session.query(Invoice).filter(Invoice.outstanding > 0).all()

    # One lookup per customer, not per invoice — a customer with several
    # outstanding invoices shares the same avg_days_late/promise/history.
    customer_cache: dict[int, dict] = {}

    for invoice in invoices:
        customer = invoice.customer
        info = customer_cache.get(customer.id)
        if info is None:
            score = session.get(ReliabilityScore, customer.id)
            avg_days_late = score.avg_days_late if score else 0.0
            has_payment_history = any(inv.paid_at is not None for inv in customer.invoices)
            pending_promise = (
                session.query(PromiseToPay)
                .filter(PromiseToPay.customer_id == customer.id, PromiseToPay.status == "pending")
                .order_by(PromiseToPay.created_at.desc())
                .first()
            )
            promise_date = (
                pending_promise.promised_date
                if pending_promise and pending_promise.promised_date >= today
                else None
            )
            info = {"avg_days_late": avg_days_late, "low_confidence": not has_payment_history, "promise_date": promise_date}
            customer_cache[customer.id] = info

        if invoice.due_date is None:
            no_due_date_amount += invoice.outstanding
            no_due_date_count += 1
            continue

        predicted_date = info["promise_date"] or (invoice.due_date + dt.timedelta(days=round(info["avg_days_late"])))

        if predicted_date < today:
            key = "overdue"
        elif predicted_date <= this_week_end:
            key = "this_week"
        elif predicted_date <= next_week_end:
            key = "next_week"
        elif predicted_date <= week_3_4_end:
            key = "week_3_4"
        else:
            key = "beyond_30"

        totals[key] += invoice.outstanding
        counts[key] += 1
        if info["low_confidence"]:
            low_conf[key] += invoice.outstanding

    buckets = [
        ForecastBucket(
            key=k, label=_BUCKET_LABELS[k],
            total_amount=round(totals[k], 2), count=counts[k], low_confidence_amount=round(low_conf[k], 2),
        )
        for k in _BUCKET_ORDER
    ]
    return CashFlowForecast(
        buckets=buckets,
        no_due_date_amount=round(no_due_date_amount, 2),
        no_due_date_count=no_due_date_count,
        total_outstanding=round(sum(inv.outstanding for inv in invoices), 2),
    )

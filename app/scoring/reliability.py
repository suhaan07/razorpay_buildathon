"""Transparent, formula-based reliability score (0-100) — deliberately not
an ML model for the MVP. Blends payment-lateness history and on-time rate
so the AI decision layer has a signal richer than the static aging bucket
alone."""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.data.models import Customer, ReliabilityScore

_BANDS = [(80, "Excellent"), (60, "Good"), (40, "Fair"), (0, "Poor")]


def _band_for(score: float) -> str:
    for threshold, label in _BANDS:
        if score >= threshold:
            return label
    return "Poor"


def compute_score(session: Session, customer: Customer) -> ReliabilityScore:
    paid_invoices = [
        inv
        for inv in customer.invoices
        if inv.paid_at is not None and inv.due_date is not None
    ]

    if paid_invoices:
        lateness_days = [max(0, (inv.paid_at - inv.due_date).days) for inv in paid_invoices]
        avg_days_late = sum(lateness_days) / len(lateness_days)
        on_time_rate = sum(1 for d in lateness_days if d == 0) / len(lateness_days)
    else:
        avg_days_late = 0.0
        on_time_rate = 1.0  # no history yet — neutral, not penalized

    lateness_score = max(0.0, 100.0 - avg_days_late * 2.0)
    score = round(0.6 * lateness_score + 0.4 * (on_time_rate * 100), 1)
    score = min(100.0, max(0.0, score))

    record = session.get(ReliabilityScore, customer.id)
    if record is None:
        record = ReliabilityScore(customer_id=customer.id)
        session.add(record)

    record.score = score
    record.band = _band_for(score)
    record.avg_days_late = round(avg_days_late, 1)
    record.on_time_rate = round(on_time_rate, 3)
    record.updated_at = dt.datetime.utcnow()
    return record


def recompute_for_customer_id(session: Session, customer_id: int) -> ReliabilityScore:
    customer = session.get(Customer, customer_id)
    record = compute_score(session, customer)
    session.commit()
    return record

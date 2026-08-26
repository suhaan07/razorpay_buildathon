from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.data.models import Case
from app.decisions.decision_layer import LEVEL_NAMES


@dataclass
class ExceptionCase:
    case_id: int
    customer_name: str
    invoice_no: str
    outstanding: float
    reached_level: str


@dataclass
class BatchReport:
    total_cases: int
    closed_paid: int
    open_cases: int
    paused_cases: int
    exhausted_cases: int
    recovery_rate: float
    avg_days_to_recovery: float | None
    escalated_beyond_spoc: int  # needed a manager or skip-level nudge, not just the first email
    reached_voice: int  # needed the last-resort AI call
    recovered_amount: float
    outstanding_amount: float
    exceptions: list[ExceptionCase] = field(default_factory=list)


def build_report(session: Session) -> BatchReport:
    cases: list[Case] = session.query(Case).all()
    total = len(cases)

    closed_paid = [c for c in cases if c.status == "closed" and c.close_reason == "paid"]
    exhausted = [c for c in cases if c.status == "exhausted"]
    paused = [c for c in cases if c.status == "paused"]
    open_cases = [c for c in cases if c.status == "open"]

    recovery_rate = round(len(closed_paid) / total, 3) if total else 0.0

    recovery_days = [
        (c.closed_at.date() - c.created_at.date()).days for c in closed_paid if c.closed_at and c.created_at
    ]
    avg_days_to_recovery = round(sum(recovery_days) / len(recovery_days), 1) if recovery_days else None

    escalated_beyond_spoc = sum(1 for c in cases if c.playbook_name and c.level_index >= 1)
    reached_voice = sum(1 for c in cases if c.playbook_name and c.level_index >= 3)

    recovered_amount = sum(c.invoice.inv_amount for c in closed_paid)
    outstanding_amount = sum(c.invoice.outstanding for c in cases if c.status != "closed")

    exceptions = [
        ExceptionCase(
            case_id=c.id,
            customer_name=c.customer.name,
            invoice_no=c.invoice.invoice_no,
            outstanding=c.invoice.outstanding,
            reached_level=LEVEL_NAMES[c.level_index] if c.playbook_name else "-",
        )
        for c in exhausted
    ]

    return BatchReport(
        total_cases=total,
        closed_paid=len(closed_paid),
        open_cases=len(open_cases),
        paused_cases=len(paused),
        exhausted_cases=len(exhausted),
        recovery_rate=recovery_rate,
        avg_days_to_recovery=avg_days_to_recovery,
        escalated_beyond_spoc=escalated_beyond_spoc,
        reached_voice=reached_voice,
        recovered_amount=recovered_amount,
        outstanding_amount=outstanding_amount,
        exceptions=exceptions,
    )

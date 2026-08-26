"""Shared payment-link helper used by both the escalation engine
(app/cases/engine.py) and the WhatsApp Q&A bot (app/reports/base.py), so a
customer gets ONE reused "pay everything outstanding" Razorpay link across
both surfaces instead of a fresh link minted on every touch — including
every WhatsApp query, which unlike escalation dispatch isn't rate-limited
by a wait timer and could otherwise be called repeatedly."""

from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from app.data.models import Customer
from app.integrations.razorpay_client import create_payment_link

_AMOUNT_TOLERANCE = 0.01  # paise-level float noise, not a real amount change


def get_or_create_consolidated_link(session: Session, customer: Customer, total_outstanding: float) -> dict | None:
    """Returns {"id", "short_url", "stub"} or None if nothing is owed.
    Reuses the cached link on Customer when it still matches the current
    total; regenerates (and persists the new one) when the total has moved
    — e.g. a partial payment landed since the link was last created."""

    if total_outstanding <= 0:
        return None

    cached_matches = (
        customer.consolidated_pay_link_id is not None
        and customer.consolidated_pay_link_amount is not None
        and abs(customer.consolidated_pay_link_amount - total_outstanding) < _AMOUNT_TOLERANCE
    )
    if cached_matches:
        return {
            "id": customer.consolidated_pay_link_id,
            "short_url": customer.consolidated_pay_link_url,
            "stub": customer.consolidated_pay_link_id.startswith("stub"),
        }

    link = create_payment_link(
        amount_rupees=total_outstanding,
        invoice_no=f"ALL-{customer.id}",
        customer_name=customer.name,
        description=f"All outstanding invoices for {customer.name}",
        reference_id=f"ALL-{customer.id}-{secrets.token_hex(3)}",
        notes={"kind": "consolidated_payoff", "customer_id": str(customer.id)},
    )
    customer.consolidated_pay_link_id = link["id"]
    customer.consolidated_pay_link_url = link["short_url"]
    customer.consolidated_pay_link_amount = total_outstanding
    session.commit()
    return link

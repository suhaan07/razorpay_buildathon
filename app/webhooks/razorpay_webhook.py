from __future__ import annotations

import datetime as dt
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.cases.engine import TERMINAL_STATUSES, close_case, record_event
from app.data.models import Case
from app.db import get_session
from app.integrations.razorpay_client import verify_webhook_signature
from app.notifications import notify_payment_received
from app.scoring.reliability import recompute_for_customer_id

logger = logging.getLogger("recovery.webhooks.razorpay")
router = APIRouter()


def _close_one(session: Session, case: Case, event_type: str, payment_id: str | None, *, consolidated: bool) -> None:
    invoice = case.invoice
    invoice.received = invoice.inv_amount
    invoice.outstanding = 0.0
    invoice.paid_at = dt.date.today()
    close_case(session, case, reason="paid")
    record_event(session, case, type="webhook", payload={"event": event_type, "payment_id": payment_id, "consolidated": consolidated})


def _handle_consolidated_payment(session: Session, notes: dict, event_type: str, payment_id: str | None) -> dict:
    """A "pay everything" link (see cases/engine.py::_create_consolidated_link)
    isn't tied to any single Case, so on payment it closes every currently
    open case for that customer instead of matching one pay_link_id.
    Naturally idempotent: a redelivered event just finds no open cases left."""

    customer_id = notes.get("customer_id")
    if not customer_id:
        return {"status": "ignored", "reason": "consolidated payment missing customer_id in notes"}

    if event_type == "payment.failed":
        return {"status": "logged", "reason": "consolidated payment failed", "customer_id": customer_id}

    if event_type not in ("payment_link.paid", "payment.captured"):
        return {"status": "ignored", "reason": f"unhandled event {event_type} for consolidated payment"}

    open_cases = (
        session.query(Case)
        .filter(Case.customer_id == int(customer_id))
        .filter(Case.status.notin_(TERMINAL_STATUSES))
        .all()
    )
    for case in open_cases:
        _close_one(session, case, event_type, payment_id, consolidated=True)
    session.commit()

    if open_cases:
        recompute_for_customer_id(session, int(customer_id))
        notify_payment_received(
            customer_name=open_cases[0].customer.name,
            total_amount=sum(c.invoice.inv_amount for c in open_cases),
            invoice_numbers=[c.invoice.invoice_no for c in open_cases],
            to_number=open_cases[0].customer.last_whatsapp_query_from,
        )
    return {"status": "closed", "case_ids": [c.id for c in open_cases], "consolidated": True}


@router.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(default=None),
    session: Session = Depends(get_session),
):
    raw_body = await request.body()

    if not verify_webhook_signature(raw_body, x_razorpay_signature or ""):
        raise HTTPException(status_code=400, detail="invalid webhook signature")

    data = json.loads(raw_body)
    event_type = data.get("event", "")
    payload = data.get("payload", {})
    payment_link_entity = payload.get("payment_link", {}).get("entity", {})
    payment_entity = payload.get("payment", {}).get("entity", {})
    link_id = payment_link_entity.get("id")
    payment_id = payment_entity.get("id")
    notes = payment_link_entity.get("notes") or {}

    if notes.get("kind") == "consolidated_payoff":
        return _handle_consolidated_payment(session, notes, event_type, payment_id)

    case = session.query(Case).filter(Case.pay_link_id == link_id).first() if link_id else None

    if case is None:
        logger.warning("razorpay webhook for unknown payment_link_id=%s event=%s", link_id, event_type)
        return {"status": "ignored", "reason": "no matching case"}

    if event_type in ("payment_link.paid", "payment.captured"):
        if case.status in TERMINAL_STATUSES:
            return {"status": "ignored", "reason": "case already terminal (idempotent)"}

        _close_one(session, case, event_type, payment_id, consolidated=False)
        session.commit()
        recompute_for_customer_id(session, case.customer_id)
        notify_payment_received(
            customer_name=case.customer.name,
            total_amount=case.invoice.inv_amount,
            invoice_numbers=[case.invoice.invoice_no],
            to_number=case.customer.last_whatsapp_query_from,
        )
        return {"status": "closed", "case_id": case.id}

    if event_type == "payment.failed":
        record_event(session, case, type="webhook", payload={"event": event_type, "payment_id": payment_id})
        session.commit()
        return {"status": "logged", "case_id": case.id}

    return {"status": "ignored", "reason": f"unhandled event {event_type}"}

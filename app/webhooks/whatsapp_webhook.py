"""Inbound-only WhatsApp Q&A bot. Anyone on the team can text the Sandbox
number and ask about any customer's account — the bot never sends anything
unprompted (see cases/engine.py for the actual outbound escalation, which
runs over email + a final voice call, never WhatsApp)."""

from __future__ import annotations

import logging
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Form, Response
from sqlalchemy.orm import Session

from app.data.models import Customer
from app.db import get_session
from app.reports.base import get_customer_report_data
from app.reports.collection_followup import format_collection_followup
from app.reports.payment_schedule import format_payment_schedule
from app.router.intent import parse_message

logger = logging.getLogger("recovery.webhooks.whatsapp")
router = APIRouter()

_FORMATTERS = {
    "payment_schedule": format_payment_schedule,
    "collection_followup": format_collection_followup,
}

_USAGE_HINT = (
    "I can help with two things:\n"
    '"Give me a weekly payment schedule for <customer>"\n'
    '"Give me a weekly collection follow-up for <customer>"'
)


def _twiml(messages: list[str]) -> Response:
    # Twilio drops a reply it can't parse as XML with no visible error on
    # your phone — a customer name with an "&" (e.g. "Smith & Sons") would
    # silently break this without escaping.
    body = "<?xml version='1.0' encoding='UTF-8'?><Response>" + "".join(f"<Message>{escape(m)}</Message>" for m in messages) + "</Response>"
    return Response(content=body, media_type="application/xml")


@router.post("/whatsapp/webhook")
async def whatsapp_webhook(Body: str = Form(...), From: str = Form(...), session: Session = Depends(get_session)):
    # Every path below must return valid TwiML, even on a bug — a 500 with
    # no body means Twilio has nothing to relay and you see nothing on
    # WhatsApp at all, which is worse than an honest error message.
    try:
        return _handle(Body, From, session)
    except Exception:  # noqa: BLE001
        logger.exception("whatsapp_webhook: unhandled error for Body=%r From=%r", Body, From)
        return _twiml(["Something went wrong processing that on our side. The team's been notified — please try again in a moment."])


def _handle(body_text: str, from_number: str, session: Session) -> Response:
    parsed = parse_message(body_text)

    if not parsed.report_type or not parsed.customer_name:
        return _twiml([_USAGE_HINT])

    result = get_customer_report_data(session, parsed.customer_name)

    if result.status == "not_found":
        return _twiml([f"I couldn't find a customer matching '{parsed.customer_name}'. Please check the spelling or share the exact account name."])

    if result.status == "ambiguous":
        candidates = "\n".join(f"- {name}" for name in result.candidates)
        return _twiml([f"That name matches more than one account:\n{candidates}\nPlease resend with the exact name."])

    # Remember who's asking about this account so a payment-received alert
    # (app/notifications.py) can reply to the same number later, rather than
    # a fixed operator number that has nothing to do with this customer.
    customer = session.query(Customer).filter(Customer.name == result.data.customer_name).first()
    if customer is not None:
        customer.last_whatsapp_query_from = from_number
        session.commit()

    formatter = _FORMATTERS[parsed.report_type]
    return _twiml(formatter(result.data))

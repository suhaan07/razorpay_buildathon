"""Inbound-only WhatsApp Q&A bot. Anyone on the team can text the Sandbox
number and ask about any customer's account — the bot never sends anything
unprompted (see cases/engine.py for the actual outbound escalation, which
runs over email + a final voice call, never WhatsApp)."""

from __future__ import annotations

import datetime as dt
import logging
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Form, Response
from sqlalchemy.orm import Session

from app.cases.engine import flag_dispute, record_promise
from app.data.models import Case, Customer
from app.db import get_session
from app.matching.resolver import resolve_customer
from app.reports.base import get_customer_report_data
from app.reports.collection_followup import format_collection_followup
from app.reports.format_utils import format_date
from app.reports.payment_schedule import format_payment_schedule
from app.router.date_phrase import parse_date_phrase
from app.router.intent import is_dispute_message, is_promise_message, parse_dispute_message, parse_message, parse_promise_message

logger = logging.getLogger("recovery.webhooks.whatsapp")
router = APIRouter()

_FORMATTERS = {
    "payment_schedule": format_payment_schedule,
    "collection_followup": format_collection_followup,
}

_USAGE_HINT = (
    "I can help with a few things:\n"
    '"Give me a weekly payment schedule for <customer>"\n'
    '"Give me a weekly collection follow-up for <customer>"\n'
    '"Promise to pay for <customer> by <date>" (e.g. "by 30-Aug", "by Friday", "by next week")\n'
    '"Dispute for <customer>: <reason>" (reason optional) — pauses escalation for human review'
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


def _resolve_customer_or_none(session: Session, name_query: str) -> tuple[Customer | None, Response | None]:
    """Shared name-matching used by both the report and promise paths.
    Returns (customer, None) on a clean match, or (None, <error TwiML>) for
    not-found/ambiguous — the caller just returns the second value if set."""
    lookup = resolve_customer(session, name_query)
    if lookup.status == "not_found":
        return None, _twiml([f"I couldn't find a customer matching '{name_query}'. Please check the spelling or share the exact account name."])
    if lookup.status == "ambiguous":
        candidates = "\n".join(f"- {name}" for name in lookup.candidates)
        return None, _twiml([f"That name matches more than one account:\n{candidates}\nPlease resend with the exact name."])
    return lookup.customer, None


def _handle_promise(body_text: str, session: Session, from_number: str, today: dt.date | None = None) -> Response:
    today = today or dt.date.today()
    parsed = parse_promise_message(body_text)

    if not parsed.customer_name or not parsed.date_phrase:
        return _twiml([
            "I couldn't quite parse that. Please use the format:\n"
            '"Promise to pay for <customer> by <date>"\n'
            'e.g. "Promise to pay for Acme Pvt Ltd by 30-Aug"'
        ])

    customer, error_response = _resolve_customer_or_none(session, parsed.customer_name)
    if error_response is not None:
        return error_response

    promised_date = parse_date_phrase(parsed.date_phrase, today)
    if promised_date is None:
        return _twiml([
            f"I couldn't understand the date '{parsed.date_phrase}'. Try something like '30-Aug', "
            "'Friday', 'tomorrow', 'in 3 days', or 'next week'."
        ])

    total_outstanding = sum(inv.outstanding for inv in customer.invoices)
    if total_outstanding <= 0:
        return _twiml([f"{customer.name} has nothing outstanding right now — no promise needed!"])

    customer.last_whatsapp_query_from = from_number
    record_promise(session, customer, promised_date, source="whatsapp", raw_text=body_text)
    return _twiml([
        f"Got it — noted that {customer.name} will clear their account by {format_date(promised_date)}. "
        "We'll check back then."
    ])


def _handle_dispute(body_text: str, session: Session, from_number: str) -> Response:
    parsed = parse_dispute_message(body_text)

    if not parsed.customer_name:
        return _twiml([
            "I couldn't quite parse that. Please use the format:\n"
            '"Dispute for <customer>: <reason>"\n'
            'e.g. "Dispute for Acme Pvt Ltd: already paid this via bank transfer"'
        ])

    customer, error_response = _resolve_customer_or_none(session, parsed.customer_name)
    if error_response is not None:
        return error_response

    open_case_count = session.query(Case).filter(Case.customer_id == customer.id, Case.status == "open").count()
    if open_case_count == 0:
        return _twiml([f"{customer.name} has no active case to dispute right now — nothing is currently being escalated for them."])

    customer.last_whatsapp_query_from = from_number
    flag_dispute(session, customer, parsed.reason)
    reason_note = f" Reason noted: {parsed.reason}." if parsed.reason else ""
    return _twiml([
        f"Got it — paused escalation on {open_case_count} case{'s' if open_case_count != 1 else ''} for {customer.name} "
        f"pending review.{reason_note} It won't resume automatically until someone reopens it."
    ])


def _handle(body_text: str, from_number: str, session: Session) -> Response:
    if is_promise_message(body_text):
        return _handle_promise(body_text, session, from_number)

    if is_dispute_message(body_text):
        return _handle_dispute(body_text, session, from_number)

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

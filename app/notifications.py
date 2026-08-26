"""Best-effort outbound alert: a WhatsApp ping when a payment actually
lands, saying what got paid and which invoices cleared. Replies to whoever
last WhatsApp-queried that customer's report (Customer.last_whatsapp_query_
from, set in app/webhooks/whatsapp_webhook.py) — falling back to a fixed
PAYMENT_NOTIFY_WHATSAPP_TO only when nobody has queried that account yet.
Deliberately NOT part of app/channels/ — that registry is for escalation
dispatch (spoc/manager/skip_level/voice), which stays inbound-only on
WhatsApp. This is a single-purpose alert sent straight through the Twilio
REST API, and it must never be able to fail the payment-processing flow
it's attached to."""

from __future__ import annotations

import logging
import os

from app.integrations import twilio_client
from app.reports.format_utils import format_inr

logger = logging.getLogger("recovery.notifications")

_SANDBOX_FROM = "whatsapp:+14155238886"


def _as_whatsapp_address(raw: str) -> str:
    raw = raw.strip()
    return raw if raw.startswith("whatsapp:") else f"whatsapp:{raw}"


def notify_payment_received(
    *,
    customer_name: str,
    total_amount: float,
    invoice_numbers: list[str],
    to_number: str | None = None,
) -> None:
    to = to_number or os.getenv("PAYMENT_NOTIFY_WHATSAPP_TO")
    if not to:
        return  # nobody has queried this account on WhatsApp yet, and no fallback configured — silent no-op, not an error

    if not invoice_numbers:
        return  # nothing actually closed (shouldn't happen, but never send an empty confirmation)

    if not twilio_client.is_configured():
        logger.info("PAYMENT_NOTIFY_WHATSAPP_TO is set but Twilio credentials are missing — skipping notification")
        return

    invoices_text = ", ".join(invoice_numbers)
    plural = "invoice" if len(invoice_numbers) == 1 else "invoices"
    body = (
        f"Payment received: {format_inr(total_amount)} from {customer_name} — "
        f"clearing {len(invoice_numbers)} {plural}: {invoices_text}."
    )

    from_number = os.getenv("TWILIO_WHATSAPP_FROM") or _SANDBOX_FROM

    try:
        client = twilio_client.get_client()
        client.messages.create(from_=_as_whatsapp_address(from_number), to=_as_whatsapp_address(to), body=body)
    except Exception:  # noqa: BLE001 — a notification failure must never block a payment from closing out
        logger.exception("payment-received WhatsApp notification failed (the payment itself was still recorded correctly)")

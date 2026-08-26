from __future__ import annotations

import logging
import os
import secrets

logger = logging.getLogger("recovery.integrations.razorpay")

_client = None


def is_configured() -> bool:
    return bool(os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET"))


def is_oversized_stub(link_id: str) -> bool:
    """True only for the "amount exceeded Razorpay's max" fallback — not
    the generic not-configured stub. Callers use this to distinguish "no
    real link because we're in dev mode" (fine to show as a placeholder)
    from "no real link because Razorpay rejected this specific amount"
    (must NOT be shown as a working payment link to anyone, since clicking
    it does nothing)."""
    return link_id.startswith("stub_oversized_")


def get_client():
    global _client
    if _client is None:
        import razorpay

        _client = razorpay.Client(auth=(os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET")))
    return _client


def create_payment_link(
    *,
    amount_rupees: float,
    invoice_no: str,
    customer_name: str,
    description: str,
    reference_id: str | None = None,
    notes: dict | None = None,
) -> dict:
    """Returns {"id": ..., "short_url": ..., "stub": bool}. Falls back to a
    local stub link when Razorpay isn't configured (NFR-7) — the case
    engine and channels never need to know which.

    `reference_id` defaults to `invoice_no` for a normal single-invoice
    link. Callers should reuse a case's existing pay_link_id/pay_link_url
    across repeated dispatches rather than calling this again for the same
    case — Razorpay's reference_id must be unique per account, so a second
    create() with the same one always fails. This function still degrades
    gracefully if that happens anyway (e.g. a stray link from earlier
    testing already occupies it) by retrying once with a disambiguated
    reference_id, so a batch run never gets stuck on it.

    `notes` is opaque metadata Razorpay echoes back on the payment and on
    the webhook — used to mark a consolidated "pay everything" link so the
    webhook handler knows to close every open case for that customer
    instead of just one (see webhooks/razorpay_webhook.py).

    Every Razorpay-side failure mode is handled without ever raising into
    the caller, so a batch run never gets permanently stuck on — or badly
    delayed by — one case: a duplicate reference_id (retried once with a
    disambiguated one, since that's cheap and usually resolves instantly);
    an amount over the account's current per-link maximum; a test-mode
    account's hard *total* link quota ("test mode limit of 30 reached for
    payment_link" — a fixed lifetime cap); and a generic "Too many
    requests" (observed, in practice, to be the SAME exhausted-quota
    condition surfacing under a different message, not a short-lived
    throttle that clears if you wait). All three of the latter fall back to
    a stub link immediately — no retry, no backoff — so the message still
    sends. Retrying any of them was tried and measured: it turned single
    batch runs into multi-minute stalls for a guaranteed second failure,
    which is worse for every other case waiting behind it than just
    degrading gracefully once and moving on."""

    ref = reference_id or invoice_no

    if not is_configured():
        stub_id = f"stub_{ref}"
        return {"id": stub_id, "short_url": f"https://rzp.io/l/{stub_id}", "stub": True}

    client = get_client()
    payload = {
        "amount": int(round(amount_rupees * 100)),
        "currency": "INR",
        "description": description,
        "customer": {"name": customer_name},
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "reference_id": ref,
    }
    if notes:
        payload["notes"] = notes

    while True:
        try:
            link = client.payment_link.create(payload)
            break
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            lower = message.lower()
            if "reference_id" in message and "already exists" in message:
                logger.warning("payment link reference_id %r already taken (likely from earlier testing) — retrying with a disambiguated one", ref)
                payload["reference_id"] = f"{ref}-{secrets.token_hex(3)}"
                continue
            if "exceeds maximum amount" in lower or "amount exceeds" in lower:
                logger.warning(
                    "payment link for %r rejected: amount ₹%.2f exceeds this Razorpay account's current per-link maximum "
                    "(tied to activation/KYC status) — falling back to a stub link so the message still sends",
                    ref, amount_rupees,
                )
            elif "test mode limit" in lower or "too many requests" in lower:
                logger.warning(
                    "payment link for %r rejected (%s) — this test account is out of headroom for new "
                    "payment links right now; falling back to a stub link so the message still sends",
                    ref, message,
                )
            else:
                raise
            stub_id = f"stub_oversized_{ref}"
            return {"id": stub_id, "short_url": f"https://rzp.io/l/{stub_id}", "stub": True}
    return {"id": link["id"], "short_url": link["short_url"], "stub": False}


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        logger.warning("RAZORPAY_WEBHOOK_SECRET not set — skipping signature verification (dev mode only)")
        return True

    import razorpay

    try:
        razorpay.Utility().verify_webhook_signature(body.decode("utf-8"), signature, secret)
        return True
    except razorpay.errors.SignatureVerificationError:
        return False

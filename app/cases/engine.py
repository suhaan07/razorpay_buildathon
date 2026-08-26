"""The case state machine. Owns every transition; decisions/, playbooks/,
and channels/ only ever receive plain context and return a result — they
never touch the DB (see DESIGN.md §1 design principle).

The escalation shape is fixed and small: every case walks the same single
playbook (spoc -> manager -> skip_level, all internal emails informing our
own team that a customer hasn't paid) and only reaches the final voice call
— to the customer directly — once the internal chain is exhausted. The AI
decision layer doesn't pick between playbooks; it picks how far up that
chain to start/jump (never past skip_level) and how many days to wait
before the next escalation, given how overdue the invoice is, how reliable
the customer has been, how large the invoice is, and how many unanswered
touches there have already been."""

from __future__ import annotations

import datetime as dt
import logging
import os

from sqlalchemy.orm import Session

from app.billing import get_or_create_consolidated_link
from app.cases import compliance
from app.channels.email_template import build_escalation_html
from app.channels.registry import get_channel
from app.data.ageing import bucket_for, days_overdue
from app.data.models import Case, CaseEvent, Customer, Invoice, ReliabilityScore, Settings
from app.decisions.decision_layer import LEVEL_NAMES, decide
from app.integrations.razorpay_client import create_payment_link, is_oversized_stub
from app.playbooks.loader import get_playbook
from app.playbooks.renderer import render
from app.reports.format_utils import format_date, format_inr
from app.scoring.reliability import compute_score

TERMINAL_STATUSES = {"closed", "exhausted"}
PLAYBOOK_NAME = "receivables_escalation"
VOICE_LEVEL_INDEX = len(LEVEL_NAMES) - 1  # 3 — reachable only by mechanical progression, never suggested directly

logger = logging.getLogger("recovery.cases.engine")


def get_settings(session: Session) -> Settings:
    settings = session.get(Settings, 1)
    if settings is None:
        settings = Settings(id=1)
        session.add(settings)
        session.commit()
    return settings


def record_event(
    session: Session,
    case: Case,
    *,
    type: str,
    channel: str | None = None,
    payload: dict | None = None,
    rationale: str | None = None,
) -> CaseEvent:
    event = CaseEvent(case_id=case.id, type=type, channel=channel, payload=payload, rationale=rationale)
    session.add(event)
    return event


def sync_cases(session: Session, today: dt.date | None = None) -> int:
    """Create/update a Case for every invoice, and close cases whose invoice
    is already fully paid but wasn't closed via a webhook (e.g. it was paid
    before the case ever went out)."""

    today = today or dt.date.today()
    invoices = session.query(Invoice).all()
    touched = 0

    for invoice in invoices:
        fully_paid = invoice.outstanding <= 0
        case = invoice.case

        if fully_paid:
            if case and case.status not in TERMINAL_STATUSES:
                close_case(session, case, reason="paid")
                touched += 1
            continue

        bucket = bucket_for(invoice.due_date, today)
        if case is None:
            case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket=bucket, status="open")
            session.add(case)
            session.flush()
            record_event(session, case, type="system", payload={"reason": "case_created", "bucket": bucket})
            touched += 1
        elif case.bucket != bucket and case.status not in TERMINAL_STATUSES:
            record_event(session, case, type="system", payload={"reason": "bucket_changed", "from": case.bucket, "to": bucket})
            case.bucket = bucket
            touched += 1

    session.commit()
    return touched


def refresh_all_reliability_scores(session: Session) -> None:
    for customer in session.query(Customer).all():
        compute_score(session, customer)
    session.commit()


def due_cases(session: Session, now: dt.datetime | None = None) -> list[Case]:
    now = now or dt.datetime.utcnow()
    return (
        session.query(Case)
        .filter(Case.status == "open")
        .filter((Case.next_action_at.is_(None)) | (Case.next_action_at <= now))
        .all()
    )


def _build_case_context(session: Session, case: Case, today: dt.date) -> dict:
    invoice: Invoice = case.invoice
    customer: Customer = case.customer
    score: ReliabilityScore | None = session.get(ReliabilityScore, customer.id)
    customer_outstanding = sum(inv.outstanding for inv in customer.invoices)

    return {
        "case_id": case.id,
        "bucket": case.bucket,
        "days_overdue": days_overdue(invoice.due_date, today),
        "reliability_score": score.score if score else 70.0,
        "reliability_band": score.band if score else "Fair",
        "avg_days_late": score.avg_days_late if score else 0.0,
        "outstanding_amount": invoice.outstanding,
        "touch_count": case.touch_count,
        "customer_name": customer.name,
        "invoice_no": invoice.invoice_no,
        "amount": format_inr(invoice.outstanding),
        "customer_outstanding": format_inr(customer_outstanding),
        "spoc_name": customer.spoc or "SPOC",
        "manager_name": customer.manager_name or "Manager",
        "skip_level_name": customer.skip_level_name or "Skip-level",
    }


def _resolve_single_contact(rule: str, customer: Customer) -> str:
    if rule == "customer":
        return customer.phone or "+910000000000"
    if rule == "spoc":
        return customer.spoc_email or f"{(customer.spoc or 'spoc').lower().replace(' ', '.')}@internal.example.com"
    if rule == "manager":
        return customer.manager_email or f"{(customer.manager_name or 'manager').lower().replace(' ', '.')}@internal.example.com"
    if rule == "skip_level":
        return customer.skip_level_email or f"{(customer.skip_level_name or 'skip.level').lower().replace(' ', '.')}@internal.example.com"
    return rule  # a literal address/number given directly in the playbook


def _resolve_contact(rule: str, customer: Customer) -> str:
    if "," in rule:
        return ",".join(_resolve_single_contact(part.strip(), customer) for part in rule.split(","))
    return _resolve_single_contact(rule, customer)


_LEVEL_SUBJECT_LABEL = {"spoc": "Payment reminder", "manager": "Escalation", "skip_level": "Urgent escalation", "voice": "Final notice"}


def _subject_for_level(level_name: str, invoice_no: str, days_overdue: int) -> str:
    label = _LEVEL_SUBJECT_LABEL.get(level_name, "Reminder")
    return f"{label}: Invoice {invoice_no} — {days_overdue} days overdue"


def _customer_invoice_rows(customer: Customer) -> list[dict]:
    open_invoices = sorted(
        (inv for inv in customer.invoices if inv.outstanding > 0),
        key=lambda inv: (inv.due_date is None, inv.due_date or dt.date.max),
    )
    return [
        {
            "invoice_no": inv.invoice_no,
            "due_date_display": format_date(inv.due_date),
            "amount_display": format_inr(inv.outstanding),
            "outstanding": inv.outstanding,
        }
        for inv in open_invoices
    ]


def _dispatch_level(session: Session, case: Case, playbook: dict, level: dict, ctx: dict, wait_days: int, now: dt.datetime) -> None:
    invoice: Invoice = case.invoice
    customer: Customer = case.customer

    # Reused across every level of this case, not recreated per dispatch —
    # Razorpay's reference_id (the invoice number) must be unique per
    # account, so calling create_payment_link() again for the same case
    # would always fail on the second escalation onward.
    if case.pay_link_id and case.pay_link_url:
        link = {"id": case.pay_link_id, "short_url": case.pay_link_url, "stub": case.pay_link_id.startswith("stub_")}
    else:
        link = create_payment_link(
            amount_rupees=invoice.outstanding,
            invoice_no=invoice.invoice_no,
            customer_name=customer.name,
            description=f"Invoice {invoice.invoice_no}",
        )
        case.pay_link_id, case.pay_link_url = link["id"], link["short_url"]
        record_event(session, case, type="system", payload={"pay_link_id": link["id"], "stub": link["stub"]})

    # An "amount exceeds Razorpay's max" stub (see razorpay_client.py) must
    # never be shown as if it were a working link — clicking it does
    # nothing. Substitute an honest note for the SAME {{pay_link}} token
    # instead, so both the plain-text body and the HTML intro (which
    # reuses that body) say the true thing without extra plumbing.
    primary_unavailable = is_oversized_stub(link["id"])
    pay_link_token = link["short_url"] if not primary_unavailable else "(payment link unavailable — this amount currently exceeds what can be auto-linked; please contact us directly to arrange payment)"

    render_ctx = {**ctx, "pay_link": pay_link_token}
    body = render(level["message"], render_ctx)
    to = _resolve_contact(level["recipients"], customer)
    cc = _resolve_contact(level["cc"], customer) if level.get("cc") else None

    html = None
    if level["channel"] == "email":
        invoice_rows = _customer_invoice_rows(customer)
        total_outstanding = sum(row["outstanding"] for row in invoice_rows)
        consolidated = get_or_create_consolidated_link(session, customer, total_outstanding) if len(invoice_rows) > 1 else None
        consolidated_unavailable = consolidated is not None and is_oversized_stub(consolidated["id"])
        if consolidated:
            record_event(session, case, type="system", payload={"consolidated_pay_link_id": consolidated["id"], "stub": consolidated["stub"]})

        level_label = LEVEL_NAMES[case.level_index].replace("_", " ").title()
        html = build_escalation_html(
            heading=f"{level_label} — {customer.name}",
            intro_text=body,
            highlighted_invoice_no=invoice.invoice_no,
            invoice_rows=invoice_rows,
            primary_label=f"Pay this invoice ({format_inr(invoice.outstanding)})" if not primary_unavailable else None,
            primary_url=link["short_url"] if not primary_unavailable else None,
            secondary_label=f"Pay everything ({format_inr(sum(r['outstanding'] for r in invoice_rows))})" if consolidated and not consolidated_unavailable else None,
            secondary_url=consolidated["short_url"] if consolidated and not consolidated_unavailable else None,
        )

    subject = _subject_for_level(LEVEL_NAMES[case.level_index], invoice.invoice_no, ctx["days_overdue"])
    channel = get_channel(level["channel"])
    result = channel.send(to=to, cc=cc, subject=subject, body=body, html=html)

    record_event(
        session,
        case,
        type="dispatch",
        channel=level["channel"],
        payload={
            "to": to, "cc": cc, "status": result.status, "detail": result.detail,
            "level_index": case.level_index, "level_name": LEVEL_NAMES[case.level_index],
        },
    )

    case.last_action_at = now
    case.next_action_at = now + dt.timedelta(days=wait_days)
    case.touch_count += 1


def _next_level_index_for_existing_case(case: Case, decision) -> int:
    """Called only for a case that already has a playbook assigned. Always
    advances at least one rung; the AI's suggestion can push it further if
    urgency has risen, but never past skip_level via a jump — voice
    (index 3) is only ever reached by the "+1" mechanical path."""
    return max(case.level_index + 1, min(decision.suggested_level, VOICE_LEVEL_INDEX - 1))


def _process_one_case(session: Session, case: Case, playbook: dict, now: dt.datetime, today: dt.date) -> str:
    """Runs the decide -> advance/exhaust -> dispatch logic for exactly one
    case. Returns "dispatched" | "exhausted" | "failed". Shared by run_batch()
    (gated on due_cases()) and force_dispatch_case() (bypasses that gate for
    on-demand testing from the portal)."""

    levels = playbook["levels"]
    try:
        ctx = _build_case_context(session, case, today)
        decision = decide(ctx)
        record_event(
            session, case, type="decision", rationale=decision.rationale,
            payload={"suggested_level": LEVEL_NAMES[decision.suggested_level], "urgency_score": decision.urgency_score, "wait_days": decision.wait_days, "source": decision.source},
        )

        if case.playbook_name is not None and case.level_index >= VOICE_LEVEL_INDEX:
            # already made the final voice call and it still went unanswered/unpaid
            on_exhausted = playbook["onExhausted"]
            record_event(session, case, type="system", payload={"reason": "exhausted", "status": on_exhausted["status"]})
            case.status = "exhausted"
            case.close_reason = "exhausted"
            case.closed_at = now
            session.commit()
            return "exhausted"

        is_new = case.playbook_name is None
        case.level_index = decision.suggested_level if is_new else _next_level_index_for_existing_case(case, decision)
        case.playbook_name = PLAYBOOK_NAME

        level = levels[case.level_index]
        _dispatch_level(session, case, playbook, level, ctx, decision.wait_days, now)
        session.commit()
        return "dispatched"
    except Exception as exc:  # noqa: BLE001 - one bad case must not sink the batch
        session.rollback()
        logger.exception("case %s failed, skipping", case.id)
        record_event(session, case, type="system", payload={"reason": "dispatch_error", "error": str(exc)[:500]})
        session.commit()
        return "failed"


def run_batch(session: Session, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.utcnow()
    today = now.date()

    sync_cases(session, today)
    refresh_all_reliability_scores(session)

    if get_settings(session).auto_dispatch_paused:
        # Neither of the above touches Razorpay or sends anything, so they
        # still run — cases/scores stay current. Only the actual dispatch
        # loop below (which can mint a real payment link per case) is
        # gated. force_dispatch_case() (the per-case "Send now (test)"
        # button) deliberately does NOT check this — a human explicitly
        # asking to send one case is not what this switch guards against.
        return {"processed": 0, "dispatched": 0, "paused": 0, "exhausted": 0, "failed": 0, "auto_dispatch_paused": True}

    playbook = get_playbook(PLAYBOOK_NAME)
    processed, dispatched, paused, exhausted, failed = 0, 0, 0, 0, 0

    for case in due_cases(session, now):
        processed += 1

        if compliance.in_quiet_hours(now):
            case.next_action_at = compliance.next_available_time(now)
            record_event(session, case, type="system", payload={"reason": "deferred_quiet_hours"})
            session.commit()
            continue

        if case.touch_count >= compliance.max_touch_cap():
            case.status = "paused"
            record_event(session, case, type="system", payload={"reason": "max_touch_cap_reached"})
            session.commit()
            paused += 1
            continue

        outcome = _process_one_case(session, case, playbook, now, today)
        if outcome == "dispatched":
            dispatched += 1
        elif outcome == "exhausted":
            exhausted += 1
        elif outcome == "failed":
            failed += 1

    return {"processed": processed, "dispatched": dispatched, "paused": paused, "exhausted": exhausted, "failed": failed}


def force_dispatch_case(session: Session, case: Case, now: dt.datetime | None = None) -> str:
    """Portal "Send now" test action: process this one case immediately,
    bypassing next_action_at and quiet hours — an explicit human request is
    not the same as an unattended background job blasting messages at 2am,
    so those two gates don't apply here. The max-touch safety cap still
    does. Returns "dispatched" | "exhausted" | "failed" | "paused"."""

    now = now or dt.datetime.utcnow()
    today = now.date()

    if case.touch_count >= compliance.max_touch_cap():
        case.status = "paused"
        record_event(session, case, type="system", payload={"reason": "max_touch_cap_reached"})
        session.commit()
        return "paused"

    playbook = get_playbook(PLAYBOOK_NAME)
    return _process_one_case(session, case, playbook, now, today)


def preview_next_message(session: Session, case: Case, now: dt.datetime | None = None) -> dict:
    """Read-only dry run: shows exactly what force_dispatch_case() would
    send right now — channel, recipient(s), rendered body — without
    actually sending it, creating a real payment link, or touching case
    state. Safe to call as often as you like."""

    now = now or dt.datetime.utcnow()
    today = now.date()
    playbook = get_playbook(PLAYBOOK_NAME)
    levels = playbook["levels"]

    ctx = _build_case_context(session, case, today)
    decision = decide(ctx)

    if case.playbook_name is not None and case.level_index >= VOICE_LEVEL_INDEX:
        return {
            "would": "exhaust",
            "rationale": decision.rationale,
            "detail": "This case has already had its final voice call with no payment — the next action would mark it exhausted, not send anything further.",
        }

    next_level_index = decision.suggested_level if case.playbook_name is None else _next_level_index_for_existing_case(case, decision)
    level = levels[next_level_index]
    customer = case.customer

    # Reuse the case's real link if it already has one (same rule as
    # _dispatch_level) so the preview matches exactly what would be sent —
    # only a genuinely new case shows the placeholder.
    pay_link_display = case.pay_link_url or "[a real Razorpay payment link is generated when actually sent]"
    render_ctx = {**ctx, "pay_link": pay_link_display}
    body = render(level["message"], render_ctx)
    to = _resolve_contact(level["recipients"], customer)
    cc = _resolve_contact(level["cc"], customer) if level.get("cc") else None
    original_to, original_cc = to, cc

    email_override = os.getenv("TEST_EMAIL_OVERRIDE")
    if level["channel"] == "email" and email_override:
        to, cc = email_override, None  # mirrors EmailChannel.send() — see CONTEXT.md

    invoice_rows = _customer_invoice_rows(customer) if level["channel"] == "email" else []
    would_pay_all = len(invoice_rows) > 1

    return {
        "would": "dispatch",
        "level_index": next_level_index,
        "level_name": LEVEL_NAMES[next_level_index],
        "channel": level["channel"],
        "to": to,
        "cc": cc,
        "original_to": original_to if to != original_to else None,
        "original_cc": original_cc if to != original_to else None,
        "subject": _subject_for_level(LEVEL_NAMES[next_level_index], case.invoice.invoice_no, ctx["days_overdue"]),
        "body": body,
        "invoice_rows": invoice_rows,
        "would_include_pay_all": would_pay_all,
        "wait_days": decision.wait_days,
        "urgency_score": decision.urgency_score,
        "rationale": decision.rationale,
    }


def close_case(session: Session, case: Case, *, reason: str, now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.utcnow()
    case.status = "closed"
    case.close_reason = reason
    case.closed_at = now
    case.next_action_at = None
    record_event(session, case, type="system", payload={"reason": f"closed_{reason}"})


def set_case_level(session: Session, case: Case, level_name: str) -> None:
    """Manual test/demo override: jump a case straight to a given level
    (e.g. "voice") instead of waiting for it to mechanically progress
    through spoc -> manager -> skip_level. Purely a state change — doesn't
    dispatch anything itself; the next "Send now (test)" or batch run will
    use whatever level this sets. Only ever a human's deliberate action,
    same footing as force_dispatch_case()."""
    if level_name not in LEVEL_NAMES:
        raise ValueError(f"unknown level {level_name!r}")
    old_level = LEVEL_NAMES[case.level_index] if case.playbook_name else None
    case.level_index = LEVEL_NAMES.index(level_name)
    case.playbook_name = PLAYBOOK_NAME
    record_event(session, case, type="system", payload={"reason": "level_override", "from": old_level, "to": level_name})
    session.commit()


def pause_case(session: Session, case: Case, *, reason: str) -> None:
    case.status = "paused"
    record_event(session, case, type="system", payload={"reason": reason})


def render_sample_message(channel_name: str) -> dict:
    """A representative message for the given channel, built from
    placeholder data — decoupled from any real case, purely to verify a
    channel integration (does Twilio Voice actually work, what does it
    sound like) without touching real case state or credentials."""
    playbook = get_playbook(PLAYBOOK_NAME)
    level = next((lvl for lvl in playbook["levels"] if lvl["channel"] == channel_name), None)
    if level is None:
        raise KeyError(f"no playbook level uses channel {channel_name!r}")

    sample_ctx = {
        "customer_name": "Sample Customer Pvt Ltd",
        "invoice_no": "INV-TEST-001",
        "amount": format_inr(50_000.0),
        "customer_outstanding": format_inr(50_000.0),
        "days_overdue": 45,
        "spoc_name": "Sample SPOC",
        "manager_name": "Sample Manager",
        "skip_level_name": "Sample Skip-level",
        "pay_link": "https://rzp.io/l/sample-test-link",
    }
    body = render(level["message"], sample_ctx)
    return {"channel": channel_name, "body": body, "recipients_role": level["recipients"]}


def _voice_message_for_case(session: Session, case: Case, today: dt.date) -> tuple[str, str]:
    """Renders the playbook's voice script against this case's REAL data
    (customer name, invoice, days overdue) regardless of what level the
    case is actually at — voice is normally reached only after the whole
    internal email chain is exhausted, so without this there'd be no way
    to hear what a given customer's call would actually say until then.
    Returns (to, body)."""
    playbook = get_playbook(PLAYBOOK_NAME)
    level = next(lvl for lvl in playbook["levels"] if lvl["channel"] == "voice")
    ctx = _build_case_context(session, case, today)
    body = render(level["message"], ctx)
    to = _resolve_contact(level["recipients"], case.customer)
    return to, body


def preview_voice_call(session: Session, case: Case, today: dt.date | None = None) -> dict:
    """Read-only: what the voice call for THIS case would say and who it'd
    dial, using real case data — no call is placed."""
    today = today or dt.date.today()
    to, body = _voice_message_for_case(session, case, today)
    return {"to": to, "body": body}


def send_voice_call_test(session: Session, case: Case, today: dt.date | None = None) -> dict:
    """Places a REAL call for this case's real customer/invoice, purely to
    verify the voice channel + this case's script — deliberately independent
    of the case's actual escalation level/state: doesn't touch level_index,
    playbook_name, touch_count, or next_action_at, since a one-off test call
    is not the same event as the case mechanically reaching voice."""
    today = today or dt.date.today()
    to, body = _voice_message_for_case(session, case, today)
    result = get_channel("voice").send(to=to, cc=None, subject="Voice test", body=body)
    record_event(session, case, type="dispatch", channel="voice", payload={"to": to, "status": result.status, "detail": result.detail, "test": True})
    session.commit()
    return {"to": to, "body": body, "status": result.status, "detail": result.detail}

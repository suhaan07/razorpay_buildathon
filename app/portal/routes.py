from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.cases.engine import (
    flag_dispute,
    force_dispatch_case,
    get_pause_info,
    get_settings,
    is_disputed,
    preview_next_message,
    preview_voice_call,
    record_promise,
    reopen_case,
    run_batch,
    send_voice_call_test,
    set_case_level,
    sync_cases,
)
from app.chatbot import agent as chatbot_agent
from app.data.ageing import bucket_for
from app.data.ingest import IngestError, ingest_xlsx
from app.data.models import Case, Invoice, PromiseToPay, ReliabilityScore
from app.db import get_session
from app.decisions.decision_layer import LEVEL_NAMES
from app.integrations import razorpay_client, twilio_client
from app.integrations.razorpay_client import is_oversized_stub
from app.playbooks.loader import get_registry
from app.reports.batch_report import build_report
from app.reports.cash_flow_forecast import build_cash_flow_forecast
from app.reports.format_utils import format_date, format_inr, format_ist

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

BUCKET_ORDER = ["All", "Not Due", "0-15", "16-30", "31-60", "61-90", "90+"]
BUCKET_LABELS = {
    "All": "All", "Not Due": "Not Due", "0-15": "0-15 Days", "16-30": "16-30 Days",
    "31-60": "31-60 Days", "61-90": "61-90 Days", "90+": "90+ Days",
}


def _get_case_or_404(case_id: int, session: Session) -> Case:
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return case


def _format_payload(payload: dict | None) -> str:
    """Timeline entries stored a raw dict for machine use; render it as
    readable "key: value" text instead of Python's dict repr (quotes,
    braces) leaking straight into the UI."""
    if not payload:
        return ""
    return " · ".join(f"{k}: {v}" for k, v in payload.items() if v is not None)


def _build_case_detail(session: Session, case: Case) -> dict:
    """Everything the case card needs in one place: not just this one
    invoice, but every other outstanding invoice for the same customer, the
    reliability score, and — critically — the rationale/urgency score
    behind whatever schedule is currently in effect, taken from the most
    recent actual decision (not a fresh live re-decide, which could differ
    from what actually produced the current next_action_at)."""

    customer = case.customer
    invoice = case.invoice
    score = session.get(ReliabilityScore, case.customer_id)

    other_invoices = sorted(
        (inv for inv in customer.invoices if inv.outstanding > 0),
        key=lambda inv: (inv.due_date is None, inv.due_date or dt.date.max),
    )
    invoice_rows = [
        {
            "invoice_no": inv.invoice_no,
            "due_date_display": format_date(inv.due_date),
            "amount_display": format_inr(inv.outstanding),
            "is_this_case": inv.id == invoice.id,
        }
        for inv in other_invoices
    ]

    sorted_events = sorted(case.events, key=lambda e: e.created_at, reverse=True)
    last_decision = next((e for e in sorted_events if e.type == "decision"), None)

    latest_promise = (
        session.query(PromiseToPay)
        .filter(PromiseToPay.customer_id == customer.id)
        .order_by(PromiseToPay.created_at.desc())
        .first()
    )

    events = [
        {
            "type": e.type,
            "channel": e.channel,
            "rationale": e.rationale,
            "payload_display": _format_payload(e.payload),
            "created_at_display": format_ist(e.created_at),
        }
        for e in sorted_events
    ]

    return {
        "id": case.id,
        "customer": customer.name,
        "invoice_no": invoice.invoice_no,
        "status": case.status,
        "disputed": is_disputed(case),
        "bucket": case.bucket,
        "outstanding_display": format_inr(invoice.outstanding),
        "level": LEVEL_NAMES[case.level_index] if case.playbook_name else None,
        # A stub from a Razorpay-side rejection (oversized amount, or the
        # test account's link quota — see razorpay_client.py) must never be
        # shown as a clickable link here, same rule as the escalation email
        # and WhatsApp bot: clicking it does nothing. This link is for THIS
        # invoice only — the consolidated "pay everything" link (when more
        # than one invoice is outstanding) is a separate one, shown below it.
        "pay_link_url": case.pay_link_url if case.pay_link_url and not is_oversized_stub(case.pay_link_id) else None,
        "pay_link_unavailable": bool(case.pay_link_id and is_oversized_stub(case.pay_link_id)),
        "consolidated_pay_link_url": (
            customer.consolidated_pay_link_url
            if len(invoice_rows) > 1 and customer.consolidated_pay_link_url and not is_oversized_stub(customer.consolidated_pay_link_id)
            else None
        ),
        "consolidated_pay_link_unavailable": bool(
            len(invoice_rows) > 1 and customer.consolidated_pay_link_id and is_oversized_stub(customer.consolidated_pay_link_id)
        ),
        "consolidated_total_display": format_inr(sum(inv.outstanding for inv in other_invoices)) if len(invoice_rows) > 1 else None,
        "score": round(score.score, 0) if score else None,
        "band": score.band if score else None,
        "avg_days_late": round(score.avg_days_late, 1) if score else None,
        "on_time_rate_pct": round(score.on_time_rate * 100, 0) if score else None,
        "next_action_display": format_ist(case.next_action_at) if case.next_action_at else "due now",
        "last_decision": {
            "urgency_score": last_decision.payload.get("urgency_score") if last_decision and last_decision.payload else None,
            "wait_days": last_decision.payload.get("wait_days") if last_decision and last_decision.payload else None,
            "suggested_level": last_decision.payload.get("suggested_level") if last_decision and last_decision.payload else None,
            "rationale": last_decision.rationale if last_decision else None,
            "decided_at_display": format_ist(last_decision.created_at) if last_decision else None,
        } if last_decision else None,
        "other_invoices": invoice_rows,
        "events": events,
        "promise": {
            "status": latest_promise.status,
            "promised_date_display": format_date(latest_promise.promised_date),
            "source": latest_promise.source,
        } if latest_promise else None,
    }


@router.get("/")
def root_redirect():
    # Upload + Run batch now live at the top of the Invoices page (see
    # invoices_page below) — no more standalone Upload page.
    return RedirectResponse(url="/invoices")


@router.post("/upload")
async def upload(file: UploadFile, session: Session = Depends(get_session)):
    try:
        result = ingest_xlsx(session, await file.read(), file.filename)
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sync_cases(session)  # so /cases shows every overdue invoice immediately, before the first "Run batch"
    return {
        "row_count": result.row_count,
        "customer_count": result.customer_count,
        "missing_due_date_count": result.missing_due_date_count,
    }


@router.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request):
    return templates.TemplateResponse("invoices.html", {"request": request})


@router.get("/api/invoices")
def api_invoices(tab: str = "All", q: str | None = None, session: Session = Depends(get_session)):
    today = dt.date.today()
    invoices = session.query(Invoice).all()

    def bucket_of(inv: Invoice) -> str:
        return bucket_for(inv.due_date, today)

    # Tab counts/totals are computed over the UNFILTERED set so the chips
    # stay stable while the user searches (matches the reference behavior).
    tabs = []
    for key in BUCKET_ORDER:
        matching = invoices if key == "All" else [i for i in invoices if bucket_of(i) == key]
        total = sum(i.outstanding for i in matching)
        tabs.append({"key": key, "label": BUCKET_LABELS[key], "count": len(matching), "total_display": format_inr(total)})

    filtered = invoices if tab == "All" else [i for i in invoices if bucket_of(i) == tab]
    if q:
        needle = q.lower()
        filtered = [i for i in filtered if needle in i.customer.name.lower() or needle in i.invoice_no.lower()]

    filtered_sorted = sorted(filtered, key=lambda i: (i.due_date is None, i.due_date or dt.date.max))

    rows = [
        {
            "invoice_no": i.invoice_no,
            "customer": i.customer.name,
            "due_date_display": format_date(i.due_date),
            "outstanding_display": format_inr(i.outstanding),
            "bucket": bucket_of(i),
            "unclassified": i.due_date is None,
            "case_id": i.case.id if i.case else None,
        }
        for i in filtered_sorted
    ]

    return {
        "tabs": tabs,
        "rows": rows,
        "count": len(rows),
        "unclassified_count": sum(1 for i in invoices if i.due_date is None),
    }


_NEEDS_REVIEW_STATUSES = ("paused", "exhausted")


def _review_info(case: Case) -> dict:
    """Unifies paused-case pause info and exhausted-case "why" into the
    same shape, for the Needs Review queue — a case that ran out the full
    escalation chain unpaid needs a human just as much as a disputed one
    does, even though it's a different status and reaches this state a
    different way."""
    if case.status == "exhausted":
        return {
            "reason": "exhausted",
            "detail": "Full chain completed (spoc → manager → skip_level → voice) with no payment.",
            "at_display": format_ist(case.closed_at) if case.closed_at else None,
        }
    pause_info = get_pause_info(case)
    if pause_info is None:
        return {"reason": None, "detail": None, "at_display": None}
    return {
        "reason": pause_info["reason"],
        "detail": pause_info["detail"],
        "at_display": format_ist(pause_info["at"]) if pause_info["at"] else None,
    }


@router.get("/cases", response_class=HTMLResponse)
def cases_page(request: Request, view: str = "all", session: Session = Depends(get_session)):
    all_cases = session.query(Case).order_by(Case.created_at.desc()).all()
    needs_review_count = sum(1 for c in all_cases if c.status in _NEEDS_REVIEW_STATUSES)

    cases = [c for c in all_cases if c.status in _NEEDS_REVIEW_STATUSES] if view == "needs_review" else all_cases

    rows = []
    for c in cases:
        score = session.get(ReliabilityScore, c.customer_id)
        info = _review_info(c)
        rows.append(
            {
                "id": c.id,
                "customer": c.customer.name,
                "invoice_no": c.invoice.invoice_no,
                "bucket": c.bucket,
                "status": c.status,
                "disputed": info["reason"] == "disputed",
                "pause_reason": info["reason"],
                "pause_detail": info["detail"],
                "paused_at_display": info["at_display"],
                "level": LEVEL_NAMES[c.level_index] if c.playbook_name else "-",
                "outstanding": format_inr(c.invoice.outstanding),
                "score": round(score.score, 0) if score else "-",
                "band": score.band if score else "-",
            }
        )
    return templates.TemplateResponse(
        "cases.html",
        {"request": request, "cases": rows, "view": view, "needs_review_count": needs_review_count},
    )


@router.get("/api/cases/export.csv")
def api_cases_export_csv(session: Session = Depends(get_session)):
    """Raw, unformatted numbers (not ₹-strings) — this is for a spreadsheet
    to sum/pivot, not for reading on screen; the portal already covers
    that. utf-8-sig so Excel on Windows opens customer names with
    non-ASCII characters correctly instead of mangling them."""
    import csv
    import io

    cases = session.query(Case).order_by(Case.created_at.desc()).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Case ID", "Customer", "Invoice No", "Due Date", "Outstanding", "Bucket", "Status", "Disputed",
        "Escalation Level", "Reliability Score", "Reliability Band", "Next Action (IST)", "Touch Count",
    ])
    for c in cases:
        score = session.get(ReliabilityScore, c.customer_id)
        writer.writerow([
            c.id,
            c.customer.name,
            c.invoice.invoice_no,
            format_date(c.invoice.due_date),
            c.invoice.outstanding,
            c.bucket,
            c.status,
            "yes" if is_disputed(c) else "no",
            LEVEL_NAMES[c.level_index] if c.playbook_name else "-",
            score.score if score else "",
            score.band if score else "",
            format_ist(c.next_action_at) if c.next_action_at else "",
            c.touch_count,
        ])

    return Response(
        content=buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="cases_{dt.date.today().isoformat()}.csv"'},
    )


@router.get("/cases/{case_id}", response_class=HTMLResponse)
def case_detail_page(case_id: int, request: Request, session: Session = Depends(get_session)):
    case = _get_case_or_404(case_id, session)
    return templates.TemplateResponse("case_detail.html", {"request": request, "case": case})


@router.get("/api/cases/{case_id}")
def api_case_detail(case_id: int, session: Session = Depends(get_session)):
    case = _get_case_or_404(case_id, session)
    return _build_case_detail(session, case)


@router.get("/api/cases/{case_id}/preview")
def api_case_preview(case_id: int, session: Session = Depends(get_session)):
    case = _get_case_or_404(case_id, session)
    return preview_next_message(session, case)


@router.post("/api/cases/{case_id}/dispatch-now")
def api_case_dispatch_now(case_id: int, session: Session = Depends(get_session)):
    case = _get_case_or_404(case_id, session)
    outcome = force_dispatch_case(session, case)
    return {"outcome": outcome}


@router.get("/api/cases/{case_id}/voice-preview")
def api_case_voice_preview(case_id: int, session: Session = Depends(get_session)):
    case = _get_case_or_404(case_id, session)
    return preview_voice_call(session, case)


@router.post("/api/cases/{case_id}/voice-test")
def api_case_voice_test(case_id: int, session: Session = Depends(get_session)):
    case = _get_case_or_404(case_id, session)
    return send_voice_call_test(session, case)


class SetLevelRequest(BaseModel):
    level: str


@router.post("/api/cases/{case_id}/set-level")
def api_case_set_level(case_id: int, payload: SetLevelRequest, session: Session = Depends(get_session)):
    case = _get_case_or_404(case_id, session)
    try:
        set_case_level(session, case, payload.level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"level": payload.level}


@router.post("/api/cases/{case_id}/reopen")
def api_case_reopen(case_id: int, session: Session = Depends(get_session)):
    case = _get_case_or_404(case_id, session)
    if case.status not in ("paused", "exhausted"):
        raise HTTPException(status_code=400, detail=f"case is {case.status} — nothing to reopen")
    reopen_case(session, case)
    return {"status": case.status}


class LogPromiseRequest(BaseModel):
    promised_date: str  # ISO YYYY-MM-DD, from a <input type="date">


@router.post("/api/cases/{case_id}/promise")
def api_case_log_promise(case_id: int, payload: LogPromiseRequest, session: Session = Depends(get_session)):
    """Manual equivalent of the WhatsApp "Promise to pay for X by Y" —
    for a promise a SPOC got over a phone call rather than through the bot.
    Same validation as the WhatsApp path (see app/router/date_phrase.py):
    no past dates, nothing implausibly far out."""
    case = _get_case_or_404(case_id, session)
    try:
        promised_date = dt.date.fromisoformat(payload.promised_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid date — expected YYYY-MM-DD") from exc

    today = dt.date.today()
    if promised_date < today:
        raise HTTPException(status_code=400, detail="promised date can't be in the past")
    if (promised_date - today).days > 365:
        raise HTTPException(status_code=400, detail="that's too far out — please pick a nearer date")

    promise = record_promise(session, case.customer, promised_date, source="manual")
    return {"status": promise.status, "promised_date": promise.promised_date.isoformat()}


class LogDisputeRequest(BaseModel):
    reason: str | None = None


@router.post("/api/cases/{case_id}/dispute")
def api_case_log_dispute(case_id: int, payload: LogDisputeRequest, session: Session = Depends(get_session)):
    """Manual equivalent of the WhatsApp "Dispute for X: reason" — for
    flagging a dispute raised over a phone call rather than through the
    bot. Pauses every open case for this customer, same as the WhatsApp
    and chatbot paths (app/cases/engine.py::flag_dispute)."""
    case = _get_case_or_404(case_id, session)
    if case.status != "open":
        raise HTTPException(status_code=400, detail=f"case is {case.status}, not open — nothing to dispute")
    cases = flag_dispute(session, case.customer, payload.reason)
    return {"cases_paused": len(cases)}


@router.post("/batch/run")
def batch_run(session: Session = Depends(get_session)):
    return run_batch(session)


class AutoDispatchRequest(BaseModel):
    paused: bool


@router.get("/api/settings")
def api_get_settings(session: Session = Depends(get_session)):
    return {"auto_dispatch_paused": get_settings(session).auto_dispatch_paused}


@router.post("/api/settings/auto-dispatch")
def api_set_auto_dispatch(payload: AutoDispatchRequest, session: Session = Depends(get_session)):
    settings = get_settings(session)
    settings.auto_dispatch_paused = payload.paused
    session.commit()
    return {"auto_dispatch_paused": settings.auto_dispatch_paused}


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "auto_dispatch_paused": get_settings(session).auto_dispatch_paused,
            "razorpay_configured": razorpay_client.is_configured(),
            "twilio_configured": twilio_client.is_configured(),
            "sendgrid_configured": bool(os.getenv("SENDGRID_API_KEY")),
            "anthropic_configured": chatbot_agent.is_configured(),
            "test_email_override": os.getenv("TEST_EMAIL_OVERRIDE") or None,
            "payment_notify_whatsapp_to": os.getenv("PAYMENT_NOTIFY_WHATSAPP_TO") or None,
        },
    )


class ChatRequest(BaseModel):
    message: str


# In-memory, single conversation — a demo/ops tool, not a multi-user
# product surface, so this deliberately doesn't need a DB table. Resets on
# server restart or an explicit "New conversation"; that's an acceptable
# tradeoff for what this is.
_CHAT_STATE: dict = {"messages": [], "pending_action": None, "display_log": []}


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "anthropic_configured": chatbot_agent.is_configured(),
            "display_log": _CHAT_STATE["display_log"],
        },
    )


@router.post("/api/chat")
def api_chat(payload: ChatRequest, session: Session = Depends(get_session)):
    result = chatbot_agent.chat(session, _CHAT_STATE["messages"], _CHAT_STATE["pending_action"], payload.message)
    _CHAT_STATE["messages"] = result.messages
    _CHAT_STATE["pending_action"] = result.pending_action
    _CHAT_STATE["display_log"].append({"role": "user", "text": payload.message})
    _CHAT_STATE["display_log"].append({"role": "assistant", "text": result.reply})
    return {"reply": result.reply, "pending_confirmation": result.pending_action is not None}


@router.post("/api/chat/reset")
def api_chat_reset():
    _CHAT_STATE["messages"] = []
    _CHAT_STATE["pending_action"] = None
    _CHAT_STATE["display_log"] = []
    return {"status": "reset"}


@router.get("/report", response_class=HTMLResponse)
def report_page(request: Request, session: Session = Depends(get_session)):
    report = build_report(session)
    forecast = build_cash_flow_forecast(session)
    return templates.TemplateResponse(
        "report.html",
        {"request": request, "report": report, "forecast": forecast, "format_inr": format_inr},
    )


@router.get("/api/report")
def api_report(session: Session = Depends(get_session)):
    report = build_report(session)
    return {
        "total_cases": report.total_cases,
        "closed_paid": report.closed_paid,
        "open_cases": report.open_cases,
        "paused_cases": report.paused_cases,
        "exhausted_cases": report.exhausted_cases,
        "recovery_rate": report.recovery_rate,
        "avg_days_to_recovery": report.avg_days_to_recovery,
        "escalated_beyond_spoc": report.escalated_beyond_spoc,
        "reached_voice": report.reached_voice,
        "recovered_amount": report.recovered_amount,
        "outstanding_amount": report.outstanding_amount,
        "exceptions": [e.__dict__ for e in report.exceptions],
    }


@router.get("/api/report/cash-flow")
def api_cash_flow_forecast(session: Session = Depends(get_session)):
    forecast = build_cash_flow_forecast(session)
    return {
        "buckets": [
            {"key": b.key, "label": b.label, "total_amount": b.total_amount, "count": b.count, "low_confidence_amount": b.low_confidence_amount}
            for b in forecast.buckets
        ],
        "no_due_date_amount": forecast.no_due_date_amount,
        "no_due_date_count": forecast.no_due_date_count,
        "total_outstanding": forecast.total_outstanding,
    }


@router.get("/api/playbooks")
def api_playbooks():
    return get_registry()

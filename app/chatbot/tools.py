"""Executable backends for the chatbot's tools — thin wrappers around
functions that already exist and are already tested elsewhere in this
codebase (app/reports/, app/cases/engine.py). No new business logic lives
here; this module's only job is turning a tool call's arguments into the
right existing function call and returning JSON-serializable data. The
chatbot's LLM never computes a number or an escalation decision itself —
it only picks which of these to call, with what arguments, and narrates
the result. See app/chatbot/agent.py for the confirmation gating on
WRITE_TOOLS."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.cases.engine import close_case, flag_dispute, force_dispatch_case, get_or_create_case_payment_link, reopen_case
from app.data.models import Case, Customer, Invoice, ReliabilityScore
from app.integrations.razorpay_client import is_oversized_stub
from app.matching.resolver import resolve_customer
from app.reports.base import get_customer_report_data
from app.reports.cash_flow_forecast import build_cash_flow_forecast
from app.reports.format_utils import format_date

WRITE_TOOLS = {"generate_payment_link", "send_reminder_email", "flag_dispute", "resolve_case"}

TOOL_SCHEMAS = [
    {
        "name": "get_account_status",
        "description": (
            "Look up a customer's current dues: overdue amount, amount due this week, total outstanding, "
            "ageing breakdown, and every open invoice. Use for any question about what a specific company "
            "owes or when it's due."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {
                    "type": "string",
                    "description": "The customer/company name, as typed by the user (fuzzy matching handles minor spelling differences).",
                }
            },
            "required": ["customer_name"],
        },
    },
    {
        "name": "get_cash_flow_forecast",
        "description": (
            "Get the full cash-flow forecast: how much outstanding money is predicted to be recovered in "
            "each time window (overdue, this week, next week, 2-4 weeks out, beyond 30 days), based on due "
            "dates, each customer's historical payment lateness, and any active promise-to-pay. Use for any "
            "question about expected future recovery, e.g. 'how much will we get in a month'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_customers_by_outstanding",
        "description": (
            "List customers filtered by their total outstanding amount, e.g. 'companies owing less than "
            "50000' or 'customers with more than 2 lakh outstanding'. Omit an argument you don't need."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_amount": {"type": "number", "description": "Only customers whose total outstanding is at or below this (rupees)."},
                "min_amount": {"type": "number", "description": "Only customers whose total outstanding is at or above this (rupees)."},
            },
        },
    },
    {
        "name": "generate_payment_link",
        "description": (
            "Create (or fetch the existing) real Razorpay payment link for one specific invoice. This "
            "spends the account's real, finite payment-link quota — only call after the person has "
            "confirmed they want this specific invoice linked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"invoice_no": {"type": "string", "description": "The invoice number, e.g. INV-1131."}},
            "required": ["invoice_no"],
        },
    },
    {
        "name": "send_reminder_email",
        "description": (
            "Immediately send the next escalation email for one specific invoice's case (spoc/manager/"
            "skip-level, whichever it's due for), bypassing the normal wait schedule. This sends a real "
            "email — only call after the person has confirmed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"invoice_no": {"type": "string", "description": "The invoice number, e.g. INV-1131."}},
            "required": ["invoice_no"],
        },
    },
    {
        "name": "get_reliability_trend",
        "description": (
            "Check whether a customer's payment behaviour is improving, worsening, or stable, by comparing "
            "their average payment lateness on their earlier paid invoices vs their more recent ones. Use "
            "for questions like 'is Acme getting better or worse at paying'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"customer_name": {"type": "string", "description": "The customer/company name."}},
            "required": ["customer_name"],
        },
    },
    {
        "name": "get_riskiest_customers",
        "description": (
            "Rank customers by risk = their outstanding amount weighted by how unreliable they've been "
            "(a customer owing a lot AND paying late historically ranks above one owing more but paying "
            "reliably). Use for 'who are my riskiest customers' or 'who should I worry about'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "How many to return. Defaults to 10."}},
        },
    },
    {
        "name": "flag_dispute",
        "description": (
            "Pause escalation on every open case for a customer pending human review, because they've "
            "disputed an invoice (wrong amount, already paid another way, etc.). This stops all automated "
            "reminders for them until someone resolves it — only call after the person has confirmed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string", "description": "The customer/company name."},
                "reason": {"type": "string", "description": "Why it's disputed, if known."},
            },
            "required": ["customer_name"],
        },
    },
    {
        "name": "resolve_case",
        "description": (
            "Either reopen a paused OR exhausted case to resume normal escalation (the dispute/pause turned "
            "out to be invalid, or an exhausted case deserves another attempt — it restarts from the "
            "beginning of the chain in that case), or close a case as manually resolved (e.g. paid through "
            "another channel, written off) so it stops being chased. Only call after the person has confirmed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_no": {"type": "string", "description": "The invoice number, e.g. INV-1131."},
                "action": {"type": "string", "enum": ["close", "reopen"], "description": "\"reopen\" resumes escalation; \"close\" marks it resolved and stops chasing it."},
                "note": {"type": "string", "description": "Optional reason, used only when closing."},
            },
            "required": ["invoice_no", "action"],
        },
    },
]


def _find_case_by_invoice_no(session: Session, invoice_no: str) -> Case | None:
    invoice = session.query(Invoice).filter(Invoice.invoice_no == invoice_no).first()
    if invoice is None or invoice.case is None:
        return None
    return invoice.case


def _resolve_customer(session: Session, name_query: str) -> tuple[Customer | None, dict | None]:
    """Shared name-matching for the tools below — returns (customer, None)
    on a clean match, or (None, <error dict>) for not-found/ambiguous,
    same shape convention as every other tool result here."""
    lookup = resolve_customer(session, name_query)
    if lookup.status == "not_found":
        return None, {"status": "not_found", "query": name_query}
    if lookup.status == "ambiguous":
        return None, {"status": "ambiguous", "candidates": lookup.candidates}
    return lookup.customer, None


def _get_account_status(session: Session, customer_name: str) -> dict:
    result = get_customer_report_data(session, customer_name)
    if result.status == "not_found":
        return {"status": "not_found", "query": customer_name}
    if result.status == "ambiguous":
        return {"status": "ambiguous", "candidates": result.candidates}

    data = result.data
    return {
        "status": "matched",
        "customer_name": data.customer_name,
        "overdue_amount": data.overdue_amount,
        "due_this_week_amount": data.due_this_week_amount,
        "total_outstanding": data.total_outstanding,
        "ageing_breakdown": data.ageing_breakdown,
        "invoices": [
            {"invoice_no": inv.invoice_no, "due_date": format_date(inv.due_date), "outstanding": inv.outstanding}
            for inv in data.invoices
        ],
        "pay_link_url": data.pay_link_url,
        "pay_link_unavailable": data.pay_link_unavailable,
    }


def _get_cash_flow_forecast(session: Session) -> dict:
    forecast = build_cash_flow_forecast(session)
    return {
        "buckets": [
            {"label": b.label, "total_amount": b.total_amount, "count": b.count, "low_confidence_amount": b.low_confidence_amount}
            for b in forecast.buckets
        ],
        "no_due_date_amount": forecast.no_due_date_amount,
        "no_due_date_count": forecast.no_due_date_count,
        "total_outstanding": forecast.total_outstanding,
    }


def _list_customers_by_outstanding(session: Session, min_amount: float | None, max_amount: float | None) -> dict:
    rows = []
    for customer in session.query(Customer).all():
        total = sum(inv.outstanding for inv in customer.invoices)
        if total <= 0:
            continue
        if min_amount is not None and total < min_amount:
            continue
        if max_amount is not None and total > max_amount:
            continue
        rows.append({"customer_name": customer.name, "total_outstanding": round(total, 2)})
    rows.sort(key=lambda r: r["total_outstanding"], reverse=True)
    return {"customers": rows, "count": len(rows)}


def _generate_payment_link(session: Session, invoice_no: str) -> dict:
    case = _find_case_by_invoice_no(session, invoice_no)
    if case is None:
        return {"status": "not_found", "invoice_no": invoice_no}
    link = get_or_create_case_payment_link(session, case)
    session.commit()
    unavailable = is_oversized_stub(link["id"])
    return {
        "status": "ok",
        "invoice_no": invoice_no,
        "pay_link_url": link["short_url"] if not unavailable else None,
        "unavailable": unavailable,
    }


def _send_reminder_email(session: Session, invoice_no: str) -> dict:
    case = _find_case_by_invoice_no(session, invoice_no)
    if case is None:
        return {"status": "not_found", "invoice_no": invoice_no}
    if case.status != "open":
        return {"status": "not_open", "invoice_no": invoice_no, "case_status": case.status}
    outcome = force_dispatch_case(session, case)
    return {"status": "ok", "invoice_no": invoice_no, "outcome": outcome}


def _get_reliability_trend(session: Session, customer_name: str) -> dict:
    customer, error = _resolve_customer(session, customer_name)
    if error is not None:
        return error

    paid = sorted(
        (inv for inv in customer.invoices if inv.paid_at is not None and inv.due_date is not None),
        key=lambda inv: inv.paid_at,
    )
    if len(paid) < 2:
        return {"status": "insufficient_history", "customer_name": customer.name, "paid_invoice_count": len(paid)}

    midpoint = len(paid) // 2
    earlier, recent = paid[:midpoint], paid[midpoint:]

    def _avg_lateness(invoices) -> float:
        return sum(max(0, (inv.paid_at - inv.due_date).days) for inv in invoices) / len(invoices)

    earlier_avg = round(_avg_lateness(earlier), 1)
    recent_avg = round(_avg_lateness(recent), 1)
    direction = "improving" if recent_avg < earlier_avg else "worsening" if recent_avg > earlier_avg else "stable"

    return {
        "status": "ok",
        "customer_name": customer.name,
        "earlier_avg_days_late": earlier_avg,
        "recent_avg_days_late": recent_avg,
        "direction": direction,
        "paid_invoice_count": len(paid),
    }


def _get_riskiest_customers(session: Session, limit: int | None) -> dict:
    limit = limit or 10
    rows = []
    for customer in session.query(Customer).all():
        total_outstanding = sum(inv.outstanding for inv in customer.invoices)
        if total_outstanding <= 0:
            continue
        score_row = session.get(ReliabilityScore, customer.id)
        reliability_score = score_row.score if score_row else 70.0
        risk_weighted_amount = round(total_outstanding * (100 - reliability_score) / 100, 2)
        rows.append({
            "customer_name": customer.name,
            "total_outstanding": round(total_outstanding, 2),
            "reliability_score": reliability_score,
            "risk_weighted_amount": risk_weighted_amount,
        })
    rows.sort(key=lambda r: r["risk_weighted_amount"], reverse=True)
    top = rows[:limit]
    return {"customers": top, "count": len(top)}


def _flag_dispute(session: Session, customer_name: str, reason: str | None) -> dict:
    customer, error = _resolve_customer(session, customer_name)
    if error is not None:
        return error
    cases = flag_dispute(session, customer, reason)
    return {"status": "ok", "customer_name": customer.name, "cases_paused": len(cases)}


def _resolve_case(session: Session, invoice_no: str, action: str, note: str | None) -> dict:
    case = _find_case_by_invoice_no(session, invoice_no)
    if case is None:
        return {"status": "not_found", "invoice_no": invoice_no}

    if action == "reopen":
        if case.status not in ("paused", "exhausted"):
            return {"status": "not_reopenable", "invoice_no": invoice_no, "case_status": case.status}
        reopen_case(session, case)
        return {"status": "ok", "invoice_no": invoice_no, "action": "reopen"}

    if action == "close":
        if case.status in ("closed", "exhausted"):
            return {"status": "already_terminal", "invoice_no": invoice_no, "case_status": case.status}
        close_case(session, case, reason=note or "resolved_manually")
        session.commit()
        return {"status": "ok", "invoice_no": invoice_no, "action": "close"}

    return {"status": "invalid_action", "action": action}


_EXECUTORS = {
    "get_account_status": lambda session, args: _get_account_status(session, args["customer_name"]),
    "get_cash_flow_forecast": lambda session, args: _get_cash_flow_forecast(session),
    "list_customers_by_outstanding": lambda session, args: _list_customers_by_outstanding(session, args.get("min_amount"), args.get("max_amount")),
    "generate_payment_link": lambda session, args: _generate_payment_link(session, args["invoice_no"]),
    "send_reminder_email": lambda session, args: _send_reminder_email(session, args["invoice_no"]),
    "get_reliability_trend": lambda session, args: _get_reliability_trend(session, args["customer_name"]),
    "get_riskiest_customers": lambda session, args: _get_riskiest_customers(session, args.get("limit")),
    "flag_dispute": lambda session, args: _flag_dispute(session, args["customer_name"], args.get("reason")),
    "resolve_case": lambda session, args: _resolve_case(session, args["invoice_no"], args["action"], args.get("note")),
}


def execute_tool(session: Session, name: str, args: dict) -> dict:
    executor = _EXECUTORS.get(name)
    if executor is None:
        return {"error": f"unknown tool {name!r}"}
    return executor(session, args)


def describe_pending_action(name: str, args: dict) -> str:
    """Human-readable description of a WRITE tool call, shown before it
    runs, so a confirmation actually means something concrete."""
    if name == "generate_payment_link":
        return f"generate a real Razorpay payment link for invoice {args.get('invoice_no')}"
    if name == "send_reminder_email":
        return f"send the next escalation email right now for invoice {args.get('invoice_no')}"
    if name == "flag_dispute":
        reason = args.get("reason")
        return f"pause escalation for {args.get('customer_name')} as disputed" + (f" ({reason})" if reason else "")
    if name == "resolve_case":
        if args.get("action") == "reopen":
            return f"reopen invoice {args.get('invoice_no')}'s case and resume escalation"
        return f"close invoice {args.get('invoice_no')}'s case as resolved" + (f" ({args['note']})" if args.get("note") else "")
    return f"run {name}({args})"

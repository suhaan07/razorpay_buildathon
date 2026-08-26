import datetime as dt

from app.chatbot.tools import (
    TOOL_SCHEMAS,
    WRITE_TOOLS,
    describe_pending_action,
    execute_tool,
)
from app.data.models import Case, ReliabilityScore


def test_tool_schemas_have_a_name_and_input_schema_for_every_write_tool():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert WRITE_TOOLS <= names


def test_get_account_status_matched(session, make_customer, make_invoice):
    customer = make_customer(name="Tool Test Co")
    make_invoice(customer=customer, invoice_no="TT-1", outstanding=10_000.0)

    result = execute_tool(session, "get_account_status", {"customer_name": "Tool Test Co"})
    assert result["status"] == "matched"
    assert result["customer_name"] == "Tool Test Co"
    assert result["total_outstanding"] == 10_000.0
    assert any(inv["invoice_no"] == "TT-1" for inv in result["invoices"])


def test_get_account_status_not_found(session):
    result = execute_tool(session, "get_account_status", {"customer_name": "Nonexistent Corp"})
    assert result["status"] == "not_found"


def test_get_account_status_ambiguous(session, make_customer, make_invoice):
    c1 = make_customer(name="Duplicate Name Mumbai")
    c2 = make_customer(name="Duplicate Name Delhi")
    make_invoice(customer=c1, invoice_no="DN-1")
    make_invoice(customer=c2, invoice_no="DN-2")

    result = execute_tool(session, "get_account_status", {"customer_name": "Duplicate Name"})
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2


def test_get_cash_flow_forecast_returns_five_buckets(session):
    result = execute_tool(session, "get_cash_flow_forecast", {})
    assert len(result["buckets"]) == 5
    assert "total_outstanding" in result


def test_list_customers_by_outstanding_filters_max(session, make_customer, make_invoice):
    small = make_customer(name="Small Co")
    big = make_customer(name="Big Co")
    make_invoice(customer=small, invoice_no="SM-1", outstanding=10_000.0)
    make_invoice(customer=big, invoice_no="BG-1", outstanding=500_000.0)

    result = execute_tool(session, "list_customers_by_outstanding", {"max_amount": 50_000.0})
    names = {c["customer_name"] for c in result["customers"]}
    assert "Small Co" in names
    assert "Big Co" not in names


def test_list_customers_by_outstanding_filters_min(session, make_customer, make_invoice):
    small = make_customer(name="Small Co 2")
    big = make_customer(name="Big Co 2")
    make_invoice(customer=small, invoice_no="SM-2", outstanding=10_000.0)
    make_invoice(customer=big, invoice_no="BG-2", outstanding=500_000.0)

    result = execute_tool(session, "list_customers_by_outstanding", {"min_amount": 100_000.0})
    names = {c["customer_name"] for c in result["customers"]}
    assert "Big Co 2" in names
    assert "Small Co 2" not in names


def test_list_customers_by_outstanding_excludes_zero_outstanding(session, make_customer, make_invoice):
    paid = make_customer(name="All Paid Co")
    make_invoice(customer=paid, invoice_no="AP-1", outstanding=0.0)

    result = execute_tool(session, "list_customers_by_outstanding", {})
    names = {c["customer_name"] for c in result["customers"]}
    assert "All Paid Co" not in names


def test_list_customers_by_outstanding_sorted_descending(session, make_customer, make_invoice):
    a = make_customer(name="Sort A")
    b = make_customer(name="Sort B")
    make_invoice(customer=a, invoice_no="SA-1", outstanding=10_000.0)
    make_invoice(customer=b, invoice_no="SB-1", outstanding=90_000.0)

    result = execute_tool(session, "list_customers_by_outstanding", {})
    totals = [c["total_outstanding"] for c in result["customers"]]
    assert totals == sorted(totals, reverse=True)


def test_generate_payment_link_creates_and_persists(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="GPL-1")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    result = execute_tool(session, "generate_payment_link", {"invoice_no": "GPL-1"})
    assert result["status"] == "ok"
    assert result["pay_link_url"] is not None

    session.refresh(case)
    assert case.pay_link_id is not None


def test_generate_payment_link_reuses_existing_link(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="GPL-2")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", pay_link_id="plink_existing", pay_link_url="https://rzp.io/l/existing")
    session.add(case)
    session.commit()

    result = execute_tool(session, "generate_payment_link", {"invoice_no": "GPL-2"})
    assert result["pay_link_url"] == "https://rzp.io/l/existing"


def test_generate_payment_link_invoice_not_found(session):
    result = execute_tool(session, "generate_payment_link", {"invoice_no": "NOPE-1"})
    assert result["status"] == "not_found"


def test_generate_payment_link_invoice_with_no_case_yet(session, make_invoice):
    make_invoice(outstanding=10_000.0, invoice_no="GPL-3")  # no Case row created
    result = execute_tool(session, "generate_payment_link", {"invoice_no": "GPL-3"})
    assert result["status"] == "not_found"


def test_send_reminder_email_dispatches(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, due_date=None, invoice_no="SRE-1")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="Unclassified")
    session.add(case)
    session.commit()

    result = execute_tool(session, "send_reminder_email", {"invoice_no": "SRE-1"})
    assert result["status"] == "ok"
    assert result["outcome"] == "dispatched"


def test_send_reminder_email_invoice_not_found(session):
    result = execute_tool(session, "send_reminder_email", {"invoice_no": "NOPE-2"})
    assert result["status"] == "not_found"


def test_send_reminder_email_case_not_open(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="SRE-2")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", status="closed", close_reason="paid")
    session.add(case)
    session.commit()

    result = execute_tool(session, "send_reminder_email", {"invoice_no": "SRE-2"})
    assert result["status"] == "not_open"
    assert result["case_status"] == "closed"


def test_execute_tool_unknown_name_returns_error_not_a_crash(session):
    result = execute_tool(session, "delete_everything", {})
    assert "error" in result


def test_describe_pending_action_mentions_the_invoice():
    text = describe_pending_action("generate_payment_link", {"invoice_no": "INV-99"})
    assert "INV-99" in text
    text2 = describe_pending_action("send_reminder_email", {"invoice_no": "INV-98"})
    assert "INV-98" in text2


def test_describe_pending_action_for_dispute_and_resolve():
    text = describe_pending_action("flag_dispute", {"customer_name": "Acme", "reason": "wrong amount"})
    assert "Acme" in text and "wrong amount" in text
    text2 = describe_pending_action("resolve_case", {"invoice_no": "INV-5", "action": "reopen"})
    assert "INV-5" in text2 and "reopen" in text2.lower()


# --- get_reliability_trend ---

def test_reliability_trend_insufficient_history(session, make_customer, make_invoice):
    customer = make_customer(name="Trend New Co")
    make_invoice(customer=customer, outstanding=10_000.0, invoice_no="TR-1")  # not paid at all

    result = execute_tool(session, "get_reliability_trend", {"customer_name": "Trend New Co"})
    assert result["status"] == "insufficient_history"
    assert result["paid_invoice_count"] == 0


def test_reliability_trend_improving(session, make_customer, make_invoice):
    customer = make_customer(name="Trend Improving Co")
    due = dt.date(2026, 1, 1)
    # earlier invoices: paid very late; recent invoices: paid closer to on time
    make_invoice(customer=customer, invoice_no="TR-2A", outstanding=0.0, inv_amount=1000.0, due_date=due, paid_at=due + dt.timedelta(days=20))
    make_invoice(customer=customer, invoice_no="TR-2B", outstanding=0.0, inv_amount=1000.0, due_date=due + dt.timedelta(days=30), paid_at=due + dt.timedelta(days=50))
    make_invoice(customer=customer, invoice_no="TR-2C", outstanding=0.0, inv_amount=1000.0, due_date=due + dt.timedelta(days=60), paid_at=due + dt.timedelta(days=62))
    make_invoice(customer=customer, invoice_no="TR-2D", outstanding=0.0, inv_amount=1000.0, due_date=due + dt.timedelta(days=90), paid_at=due + dt.timedelta(days=90))

    result = execute_tool(session, "get_reliability_trend", {"customer_name": "Trend Improving Co"})
    assert result["status"] == "ok"
    assert result["direction"] == "improving"
    assert result["recent_avg_days_late"] < result["earlier_avg_days_late"]


def test_reliability_trend_worsening(session, make_customer, make_invoice):
    customer = make_customer(name="Trend Worsening Co")
    due = dt.date(2026, 1, 1)
    make_invoice(customer=customer, invoice_no="TR-3A", outstanding=0.0, inv_amount=1000.0, due_date=due, paid_at=due)
    make_invoice(customer=customer, invoice_no="TR-3B", outstanding=0.0, inv_amount=1000.0, due_date=due + dt.timedelta(days=30), paid_at=due + dt.timedelta(days=30))
    make_invoice(customer=customer, invoice_no="TR-3C", outstanding=0.0, inv_amount=1000.0, due_date=due + dt.timedelta(days=60), paid_at=due + dt.timedelta(days=90))
    make_invoice(customer=customer, invoice_no="TR-3D", outstanding=0.0, inv_amount=1000.0, due_date=due + dt.timedelta(days=90), paid_at=due + dt.timedelta(days=140))

    result = execute_tool(session, "get_reliability_trend", {"customer_name": "Trend Worsening Co"})
    assert result["direction"] == "worsening"


def test_reliability_trend_not_found(session):
    result = execute_tool(session, "get_reliability_trend", {"customer_name": "Nonexistent Corp"})
    assert result["status"] == "not_found"


def test_reliability_trend_ambiguous(session, make_customer, make_invoice):
    c1 = make_customer(name="Trend Ambiguous Mumbai")
    c2 = make_customer(name="Trend Ambiguous Delhi")
    make_invoice(customer=c1, invoice_no="TRA-1")
    make_invoice(customer=c2, invoice_no="TRA-2")

    result = execute_tool(session, "get_reliability_trend", {"customer_name": "Trend Ambiguous"})
    assert result["status"] == "ambiguous"


# --- get_riskiest_customers ---

def test_riskiest_customers_ranks_poor_reliability_above_good(session, make_customer, make_invoice):
    reliable = make_customer(name="Risk Reliable Co")
    unreliable = make_customer(name="Risk Unreliable Co")
    make_invoice(customer=reliable, invoice_no="RC-1", outstanding=100_000.0)
    make_invoice(customer=unreliable, invoice_no="RC-2", outstanding=100_000.0)
    session.add(ReliabilityScore(customer_id=reliable.id, score=95.0, band="Excellent", avg_days_late=0.0, on_time_rate=1.0))
    session.add(ReliabilityScore(customer_id=unreliable.id, score=20.0, band="Poor", avg_days_late=60.0, on_time_rate=0.1))
    session.commit()

    result = execute_tool(session, "get_riskiest_customers", {})
    names_in_order = [c["customer_name"] for c in result["customers"]]
    assert names_in_order.index("Risk Unreliable Co") < names_in_order.index("Risk Reliable Co")


def test_riskiest_customers_defaults_to_neutral_score_when_no_history(session, make_customer, make_invoice):
    customer = make_customer(name="Risk No History Co")
    make_invoice(customer=customer, invoice_no="RNH-1", outstanding=50_000.0)
    result = execute_tool(session, "get_riskiest_customers", {})
    row = next(c for c in result["customers"] if c["customer_name"] == "Risk No History Co")
    assert row["reliability_score"] == 70.0


def test_riskiest_customers_excludes_zero_outstanding(session, make_customer, make_invoice):
    customer = make_customer(name="Risk Paid Off Co")
    make_invoice(customer=customer, invoice_no="RPO-1", outstanding=0.0)
    result = execute_tool(session, "get_riskiest_customers", {})
    names = {c["customer_name"] for c in result["customers"]}
    assert "Risk Paid Off Co" not in names


def test_riskiest_customers_respects_limit(session, make_customer, make_invoice):
    for i in range(5):
        c = make_customer(name=f"Risk Limit Co {i}")
        make_invoice(customer=c, invoice_no=f"RL-{i}", outstanding=10_000.0 * (i + 1))
    result = execute_tool(session, "get_riskiest_customers", {"limit": 2})
    assert result["count"] == 2


# --- flag_dispute (chatbot tool layer) ---

def test_flag_dispute_tool_happy_path(session, make_customer, make_invoice):
    customer = make_customer(name="Tool Dispute Co")
    invoice = make_invoice(customer=customer, invoice_no="TD-1", outstanding=10_000.0)
    case = Case(invoice_id=invoice.id, customer_id=customer.id, bucket="0-15")
    session.add(case)
    session.commit()

    result = execute_tool(session, "flag_dispute", {"customer_name": "Tool Dispute Co", "reason": "wrong amount"})
    assert result["status"] == "ok"
    assert result["cases_paused"] == 1

    session.refresh(case)
    assert case.status == "paused"


def test_flag_dispute_tool_not_found(session):
    result = execute_tool(session, "flag_dispute", {"customer_name": "Nonexistent Corp"})
    assert result["status"] == "not_found"


# --- resolve_case ---

def test_resolve_case_reopen_happy_path(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="RSC-1")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", status="paused")
    session.add(case)
    session.commit()

    result = execute_tool(session, "resolve_case", {"invoice_no": "RSC-1", "action": "reopen"})
    assert result["status"] == "ok"
    session.refresh(case)
    assert case.status == "open"


def test_resolve_case_reopen_when_not_reopenable(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="RSC-2")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", status="open")
    session.add(case)
    session.commit()

    result = execute_tool(session, "resolve_case", {"invoice_no": "RSC-2", "action": "reopen"})
    assert result["status"] == "not_reopenable"


def test_resolve_case_reopen_from_exhausted(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="RSC-6")
    case = Case(
        invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="90+",
        status="exhausted", close_reason="exhausted", level_index=3, playbook_name="receivables_escalation",
    )
    session.add(case)
    session.commit()

    result = execute_tool(session, "resolve_case", {"invoice_no": "RSC-6", "action": "reopen"})
    assert result["status"] == "ok"
    session.refresh(case)
    assert case.status == "open"
    assert case.level_index == 0
    assert case.playbook_name is None


def test_resolve_case_close_happy_path(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="RSC-3")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    result = execute_tool(session, "resolve_case", {"invoice_no": "RSC-3", "action": "close", "note": "paid via bank transfer"})
    assert result["status"] == "ok"
    session.refresh(case)
    assert case.status == "closed"
    assert case.close_reason == "paid via bank transfer"


def test_resolve_case_close_already_terminal(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="RSC-4")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", status="closed", close_reason="paid")
    session.add(case)
    session.commit()

    result = execute_tool(session, "resolve_case", {"invoice_no": "RSC-4", "action": "close"})
    assert result["status"] == "already_terminal"


def test_resolve_case_not_found(session):
    result = execute_tool(session, "resolve_case", {"invoice_no": "NOPE-9", "action": "close"})
    assert result["status"] == "not_found"


def test_resolve_case_invalid_action(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="RSC-5")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    result = execute_tool(session, "resolve_case", {"invoice_no": "RSC-5", "action": "delete"})
    assert result["status"] == "invalid_action"

import datetime as dt
import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.data.models import Case
from app.db import get_session
from app.main import app as fastapi_app


@pytest.fixture()
def client(session):
    fastapi_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(fastapi_app)
    fastapi_app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_chat_state():
    # _CHAT_STATE is module-level (a single in-memory conversation, by
    # design — see routes.py) so it must not leak between tests.
    import app.portal.routes as routes_module

    routes_module._CHAT_STATE["messages"] = []
    routes_module._CHAT_STATE["pending_action"] = None
    routes_module._CHAT_STATE["display_log"] = []
    yield
    routes_module._CHAT_STATE["messages"] = []
    routes_module._CHAT_STATE["pending_action"] = None
    routes_module._CHAT_STATE["display_log"] = []


def _xlsx_bytes(rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    return buf.getvalue()


def test_root_redirects_to_invoices(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/invoices"


def test_invoices_page_includes_upload_and_run_batch_controls(client):
    resp = client.get("/invoices")
    assert resp.status_code == 200
    assert "upload-form" in resp.text
    assert "run-batch" in resp.text
    assert 'id="file-input"' in resp.text


def test_upload_creates_cases_immediately_without_a_batch_run(client, session):
    import datetime as dt

    rows = [
        {
            "Customer": "Acme Pvt Ltd",
            "SPOC": "Aditi",
            "Invoice No": "INV-1",
            "Invoice Date": "2026-06-01",
            "Due Date": (dt.date.today() - dt.timedelta(days=20)).isoformat(),
            "Inv Amount": 1000.0,
            "Received": 0.0,
            "Outstanding": 1000.0,
        }
    ]
    resp = client.post("/upload", files={"file": ("sheet.xlsx", _xlsx_bytes(rows))})
    assert resp.status_code == 200

    case = session.query(Case).one()
    assert case.bucket == "16-30"
    assert case.playbook_name is None  # not dispatched yet — that's still Run batch's job


def test_settings_default_to_unpaused_in_this_test_session(client):
    # conftest.py seeds Settings(auto_dispatch_paused=False) for tests —
    # the app's own real default (a fresh row with no seed) is True, see
    # test_cases_engine.py::test_get_settings_creates_a_default_row_paused_by_default.
    resp = client.get("/api/settings")
    assert resp.json() == {"auto_dispatch_paused": False}


def test_toggling_auto_dispatch_persists(client):
    resp = client.post("/api/settings/auto-dispatch", json={"paused": True})
    assert resp.json() == {"auto_dispatch_paused": True}

    resp = client.get("/api/settings")
    assert resp.json() == {"auto_dispatch_paused": True}

    resp = client.post("/api/settings/auto-dispatch", json={"paused": False})
    assert resp.json() == {"auto_dispatch_paused": False}


def test_batch_run_endpoint_reflects_the_pause(client):
    client.post("/api/settings/auto-dispatch", json={"paused": True})
    resp = client.post("/batch/run")
    assert resp.json()["auto_dispatch_paused"] is True


def test_settings_page_renders_with_current_toggle_state(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Auto-dispatch" in resp.text
    assert "Integration status" in resp.text


def test_report_page_includes_cash_flow_forecast(client, make_invoice):
    make_invoice(outstanding=10_000.0, invoice_no="RPT-CF-1")
    resp = client.get("/report")
    assert resp.status_code == 200
    assert "Cash flow forecast" in resp.text


def test_cash_flow_api_returns_all_five_buckets(client, make_invoice):
    make_invoice(outstanding=10_000.0, invoice_no="RPT-CF-2")
    resp = client.get("/api/report/cash-flow")
    assert resp.status_code == 200
    data = resp.json()
    assert {b["key"] for b in data["buckets"]} == {"overdue", "this_week", "next_week", "week_3_4", "beyond_30"}
    assert data["total_outstanding"] == 10_000.0


def test_case_detail_api_includes_other_invoices_and_last_decision(client, session, make_customer, make_invoice):
    customer = make_customer(name="Multi Invoice Co")
    inv1 = make_invoice(customer=customer, invoice_no="MI-1", outstanding=10_000.0)
    inv2 = make_invoice(customer=customer, invoice_no="MI-2", outstanding=20_000.0)
    case1 = Case(invoice_id=inv1.id, customer_id=customer.id, bucket="0-15")
    session.add(case1)
    session.add(Case(invoice_id=inv2.id, customer_id=customer.id, bucket="0-15"))
    session.commit()

    resp = client.post(f"/api/cases/{case1.id}/dispatch-now")
    assert resp.status_code == 200

    detail = client.get(f"/api/cases/{case1.id}").json()

    assert detail["customer"] == "Multi Invoice Co"
    assert {row["invoice_no"] for row in detail["other_invoices"]} == {"MI-1", "MI-2"}
    this_case_row = next(r for r in detail["other_invoices"] if r["invoice_no"] == "MI-1")
    assert this_case_row["is_this_case"] is True
    other_row = next(r for r in detail["other_invoices"] if r["invoice_no"] == "MI-2")
    assert other_row["is_this_case"] is False

    assert detail["last_decision"] is not None
    assert detail["last_decision"]["urgency_score"] is not None
    assert detail["last_decision"]["rationale"]
    assert "IST" in detail["next_action_display"]
    assert all("IST" in e["created_at_display"] for e in detail["events"])


def test_case_detail_api_never_exposes_an_oversized_stub_as_a_real_link(client, session, make_invoice):
    # regression test: same rule as the escalation email and WhatsApp bot —
    # a stub link from a Razorpay-side rejection (oversized amount, or the
    # test account's link quota) must never be shown as clickable here.
    invoice = make_invoice(outstanding=50_000.0, invoice_no="STUB-1")
    case = Case(
        invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15",
        pay_link_id="stub_oversized_STUB-1", pay_link_url="https://rzp.io/l/stub_oversized_STUB-1",
    )
    session.add(case)
    session.commit()

    detail = client.get(f"/api/cases/{case.id}").json()
    assert detail["pay_link_url"] is None
    assert detail["pay_link_unavailable"] is True


def test_case_detail_api_shows_a_real_link_normally(client, session, make_invoice):
    invoice = make_invoice(outstanding=50_000.0, invoice_no="REAL-1")
    case = Case(
        invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15",
        pay_link_id="plink_real123", pay_link_url="https://rzp.io/l/real123",
    )
    session.add(case)
    session.commit()

    detail = client.get(f"/api/cases/{case.id}").json()
    assert detail["pay_link_url"] == "https://rzp.io/l/real123"
    assert detail["pay_link_unavailable"] is False


def test_case_detail_api_distinguishes_this_invoice_link_from_consolidated_link(client, session, make_customer, make_invoice):
    # regression test: the per-invoice link and the "pay everything" link
    # are two DIFFERENT Razorpay links for two different amounts — the card
    # must never blur them together or imply the per-invoice link covers
    # the customer's full outstanding balance.
    customer = make_customer(name="Two Invoice Co")
    customer.consolidated_pay_link_id = "plink_consolidated_real"
    customer.consolidated_pay_link_url = "https://rzp.io/l/consolidated_real"
    inv1 = make_invoice(customer=customer, invoice_no="TI-1", outstanding=10_000.0)
    make_invoice(customer=customer, invoice_no="TI-2", outstanding=20_000.0)
    case1 = Case(invoice_id=inv1.id, customer_id=customer.id, bucket="0-15", pay_link_id="plink_this_one", pay_link_url="https://rzp.io/l/this_one")
    session.add(case1)
    session.commit()

    detail = client.get(f"/api/cases/{case1.id}").json()
    assert detail["pay_link_url"] == "https://rzp.io/l/this_one"
    assert detail["consolidated_pay_link_url"] == "https://rzp.io/l/consolidated_real"
    assert detail["consolidated_total_display"] == "₹30,000.00"  # both invoices combined, not just this one


def test_case_detail_api_no_consolidated_link_shown_for_a_single_invoice_customer(client, session, make_invoice):
    invoice = make_invoice(outstanding=5_000.0, invoice_no="SI-1")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", pay_link_id="plink_x", pay_link_url="https://rzp.io/l/x")
    session.add(case)
    session.commit()

    detail = client.get(f"/api/cases/{case.id}").json()
    assert detail["consolidated_pay_link_url"] is None
    assert detail["consolidated_total_display"] is None


def test_voice_preview_endpoint_uses_real_case_data(client, session, make_customer, make_invoice):
    customer = make_customer(name="Voice API Co", phone="+919876512399")
    invoice = make_invoice(customer=customer, invoice_no="VAPI-1", outstanding=10_000.0)
    case = Case(invoice_id=invoice.id, customer_id=customer.id, bucket="0-15")
    session.add(case)
    session.commit()

    resp = client.get(f"/api/cases/{case.id}/voice-preview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["to"] == "+919876512399"
    assert "Voice API Co" in data["body"]

    session.refresh(case)
    assert case.playbook_name is None  # read-only


def test_voice_test_endpoint_calls_and_logs_without_advancing_case(client, session, make_customer, make_invoice):
    customer = make_customer(name="Voice API Call Co", phone="+919876512400")
    invoice = make_invoice(customer=customer, invoice_no="VAPI-2", outstanding=10_000.0)
    case = Case(invoice_id=invoice.id, customer_id=customer.id, bucket="0-15")
    session.add(case)
    session.commit()

    resp = client.post(f"/api/cases/{case.id}/voice-test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["to"] == "+919876512400"
    assert data["status"] == "logged"  # no real Twilio creds in tests

    session.refresh(case)
    assert case.playbook_name is None
    assert case.level_index == 0


def test_case_detail_api_before_any_dispatch_has_no_last_decision(client, session, make_invoice):
    invoice = make_invoice(outstanding=5_000.0, invoice_no="ND-1")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    detail = client.get(f"/api/cases/{case.id}").json()
    assert detail["last_decision"] is None
    assert detail["level"] is None
    assert detail["promise"] is None


def test_case_detail_api_surfaces_a_pending_promise(client, session, make_customer, make_invoice):
    customer = make_customer(name="Promise Detail Co")
    invoice = make_invoice(customer=customer, invoice_no="PD-1", outstanding=5_000.0)
    case = Case(invoice_id=invoice.id, customer_id=customer.id, bucket="0-15")
    session.add(case)
    session.commit()

    resp = client.post(f"/api/cases/{case.id}/promise", json={"promised_date": (dt.date.today() + dt.timedelta(days=7)).isoformat()})
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"

    detail = client.get(f"/api/cases/{case.id}").json()
    assert detail["promise"]["status"] == "pending"
    assert detail["promise"]["source"] == "manual"


def test_log_promise_rejects_past_date(client, session, make_invoice):
    invoice = make_invoice(outstanding=5_000.0, invoice_no="PD-2")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    resp = client.post(f"/api/cases/{case.id}/promise", json={"promised_date": (dt.date.today() - dt.timedelta(days=1)).isoformat()})
    assert resp.status_code == 400


def test_log_promise_rejects_implausibly_far_future(client, session, make_invoice):
    invoice = make_invoice(outstanding=5_000.0, invoice_no="PD-3")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    resp = client.post(f"/api/cases/{case.id}/promise", json={"promised_date": (dt.date.today() + dt.timedelta(days=400)).isoformat()})
    assert resp.status_code == 400


def test_log_promise_rejects_malformed_date(client, session, make_invoice):
    invoice = make_invoice(outstanding=5_000.0, invoice_no="PD-4")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    resp = client.post(f"/api/cases/{case.id}/promise", json={"promised_date": "not-a-date"})
    assert resp.status_code == 400


def test_chat_page_shows_not_configured_warning(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = client.get("/chat")
    assert resp.status_code == 200
    assert "Not configured" in resp.text


def test_chat_page_lists_settings_tile(client):
    resp = client.get("/settings")
    assert "Chatbot" in resp.text


def test_api_chat_delegates_to_agent_and_updates_state(client, monkeypatch):
    import app.portal.routes as routes_module

    class FakeResult:
        reply = "Acme owes ₹10,000."
        messages = [{"role": "user", "content": "what does acme owe"}]
        pending_action = None

    calls = []

    def fake_chat(session, messages, pending_action, user_text):
        calls.append((messages, pending_action, user_text))
        return FakeResult()

    monkeypatch.setattr(routes_module.chatbot_agent, "chat", fake_chat)

    resp = client.post("/api/chat", json={"message": "what does acme owe"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["reply"] == "Acme owes ₹10,000."
    assert data["pending_confirmation"] is False
    assert len(calls) == 1
    assert calls[0][2] == "what does acme owe"

    # state persisted for the next turn
    assert routes_module._CHAT_STATE["messages"] == FakeResult.messages
    assert len(routes_module._CHAT_STATE["display_log"]) == 2


def test_api_chat_reports_pending_confirmation(client, monkeypatch):
    import app.portal.routes as routes_module

    class FakeResult:
        reply = "About to generate a link. Confirm?"
        messages = []
        pending_action = {"name": "generate_payment_link", "args": {"invoice_no": "INV-1"}}

    monkeypatch.setattr(routes_module.chatbot_agent, "chat", lambda *a, **k: FakeResult())

    resp = client.post("/api/chat", json={"message": "link INV-1"})
    data = resp.json()
    assert data["pending_confirmation"] is True
    assert routes_module._CHAT_STATE["pending_action"] == FakeResult.pending_action


def test_api_chat_reset_clears_state(client, monkeypatch):
    import app.portal.routes as routes_module

    routes_module._CHAT_STATE["messages"] = [{"role": "user", "content": "hi"}]
    routes_module._CHAT_STATE["pending_action"] = {"name": "x", "args": {}}
    routes_module._CHAT_STATE["display_log"] = [{"role": "user", "text": "hi"}]

    resp = client.post("/api/chat/reset")
    assert resp.status_code == 200
    assert routes_module._CHAT_STATE["messages"] == []
    assert routes_module._CHAT_STATE["pending_action"] is None
    assert routes_module._CHAT_STATE["display_log"] == []


def test_case_detail_api_flags_disputed(client, session, make_customer, make_invoice):
    from app.cases.engine import flag_dispute, sync_cases

    customer = make_customer(name="Dispute Route Co")
    make_invoice(customer=customer, invoice_no="DRT-1", outstanding=10_000.0)
    sync_cases(session)
    case = session.query(Case).one()

    flag_dispute(session, customer, "wrong invoice")

    detail = client.get(f"/api/cases/{case.id}").json()
    assert detail["status"] == "paused"
    assert detail["disputed"] is True


def test_case_detail_api_not_disputed_for_ordinary_pause(client, session, make_invoice):
    from app.cases.engine import pause_case

    invoice = make_invoice(outstanding=10_000.0, invoice_no="ORD-RT-1")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()
    pause_case(session, case, reason="max_touch_cap_reached")
    session.commit()

    detail = client.get(f"/api/cases/{case.id}").json()
    assert detail["disputed"] is False


def test_reopen_case_endpoint(client, session, make_invoice):
    from app.cases.engine import pause_case

    invoice = make_invoice(outstanding=10_000.0, invoice_no="REOPEN-RT-1")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()
    pause_case(session, case, reason="disputed")
    session.commit()

    resp = client.post(f"/api/cases/{case.id}/reopen")
    assert resp.status_code == 200
    session.refresh(case)
    assert case.status == "open"


def test_reopen_case_endpoint_from_exhausted(client, session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="REOPEN-RT-3")
    case = Case(
        invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="90+",
        status="exhausted", close_reason="exhausted", level_index=3, playbook_name="receivables_escalation",
    )
    session.add(case)
    session.commit()

    resp = client.post(f"/api/cases/{case.id}/reopen")
    assert resp.status_code == 200
    session.refresh(case)
    assert case.status == "open"
    assert case.level_index == 0
    assert case.close_reason is None


def test_reopen_case_endpoint_rejects_non_paused_case(client, session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="REOPEN-RT-2")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    resp = client.post(f"/api/cases/{case.id}/reopen")
    assert resp.status_code == 400


def test_cases_page_has_export_link(client):
    resp = client.get("/cases")
    assert "/api/cases/export.csv" in resp.text


def test_export_csv_headers(client):
    resp = client.get("/api/cases/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert "cases_" in resp.headers["content-disposition"]


def test_export_csv_empty_state_has_only_header_row(client):
    resp = client.get("/api/cases/export.csv")
    content = resp.content.decode("utf-8-sig")
    lines = [line for line in content.strip().split("\r\n") if line]
    assert len(lines) == 1
    assert "Case ID" in lines[0]
    assert "Customer" in lines[0]


def test_export_csv_includes_case_data(client, session, make_customer, make_invoice):
    customer = make_customer(name="Export Test Co")
    invoice = make_invoice(customer=customer, invoice_no="EXP-1", outstanding=12_345.67, due_date=dt.date(2026, 9, 1))
    case = Case(invoice_id=invoice.id, customer_id=customer.id, bucket="0-15", status="open", touch_count=2)
    session.add(case)
    session.commit()

    resp = client.get("/api/cases/export.csv")
    content = resp.content.decode("utf-8-sig")
    assert "Export Test Co" in content
    assert "EXP-1" in content
    assert "12345.67" in content
    assert "01-Sep-2026" in content


def test_export_csv_reflects_disputed_flag(client, session, make_customer, make_invoice):
    from app.cases.engine import flag_dispute, sync_cases

    customer = make_customer(name="Export Dispute Co")
    make_invoice(customer=customer, invoice_no="EXP-2", outstanding=10_000.0)
    sync_cases(session)
    flag_dispute(session, customer, "wrong amount")

    resp = client.get("/api/cases/export.csv")
    content = resp.content.decode("utf-8-sig")
    row = next(line for line in content.split("\r\n") if "EXP-2" in line)
    assert row.split(",")[7] == "yes"  # Disputed column


def test_export_csv_non_disputed_case_says_no(client, session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="EXP-3")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    resp = client.get("/api/cases/export.csv")
    content = resp.content.decode("utf-8-sig")
    row = next(line for line in content.split("\r\n") if "EXP-3" in line)
    assert row.split(",")[7] == "no"


def test_needs_review_tab_shows_only_paused_cases(client, session, make_customer, make_invoice):
    from app.cases.engine import flag_dispute, sync_cases

    disputed_customer = make_customer(name="Review Disputed Co")
    make_invoice(customer=disputed_customer, invoice_no="REV-1", outstanding=10_000.0)
    open_customer = make_customer(name="Review Open Co")
    make_invoice(customer=open_customer, invoice_no="REV-2", outstanding=10_000.0)
    sync_cases(session)
    flag_dispute(session, disputed_customer, "wrong invoice")

    resp = client.get("/cases?view=needs_review")
    assert resp.status_code == 200
    assert "Review Disputed Co" in resp.text
    assert "Review Open Co" not in resp.text
    assert "wrong invoice" in resp.text


def test_needs_review_tab_empty_state(client, make_invoice):
    make_invoice(outstanding=10_000.0, invoice_no="REV-3")  # open, not paused
    resp = client.get("/cases?view=needs_review")
    assert "Nothing needs review" in resp.text


def test_needs_review_count_shown_in_all_cases_view(client, session, make_customer, make_invoice):
    from app.cases.engine import flag_dispute, sync_cases

    customer = make_customer(name="Review Count Co")
    make_invoice(customer=customer, invoice_no="REV-4", outstanding=10_000.0)
    sync_cases(session)
    flag_dispute(session, customer, "reason")

    resp = client.get("/cases")
    assert "Needs Review (1)" in resp.text


def test_all_cases_view_shows_disputed_badge(client, session, make_customer, make_invoice):
    from app.cases.engine import flag_dispute, sync_cases

    customer = make_customer(name="Review Badge Co")
    make_invoice(customer=customer, invoice_no="REV-5", outstanding=10_000.0)
    sync_cases(session)
    flag_dispute(session, customer, "reason")

    resp = client.get("/cases")
    assert "pill-disputed" in resp.text
    assert "disputed" in resp.text


def test_reopen_via_list_removes_case_from_review_tab(client, session, make_customer, make_invoice):
    from app.cases.engine import flag_dispute, sync_cases

    customer = make_customer(name="Review Reopen Co")
    make_invoice(customer=customer, invoice_no="REV-6", outstanding=10_000.0)
    sync_cases(session)
    flag_dispute(session, customer, "reason")
    case = session.query(Case).filter(Case.invoice.has(invoice_no="REV-6")).one()

    resp = client.post(f"/api/cases/{case.id}/reopen")
    assert resp.status_code == 200

    resp2 = client.get("/cases?view=needs_review")
    assert "Review Reopen Co" not in resp2.text


def test_manual_dispute_endpoint_pauses_customers_open_cases(client, session, make_customer, make_invoice):
    from app.cases.engine import sync_cases

    customer = make_customer(name="Manual Dispute Co")
    make_invoice(customer=customer, invoice_no="MD-1", outstanding=10_000.0)
    make_invoice(customer=customer, invoice_no="MD-2", outstanding=20_000.0)
    sync_cases(session)
    case = session.query(Case).filter(Case.invoice.has(invoice_no="MD-1")).one()

    resp = client.post(f"/api/cases/{case.id}/dispute", json={"reason": "already paid via bank transfer"})
    assert resp.status_code == 200
    assert resp.json()["cases_paused"] == 2  # both of this customer's open cases

    session.refresh(case)
    assert case.status == "paused"


def test_manual_dispute_endpoint_without_reason(client, session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="MD-3")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    resp = client.post(f"/api/cases/{case.id}/dispute", json={})
    assert resp.status_code == 200
    session.refresh(case)
    assert case.status == "paused"


def test_manual_dispute_endpoint_rejects_non_open_case(client, session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="MD-4")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", status="closed", close_reason="paid")
    session.add(case)
    session.commit()

    resp = client.post(f"/api/cases/{case.id}/dispute", json={"reason": "wrong"})
    assert resp.status_code == 400

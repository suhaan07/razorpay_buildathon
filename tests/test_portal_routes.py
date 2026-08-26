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


def _xlsx_bytes(rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    return buf.getvalue()


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

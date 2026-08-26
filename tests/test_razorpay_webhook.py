import json

import pytest
from fastapi.testclient import TestClient

import app.webhooks.razorpay_webhook as webhook_module
from app.data.models import Case
from app.db import get_session
from app.main import app as fastapi_app


@pytest.fixture()
def notify_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        webhook_module,
        "notify_payment_received",
        lambda **kwargs: calls.append(kwargs),
    )
    return calls


@pytest.fixture()
def client(session):
    fastapi_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(fastapi_app)  # no lifespan -> skips create_all() against the real dev DB
    fastapi_app.dependency_overrides.clear()


def _webhook_body(event: str, link_id: str, payment_id: str = "pay_1") -> dict:
    return {
        "event": event,
        "payload": {
            "payment_link": {"entity": {"id": link_id}},
            "payment": {"entity": {"id": payment_id}},
        },
    }


def test_payment_captured_closes_matching_case(client, session, make_invoice, notify_calls):
    invoice = make_invoice(outstanding=5_000.0, inv_amount=5_000.0, invoice_no="WH-1")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", pay_link_id="plink_abc")
    session.add(case)
    session.commit()

    resp = client.post("/webhooks/razorpay", content=json.dumps(_webhook_body("payment_link.paid", "plink_abc")))
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"

    session.refresh(case)
    session.refresh(invoice)
    assert case.status == "closed"
    assert case.close_reason == "paid"
    assert invoice.outstanding == 0.0

    assert len(notify_calls) == 1
    assert notify_calls[0]["total_amount"] == 5_000.0
    assert notify_calls[0]["invoice_numbers"] == ["WH-1"]


def test_notification_goes_to_whoever_last_queried_this_customer(client, session, make_customer, make_invoice, notify_calls):
    customer = make_customer(name="Queried Before Paying Co")
    customer.last_whatsapp_query_from = "whatsapp:+919999900001"
    invoice = make_invoice(customer=customer, outstanding=5_000.0, inv_amount=5_000.0, invoice_no="WH-Q1")
    case = Case(invoice_id=invoice.id, customer_id=customer.id, bucket="0-15", pay_link_id="plink_q1")
    session.add(case)
    session.commit()

    resp = client.post("/webhooks/razorpay", content=json.dumps(_webhook_body("payment_link.paid", "plink_q1")))
    assert resp.status_code == 200

    assert notify_calls[0]["to_number"] == "whatsapp:+919999900001"


def test_notification_has_no_to_number_when_nobody_has_queried_yet(client, session, make_invoice, notify_calls):
    invoice = make_invoice(outstanding=5_000.0, inv_amount=5_000.0, invoice_no="WH-Q2")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", pay_link_id="plink_q2")
    session.add(case)
    session.commit()

    resp = client.post("/webhooks/razorpay", content=json.dumps(_webhook_body("payment_link.paid", "plink_q2")))
    assert resp.status_code == 200

    assert notify_calls[0]["to_number"] is None  # notify_payment_received falls back to PAYMENT_NOTIFY_WHATSAPP_TO itself


def test_redelivered_event_for_closed_case_is_idempotent(client, session, make_invoice, notify_calls):
    invoice = make_invoice(outstanding=5_000.0, inv_amount=5_000.0, invoice_no="WH-2")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", pay_link_id="plink_dup", status="closed", close_reason="paid")
    session.add(case)
    session.commit()

    resp = client.post("/webhooks/razorpay", content=json.dumps(_webhook_body("payment_link.paid", "plink_dup")))
    assert resp.status_code == 200
    assert resp.json()["reason"] == "case already terminal (idempotent)"
    assert notify_calls == []  # redelivery must never re-notify


def test_unknown_payment_link_is_ignored_not_errored(client):
    resp = client.post("/webhooks/razorpay", content=json.dumps(_webhook_body("payment_link.paid", "plink_nonexistent")))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_bad_signature_rejected_when_secret_configured(client, monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "test-secret")
    resp = client.post(
        "/webhooks/razorpay",
        content=json.dumps(_webhook_body("payment_link.paid", "plink_x")),
        headers={"x-razorpay-signature": "not-a-real-signature"},
    )
    assert resp.status_code == 400


def test_consolidated_payment_closes_every_open_case_for_that_customer(client, session, make_customer, make_invoice, notify_calls):
    customer = make_customer(name="Pays It All Co")
    inv1 = make_invoice(customer=customer, outstanding=10_000.0, inv_amount=10_000.0, invoice_no="ALLW-1")
    inv2 = make_invoice(customer=customer, outstanding=20_000.0, inv_amount=20_000.0, invoice_no="ALLW-2")
    case1 = Case(invoice_id=inv1.id, customer_id=customer.id, bucket="0-15", pay_link_id="plink_case1")
    case2 = Case(invoice_id=inv2.id, customer_id=customer.id, bucket="0-15", pay_link_id="plink_case2")
    session.add_all([case1, case2])
    session.commit()

    body = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"id": "plink_consolidated", "notes": {"kind": "consolidated_payoff", "customer_id": str(customer.id)}}},
            "payment": {"entity": {"id": "pay_all_1"}},
        },
    }
    resp = client.post("/webhooks/razorpay", content=json.dumps(body))
    assert resp.status_code == 200
    data = resp.json()
    assert data["consolidated"] is True
    assert set(data["case_ids"]) == {case1.id, case2.id}

    session.refresh(case1)
    session.refresh(case2)
    assert case1.status == "closed" and case1.close_reason == "paid"
    assert case2.status == "closed" and case2.close_reason == "paid"

    # one combined alert for the whole payoff, not one per case
    assert len(notify_calls) == 1
    assert notify_calls[0]["customer_name"] == "Pays It All Co"
    assert notify_calls[0]["total_amount"] == 30_000.0
    assert set(notify_calls[0]["invoice_numbers"]) == {"ALLW-1", "ALLW-2"}


def test_redelivered_consolidated_payment_is_idempotent(client, session, make_customer, make_invoice, notify_calls):
    customer = make_customer(name="Already Paid All Co")
    inv = make_invoice(customer=customer, outstanding=0.0, inv_amount=10_000.0, invoice_no="ALLW-3")
    case = Case(invoice_id=inv.id, customer_id=customer.id, bucket="0-15", status="closed", close_reason="paid")
    session.add(case)
    session.commit()

    body = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"id": "plink_consolidated_2", "notes": {"kind": "consolidated_payoff", "customer_id": str(customer.id)}}},
            "payment": {"entity": {"id": "pay_all_2"}},
        },
    }
    resp = client.post("/webhooks/razorpay", content=json.dumps(body))
    assert resp.status_code == 200
    assert resp.json()["case_ids"] == []  # nothing left open — naturally idempotent
    assert notify_calls == []


def test_payment_failed_does_not_notify(client, session, make_invoice, notify_calls):
    invoice = make_invoice(outstanding=5_000.0, inv_amount=5_000.0, invoice_no="WH-FAIL")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", pay_link_id="plink_fail")
    session.add(case)
    session.commit()

    resp = client.post("/webhooks/razorpay", content=json.dumps(_webhook_body("payment.failed", "plink_fail")))
    assert resp.status_code == 200
    assert notify_calls == []

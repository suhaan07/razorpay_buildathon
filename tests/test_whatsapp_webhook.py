import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app as fastapi_app


@pytest.fixture()
def client(session):
    fastapi_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(fastapi_app)
    fastapi_app.dependency_overrides.clear()


def _ask(client, body: str) -> str:
    resp = client.post("/whatsapp/webhook", data={"Body": body, "From": "whatsapp:+919876500001"})
    assert resp.status_code == 200
    return resp.text


def test_query_records_the_asking_number_on_the_customer(client, session, make_customer, make_invoice):
    customer = make_customer(name="Remembered Query Co")
    make_invoice(customer=customer, invoice_no="RM-1", outstanding=25_000.0)

    _ask(client, "Give me a weekly payment schedule for Remembered Query Co")

    session.refresh(customer)
    assert customer.last_whatsapp_query_from == "whatsapp:+919876500001"


def test_later_query_from_a_different_number_overwrites_the_earlier_one(client, session, make_customer, make_invoice):
    customer = make_customer(name="Reassigned Query Co")
    make_invoice(customer=customer, invoice_no="RA-1", outstanding=25_000.0)

    _ask(client, "Give me a weekly payment schedule for Reassigned Query Co")

    resp = client.post(
        "/whatsapp/webhook",
        data={"Body": "Give me a weekly payment schedule for Reassigned Query Co", "From": "whatsapp:+919876500002"},
    )
    assert resp.status_code == 200

    session.refresh(customer)
    assert customer.last_whatsapp_query_from == "whatsapp:+919876500002"


def test_no_match_or_ambiguous_query_does_not_touch_any_customer_record(client, session, make_customer, make_invoice):
    c1 = make_customer(name="Ambiguous Query One")
    c2 = make_customer(name="Ambiguous Query Two")
    make_invoice(customer=c1, invoice_no="AQ-1")
    make_invoice(customer=c2, invoice_no="AQ-2")

    _ask(client, "Give me a weekly payment schedule for Ambiguous Query")

    session.refresh(c1)
    session.refresh(c2)
    assert c1.last_whatsapp_query_from is None
    assert c2.last_whatsapp_query_from is None


def test_payment_schedule_query(client, make_customer, make_invoice):
    customer = make_customer(name="Gamma Technologies Private Limited")
    make_invoice(customer=customer, invoice_no="G-1", outstanding=25_000.0)

    body = _ask(client, "Give me a weekly payment schedule for Gamma Technologies")
    assert "Weekly Payment Schedule" in body
    assert "Gamma Technologies" in body
    assert "Pay now:" in body  # anyone querying gets a way to pay right in the reply


def test_query_reuses_the_same_link_as_a_prior_query(client, make_customer, make_invoice):
    customer = make_customer(name="Repeat Query Co")
    make_invoice(customer=customer, invoice_no="RQ-1", outstanding=10_000.0)

    first = _ask(client, "Give me a weekly payment schedule for Repeat Query Co")
    second = _ask(client, "Give me a weekly collection follow-up for Repeat Query Co")

    def extract_link(text):
        return next(line for line in text.split("\n") if line.startswith("Pay now:"))

    assert extract_link(first) == extract_link(second)  # cached, not a fresh link every query


def test_no_pay_link_when_nothing_owed(client, make_customer, make_invoice):
    customer = make_customer(name="All Paid Co")
    make_invoice(customer=customer, invoice_no="AP-1", outstanding=0.0)

    body = _ask(client, "Give me a weekly payment schedule for All Paid Co")
    assert "Pay now:" not in body


def test_oversized_amount_shown_honestly_not_as_a_dead_link(client, make_customer, make_invoice, monkeypatch):
    # regression test: a real Razorpay "amount exceeds maximum" rejection
    # degrades to a stub link (razorpay_client.create_payment_link) — that
    # stub must never be shown as a clickable "Pay now" link to the person
    # who just asked for one, since clicking it does nothing.
    customer = make_customer(name="Too Big To Link Co")
    make_invoice(customer=customer, invoice_no="BIG-1", outstanding=50_000.0)

    import app.billing as billing_module

    def fake_create_payment_link(**kwargs):
        return {"id": "stub_oversized_ALL-1-abc123", "short_url": "https://rzp.io/l/stub_oversized_ALL-1-abc123", "stub": True}

    monkeypatch.setattr(billing_module, "create_payment_link", fake_create_payment_link)

    body = _ask(client, "Give me a weekly payment schedule for Too Big To Link Co")
    assert "Pay now:" not in body
    assert "stub_oversized" not in body
    assert "exceeds what we can auto-generate" in body


def test_collection_followup_query(client, make_customer, make_invoice):
    customer = make_customer(name="Gamma Technologies Private Limited")
    make_invoice(customer=customer, invoice_no="G-2", outstanding=25_000.0)

    body = _ask(client, "Give me a weekly collection follow-up for Gamma Technologies")
    assert "Weekly Collection Follow-up" in body


def test_customer_not_found_gets_graceful_reply(client):
    body = _ask(client, "Give me a weekly payment schedule for Nonexistent Corp")
    assert "couldn't find" in body


def test_ambiguous_name_lists_candidates(client, make_customer, make_invoice):
    c1 = make_customer(name="Kumar Enterprises Mumbai")
    c2 = make_customer(name="Kumar Enterprises Delhi")
    make_invoice(customer=c1, invoice_no="K-1")
    make_invoice(customer=c2, invoice_no="K-2")

    body = _ask(client, "Give me a weekly payment schedule for Kumar Enterprises")
    assert "more than one account" in body
    assert "Kumar Enterprises Mumbai" in body
    assert "Kumar Enterprises Delhi" in body


def test_unparseable_message_gets_usage_hint(client):
    body = _ask(client, "hello there")
    assert "payment schedule" in body.lower()


def test_internal_error_still_returns_valid_twiml_not_a_500(client, make_customer, make_invoice, monkeypatch):
    # regression test: any bug in report building must still produce a
    # WhatsApp-visible reply, not a silent 500 — a 500 has no TwiML body,
    # so Twilio has nothing to relay and the sender sees nothing at all.
    customer = make_customer(name="Broken Formatter Co")
    make_invoice(customer=customer, invoice_no="BRK-1", outstanding=1_000.0)

    import app.webhooks.whatsapp_webhook as webhook_module

    def boom(data):
        raise RuntimeError("simulated bug in the formatter")

    monkeypatch.setitem(webhook_module._FORMATTERS, "payment_schedule", boom)

    resp = client.post("/whatsapp/webhook", data={"Body": "Give me a weekly payment schedule for Broken Formatter Co", "From": "whatsapp:+919876500001"})
    assert resp.status_code == 200
    assert "<Response>" in resp.text
    assert "went wrong" in resp.text


def test_customer_name_with_ampersand_does_not_break_the_xml_reply(client, make_customer, make_invoice):
    customer = make_customer(name="Smith & Sons Pvt Ltd")
    make_invoice(customer=customer, invoice_no="SS-1", outstanding=5_000.0)

    resp = client.post("/whatsapp/webhook", data={"Body": "Give me a weekly payment schedule for Smith & Sons", "From": "whatsapp:+919876500001"})
    assert resp.status_code == 200
    assert "&amp;" in resp.text  # properly escaped, not a raw & that would break XML parsing

    import xml.etree.ElementTree as ET

    ET.fromstring(resp.text)  # raises if the reply isn't well-formed XML

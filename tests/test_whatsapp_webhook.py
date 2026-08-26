import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.cases.engine import sync_cases
from app.data.models import Case, PromiseToPay
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


def test_promise_to_pay_happy_path(client, session, make_customer, make_invoice):
    customer = make_customer(name="Promise WA Co")
    make_invoice(customer=customer, invoice_no="PWA-1", outstanding=10_000.0)

    body = _ask(client, "Promise to pay for Promise WA Co by tomorrow")
    assert "Promise WA Co" in body
    assert "check back" in body.lower()

    promise = session.query(PromiseToPay).filter(PromiseToPay.customer_id == customer.id).one()
    assert promise.status == "pending"
    assert promise.source == "whatsapp"
    assert promise.promised_date == dt.date.today() + dt.timedelta(days=1)


def test_promise_to_pay_sets_last_whatsapp_query_from(client, session, make_customer, make_invoice):
    customer = make_customer(name="Promise Query From Co")
    make_invoice(customer=customer, invoice_no="PWA-2", outstanding=10_000.0)

    resp = client.post("/whatsapp/webhook", data={"Body": "Promise to pay for Promise Query From Co by tomorrow", "From": "whatsapp:+919999900002"})
    assert resp.status_code == 200

    session.refresh(customer)
    assert customer.last_whatsapp_query_from == "whatsapp:+919999900002"


def test_promise_to_pay_missing_by_clause_gets_format_hint(client, make_customer, make_invoice):
    make_customer(name="Acme Pvt Ltd")
    body = _ask(client, "Promise to pay for Acme Pvt Ltd")
    assert "use the format" in body.lower()


def test_promise_to_pay_customer_name_containing_the_word_by_still_fails_gracefully(client, make_customer):
    # known limitation: a customer name that itself contains " by " (rare —
    # none of the real customer names in this app do) can confuse the
    # for/by split when there's no real date clause at all. It must still
    # degrade to a clear, actionable error, never a crash or silent 500.
    make_customer(name="Missing By Co")
    body = _ask(client, "Promise to pay for Missing By Co")
    assert "<Response>" in body
    assert "couldn't understand the date" in body


def test_promise_to_pay_customer_not_found(client):
    body = _ask(client, "Promise to pay for Nonexistent Corp by tomorrow")
    assert "couldn't find" in body


def test_promise_to_pay_ambiguous_customer(client, make_customer, make_invoice):
    c1 = make_customer(name="Ambiguous Promise Mumbai")
    c2 = make_customer(name="Ambiguous Promise Delhi")
    make_invoice(customer=c1, invoice_no="AP-1")
    make_invoice(customer=c2, invoice_no="AP-2")

    body = _ask(client, "Promise to pay for Ambiguous Promise by tomorrow")
    assert "more than one account" in body


def test_promise_to_pay_unparseable_date(client, make_customer, make_invoice):
    customer = make_customer(name="Bad Date Co")
    make_invoice(customer=customer, invoice_no="BD-1", outstanding=10_000.0)

    body = _ask(client, "Promise to pay for Bad Date Co by whenever")
    assert "couldn't understand the date" in body


def test_promise_to_pay_nothing_outstanding(client, make_customer, make_invoice):
    customer = make_customer(name="All Clear Promise Co")
    make_invoice(customer=customer, invoice_no="AC-1", outstanding=0.0)

    body = _ask(client, "Promise to pay for All Clear Promise Co by tomorrow")
    assert "nothing outstanding" in body.lower()


def test_promise_to_pay_customer_name_with_ampersand_stays_valid_xml(client, make_customer, make_invoice):
    customer = make_customer(name="Smith & Sons Pvt Ltd")
    make_invoice(customer=customer, invoice_no="SS-2", outstanding=5_000.0)

    resp = client.post("/whatsapp/webhook", data={"Body": "Promise to pay for Smith & Sons by tomorrow", "From": "whatsapp:+919876500001"})
    assert resp.status_code == 200

    import xml.etree.ElementTree as ET
    ET.fromstring(resp.text)


def test_usage_hint_mentions_promise_command(client):
    body = _ask(client, "hello there")
    assert "promise to pay" in body.lower()


def test_usage_hint_mentions_dispute_command(client):
    body = _ask(client, "hello there")
    assert "dispute" in body.lower()


def test_dispute_happy_path_pauses_the_case(client, session, make_customer, make_invoice):
    customer = make_customer(name="Dispute WA Co")
    invoice = make_invoice(customer=customer, invoice_no="DWA-1", outstanding=10_000.0)
    sync_cases(session)
    case = session.query(Case).filter(Case.invoice_id == invoice.id).one()

    body = _ask(client, "Dispute for Dispute WA Co: already paid via bank transfer")
    assert "Dispute WA Co" in body
    assert "paused" in body.lower()
    assert "already paid via bank transfer" in body

    session.refresh(case)
    assert case.status == "paused"


def test_dispute_without_reason(client, session, make_customer, make_invoice):
    customer = make_customer(name="Dispute No Reason Co")
    make_invoice(customer=customer, invoice_no="DNR-1", outstanding=10_000.0)
    sync_cases(session)

    body = _ask(client, "Dispute for Dispute No Reason Co")
    assert "paused" in body.lower()
    assert "Reason noted" not in body


def test_dispute_sets_last_whatsapp_query_from(client, session, make_customer, make_invoice):
    customer = make_customer(name="Dispute Query From Co")
    make_invoice(customer=customer, invoice_no="DQF-1", outstanding=10_000.0)
    sync_cases(session)

    resp = client.post("/whatsapp/webhook", data={"Body": "Dispute for Dispute Query From Co: wrong amount", "From": "whatsapp:+919999900003"})
    assert resp.status_code == 200
    session.refresh(customer)
    assert customer.last_whatsapp_query_from == "whatsapp:+919999900003"


def test_dispute_missing_customer_gets_format_hint(client):
    body = _ask(client, "Dispute please help")
    assert "use the format" in body.lower()


def test_dispute_customer_not_found(client):
    body = _ask(client, "Dispute for Nonexistent Corp: wrong")
    assert "couldn't find" in body


def test_dispute_ambiguous_customer(client, make_customer, make_invoice):
    c1 = make_customer(name="Ambiguous Dispute Mumbai")
    c2 = make_customer(name="Ambiguous Dispute Delhi")
    make_invoice(customer=c1, invoice_no="ADI-1")
    make_invoice(customer=c2, invoice_no="ADI-2")

    body = _ask(client, "Dispute for Ambiguous Dispute: wrong")
    assert "more than one account" in body


def test_dispute_no_open_cases(client, make_customer, make_invoice):
    customer = make_customer(name="Dispute Nothing Owed Co")
    make_invoice(customer=customer, invoice_no="DNO-1", outstanding=0.0)

    body = _ask(client, "Dispute for Dispute Nothing Owed Co: wrong")
    assert "no active case" in body.lower()


def test_dispute_customer_name_with_ampersand_stays_valid_xml(client, session, make_customer, make_invoice):
    customer = make_customer(name="Bright & Sons Pvt Ltd")
    make_invoice(customer=customer, invoice_no="BS-1", outstanding=5_000.0)
    sync_cases(session)

    resp = client.post("/whatsapp/webhook", data={"Body": "Dispute for Bright & Sons: wrong", "From": "whatsapp:+919876500001"})
    assert resp.status_code == 200

    import xml.etree.ElementTree as ET
    ET.fromstring(resp.text)

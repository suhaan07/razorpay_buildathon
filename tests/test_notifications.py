from __future__ import annotations

import app.notifications as notifications_module
from app.notifications import notify_payment_received, send_ops_digest


def test_noop_when_recipient_not_configured(monkeypatch):
    monkeypatch.delenv("PAYMENT_NOTIFY_WHATSAPP_TO", raising=False)
    # no Twilio client access at all — a stray call would raise since
    # credentials are stripped in tests, so reaching get_client() would fail
    notify_payment_received(customer_name="Acme", total_amount=1000.0, invoice_numbers=["INV-1"])


def test_noop_when_twilio_not_configured(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "+919876500001")
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    notify_payment_received(customer_name="Acme", total_amount=1000.0, invoice_numbers=["INV-1"])


def test_noop_when_invoice_list_is_empty(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "+919876500001")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake_token")

    class ExplodingClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise AssertionError("should never be called for an empty invoice list")

    monkeypatch.setattr(notifications_module.twilio_client, "get_client", lambda: ExplodingClient())
    notify_payment_received(customer_name="Acme", total_amount=0.0, invoice_numbers=[])


def test_sends_formatted_message_with_correct_recipient_and_body(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "+919876500001")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake_token")
    monkeypatch.delenv("TWILIO_WHATSAPP_FROM", raising=False)

    sent = {}

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)

    monkeypatch.setattr(notifications_module.twilio_client, "get_client", lambda: FakeClient())

    notify_payment_received(customer_name="Beta Logistics", total_amount=458172.39, invoice_numbers=["INV-1011", "INV-1012", "INV-1013"])

    assert sent["to"] == "whatsapp:+919876500001"
    assert sent["from_"] == "whatsapp:+14155238886"
    assert "Beta Logistics" in sent["body"]
    assert "₹4,58,172.39" in sent["body"]
    assert "3 invoices" in sent["body"]
    assert "INV-1011, INV-1012, INV-1013" in sent["body"]


def test_singular_wording_for_one_invoice(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "+919876500001")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake_token")

    sent = {}

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)

    monkeypatch.setattr(notifications_module.twilio_client, "get_client", lambda: FakeClient())

    notify_payment_received(customer_name="Acme", total_amount=1000.0, invoice_numbers=["INV-1"])
    assert "1 invoice:" in sent["body"]
    assert "invoices" not in sent["body"]


def test_respects_custom_from_number(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "919876500001")  # no leading +, no prefix
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake_token")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "+15550001111")

    sent = {}

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)

    monkeypatch.setattr(notifications_module.twilio_client, "get_client", lambda: FakeClient())

    notify_payment_received(customer_name="Acme", total_amount=1000.0, invoice_numbers=["INV-1"])
    assert sent["from_"] == "whatsapp:+15550001111"
    assert sent["to"] == "whatsapp:919876500001"


def test_to_number_takes_priority_over_env_fallback(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "+919876500001")  # fallback — should be ignored
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake_token")

    sent = {}

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)

    monkeypatch.setattr(notifications_module.twilio_client, "get_client", lambda: FakeClient())

    notify_payment_received(
        customer_name="Acme",
        total_amount=1000.0,
        invoice_numbers=["INV-1"],
        to_number="whatsapp:+917428551996",
    )
    assert sent["to"] == "whatsapp:+917428551996"


def test_noop_when_neither_query_number_nor_fallback_is_available(monkeypatch):
    monkeypatch.delenv("PAYMENT_NOTIFY_WHATSAPP_TO", raising=False)
    notify_payment_received(customer_name="Acme", total_amount=1000.0, invoice_numbers=["INV-1"], to_number=None)


def test_send_failure_is_swallowed_not_raised(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "+919876500001")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake_token")

    class FailingClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("simulated Twilio outage")

    monkeypatch.setattr(notifications_module.twilio_client, "get_client", lambda: FailingClient())

    # must not raise — a notification outage can never take down payment processing
    notify_payment_received(customer_name="Acme", total_amount=1000.0, invoice_numbers=["INV-1"])


def test_digest_noop_when_nothing_to_report(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "+919876500001")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake_token")

    class ExplodingClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise AssertionError("should never be called when there's nothing new to report")

    monkeypatch.setattr(notifications_module.twilio_client, "get_client", lambda: ExplodingClient())
    send_ops_digest(broken_promise_customer_names=[], exhausted_case_summaries=[])


def test_digest_noop_when_recipient_not_configured(monkeypatch):
    monkeypatch.delenv("PAYMENT_NOTIFY_WHATSAPP_TO", raising=False)
    send_ops_digest(broken_promise_customer_names=["Acme"], exhausted_case_summaries=[])


def test_digest_includes_broken_promises_only(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "+919876500001")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake_token")

    sent = {}

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)

    monkeypatch.setattr(notifications_module.twilio_client, "get_client", lambda: FakeClient())

    send_ops_digest(broken_promise_customer_names=["Acme Pvt Ltd", "Beta Co"], exhausted_case_summaries=[])
    assert "Broken promises (2)" in sent["body"]
    assert "Acme Pvt Ltd" in sent["body"] and "Beta Co" in sent["body"]
    assert "Exhausted" not in sent["body"]


def test_digest_singular_wording_for_one_broken_promise(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "+919876500001")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake_token")

    sent = {}

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)

    monkeypatch.setattr(notifications_module.twilio_client, "get_client", lambda: FakeClient())

    send_ops_digest(broken_promise_customer_names=["Acme"], exhausted_case_summaries=[])
    assert "Broken promise (1)" in sent["body"]
    assert "promises" not in sent["body"]


def test_digest_includes_exhausted_cases_only(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "+919876500001")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake_token")

    sent = {}

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)

    monkeypatch.setattr(notifications_module.twilio_client, "get_client", lambda: FakeClient())

    send_ops_digest(broken_promise_customer_names=[], exhausted_case_summaries=["Gamma Co (INV-99)"])
    assert "Exhausted, unpaid despite full chain (1 case)" in sent["body"]
    assert "Gamma Co (INV-99)" in sent["body"]
    assert "Broken" not in sent["body"]


def test_digest_includes_both_sections_when_both_present(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "+919876500001")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake_token")

    sent = {}

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)

    monkeypatch.setattr(notifications_module.twilio_client, "get_client", lambda: FakeClient())

    send_ops_digest(broken_promise_customer_names=["Acme"], exhausted_case_summaries=["Gamma Co (INV-99)", "Delta Co (INV-100)"])
    assert "Broken promise (1)" in sent["body"]
    assert "Exhausted, unpaid despite full chain (2 cases)" in sent["body"]


def test_digest_send_failure_is_swallowed(monkeypatch):
    monkeypatch.setenv("PAYMENT_NOTIFY_WHATSAPP_TO", "+919876500001")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake_token")

    class FailingClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("simulated outage")

    monkeypatch.setattr(notifications_module.twilio_client, "get_client", lambda: FailingClient())
    send_ops_digest(broken_promise_customer_names=["Acme"], exhausted_case_summaries=[])  # must not raise

from app.channels.base import ChannelResult
from app.channels.email_channel import EmailChannel
from app.channels.log_channel import LogChannel
from app.channels.registry import get_channel
from app.channels.voice import VoiceChannel


def test_log_channel_always_logged():
    result = LogChannel().send(to="+91123", cc=None, subject="s", body="b")
    assert result.status == "logged"


def test_voice_falls_back_to_log_without_credentials(monkeypatch):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    result = VoiceChannel().send(to="+911234567890", cc=None, subject="s", body="hello")
    assert result.status == "logged"


def test_email_falls_back_to_log_without_credentials(monkeypatch):
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    result = EmailChannel().send(to="a@b.com", cc=None, subject="s", body="hello")
    assert result.status == "logged"


def test_registry_returns_every_declared_channel():
    for name in ("email", "voice", "log"):
        assert get_channel(name).name == name


def test_email_test_override_redirects_and_notes_original_recipient(monkeypatch):
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", "tester@example.com")

    captured = {}

    def fake_send(self, *, to, cc, subject, body, html=None):
        captured.update(to=to, cc=cc, subject=subject, body=body)
        return ChannelResult(status="logged", detail="ok")

    monkeypatch.setattr(LogChannel, "send", fake_send)

    EmailChannel().send(to="spoc@customer.com", cc="manager@customer.com", subject="Invoice X", body="hi")

    assert captured["to"] == "tester@example.com"
    assert captured["cc"] is None
    assert captured["subject"] == "Invoice X"  # subject stays clean — an address list there reads as spam
    assert "spoc@customer.com" in captured["body"]
    assert "manager@customer.com" in captured["body"]


def test_email_without_override_sends_to_real_recipient(monkeypatch):
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("TEST_EMAIL_OVERRIDE", raising=False)

    result = EmailChannel().send(to="spoc@customer.com", cc=None, subject="Invoice X", body="hi")
    assert "spoc@customer.com" in result.detail


def test_sendgrid_path_builds_a_serializable_message(monkeypatch):
    # regression test: the credential-stripping autouse fixture means every
    # other email test exercises the LogChannel fallback, never the real
    # SendGrid code path — which is exactly how a broken tracking_settings
    # assignment (a raw dict instead of the SDK's own TrackingSettings
    # object) shipped without a single test catching it. This test mocks
    # only the network call, not the message construction, so a similarly
    # malformed Mail object fails here instead of in production.
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.fake-key-for-testing")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("TEST_EMAIL_OVERRIDE", raising=False)

    captured = {}

    class FakeResponse:
        status_code = 202

    class FakeSendGridAPIClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key

        def send(self, message):
            captured["message_dict"] = message.get()  # raises the same way the real SDK does if malformed
            return FakeResponse()

    import sendgrid

    monkeypatch.setattr(sendgrid, "SendGridAPIClient", FakeSendGridAPIClient)

    result = EmailChannel().send(to="spoc@customer.com", cc="manager@customer.com", subject="Invoice X", body="hi", html="<p>hi</p>")

    assert result.status == "sent"
    assert captured["message_dict"]["tracking_settings"] == {
        "click_tracking": {"enable": False, "enable_text": False},
        "open_tracking": {"enable": False},
    }


def test_sendgrid_path_works_without_html_too(monkeypatch):
    # the "Test a channel" button and any plain-text-only send never build
    # an HTML body — html=None must not be a special case that breaks.
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.fake-key-for-testing")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("TEST_EMAIL_OVERRIDE", raising=False)

    class FakeResponse:
        status_code = 202

    class FakeSendGridAPIClient:
        def __init__(self, api_key):
            pass

        def send(self, message):
            message.get()  # must not raise
            return FakeResponse()

    import sendgrid

    monkeypatch.setattr(sendgrid, "SendGridAPIClient", FakeSendGridAPIClient)

    result = EmailChannel().send(to="spoc@customer.com", cc=None, subject="Test", body="hi")
    assert result.status == "sent"

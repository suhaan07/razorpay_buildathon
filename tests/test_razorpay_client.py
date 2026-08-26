from app.integrations import razorpay_client


def test_create_payment_link_stub_when_not_configured(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    link = razorpay_client.create_payment_link(amount_rupees=1000.0, invoice_no="INV-STUB", customer_name="Acme", description="d")
    assert link["stub"] is True
    assert link["id"] == "stub_INV-STUB"


def test_create_payment_link_retries_on_duplicate_reference_id(monkeypatch):
    # regression test: a stray payment link from earlier testing (or a case
    # that legitimately already has one) must not permanently block new
    # dispatches — create_payment_link() retries once with a disambiguated
    # reference_id instead of propagating Razorpay's "already exists" error.
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")

    calls = []

    class FakePaymentLinkResource:
        def create(self, payload):
            calls.append(dict(payload))
            if len(calls) == 1:
                raise Exception(
                    "BadRequestError: payment link with given reference_id: INV-DUP already "
                    "exists. Please create a payment link with a different reference_id"
                )
            return {"id": "plink_retry", "short_url": "https://rzp.io/l/retry"}

    class FakeClient:
        payment_link = FakePaymentLinkResource()

    monkeypatch.setattr(razorpay_client, "get_client", lambda: FakeClient())

    link = razorpay_client.create_payment_link(amount_rupees=500.0, invoice_no="INV-DUP", customer_name="Acme", description="d")

    assert len(calls) == 2
    assert calls[0]["reference_id"] == "INV-DUP"
    assert calls[1]["reference_id"] != "INV-DUP"
    assert calls[1]["reference_id"].startswith("INV-DUP-")
    assert link["id"] == "plink_retry"


def test_create_payment_link_falls_back_to_stub_when_amount_exceeds_account_max(monkeypatch):
    # regression test: this account's real Razorpay error for INV-1104
    # ("amount exceeds maximum amount allowed") must degrade to a stub link
    # rather than failing the whole dispatch — a large invoice shouldn't
    # block its case from getting its escalation email/call at all.
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")

    class FakePaymentLinkResource:
        def create(self, payload):
            raise Exception("BadRequestError: amount exceeds maximum amount allowed.")

    class FakeClient:
        payment_link = FakePaymentLinkResource()

    monkeypatch.setattr(razorpay_client, "get_client", lambda: FakeClient())

    link = razorpay_client.create_payment_link(amount_rupees=839_799.36, invoice_no="INV-1104", customer_name="Pi Plastics Ltd", description="d")

    assert link["stub"] is True
    assert link["id"] == "stub_oversized_INV-1104"


def test_create_payment_link_falls_back_immediately_on_test_mode_quota(monkeypatch):
    # regression test: "test mode limit of 30 reached for payment_link" is a
    # fixed, non-time-based cap on the whole account. An earlier version of
    # this code retried it with backoff, which — measured against the real
    # account — just burned minutes per case for a guaranteed second
    # failure. Must degrade to a stub immediately, on the first attempt.
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")

    calls = []

    class FakePaymentLinkResource:
        def create(self, payload):
            calls.append(dict(payload))
            raise Exception("ServerError: test mode limit of 30 reached for payment_link")

    class FakeClient:
        payment_link = FakePaymentLinkResource()

    monkeypatch.setattr(razorpay_client, "get_client", lambda: FakeClient())

    link = razorpay_client.create_payment_link(amount_rupees=5_000.0, invoice_no="INV-QUOTA", customer_name="Acme", description="d")

    assert len(calls) == 1  # no retry at all
    assert link["stub"] is True
    assert link["id"] == "stub_oversized_INV-QUOTA"


def test_create_payment_link_falls_back_immediately_on_too_many_requests(monkeypatch):
    # regression test: observed directly against the real account, "Too many
    # requests" turned out to be the SAME exhausted-quota condition under a
    # different message, not a short burst that clears if you wait — so this
    # degrades exactly like the quota case above, not with a retry.
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")

    calls = []

    class FakePaymentLinkResource:
        def create(self, payload):
            calls.append(dict(payload))
            raise Exception("BadRequestError: Too many requests")

    class FakeClient:
        payment_link = FakePaymentLinkResource()

    monkeypatch.setattr(razorpay_client, "get_client", lambda: FakeClient())

    link = razorpay_client.create_payment_link(amount_rupees=500.0, invoice_no="INV-RL", customer_name="Acme", description="d")

    assert len(calls) == 1
    assert link["stub"] is True
    assert link["id"] == "stub_oversized_INV-RL"


def test_create_payment_link_reraises_unrelated_errors(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")

    class FakePaymentLinkResource:
        def create(self, payload):
            raise Exception("AuthenticationError: invalid api key")

    class FakeClient:
        payment_link = FakePaymentLinkResource()

    monkeypatch.setattr(razorpay_client, "get_client", lambda: FakeClient())

    try:
        razorpay_client.create_payment_link(amount_rupees=500.0, invoice_no="INV-AUTH", customer_name="Acme", description="d")
        assert False, "expected an exception to propagate"
    except Exception as exc:
        assert "AuthenticationError" in str(exc)

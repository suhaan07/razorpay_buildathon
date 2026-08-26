import datetime as dt

from app.cases.engine import (
    due_cases,
    force_dispatch_case,
    get_settings,
    preview_next_message,
    preview_voice_call,
    run_batch,
    send_voice_call_test,
    set_case_level,
    sync_cases,
)
from app.data.models import Case, Settings

# 10:00 UTC = 15:30 IST — comfortably outside the default 21:00-09:00 IST
# quiet-hours window, so these tests don't flake depending on what time of
# day they run. Pinned to today's date (matching make_invoice's due_date,
# which is relative to dt.date.today()) rather than a hardcoded calendar date.
FIXED_NOW = dt.datetime.combine(dt.date.today(), dt.time(10, 0))


def test_sync_cases_creates_one_case_per_outstanding_invoice(session, make_invoice):
    make_invoice(outstanding=50_000.0, due_date=dt.date.today() - dt.timedelta(days=20))
    touched = sync_cases(session)
    assert touched == 1
    case = session.query(Case).one()
    assert case.bucket == "16-30"
    assert case.status == "open"


def test_sync_cases_closes_case_when_invoice_fully_paid(session, make_invoice):
    invoice = make_invoice(outstanding=0.0, due_date=dt.date.today() - dt.timedelta(days=5))
    sync_cases(session)
    assert session.query(Case).count() == 0  # never overdue-with-balance, no case needed
    invoice.outstanding = 100.0
    sync_cases(session)
    case = session.query(Case).one()
    invoice.outstanding = 0.0
    sync_cases(session)
    session.refresh(case)
    assert case.status == "closed"
    assert case.close_reason == "paid"


def test_get_settings_creates_a_default_row_paused_by_default(session):
    # regression test: a fresh Settings row must default to paused — an
    # unattended run_batch() can mint real Razorpay payment links against a
    # limited test-account quota, so requiring explicit opt-in is the safe
    # default (see app/data/models.py::Settings).
    session.query(Settings).delete()
    session.commit()
    settings = get_settings(session)
    assert settings.auto_dispatch_paused is True


def test_run_batch_does_nothing_while_auto_dispatch_paused(session, make_invoice):
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="PAUSED-1")
    get_settings(session).auto_dispatch_paused = True
    session.commit()

    summary = run_batch(session, now=FIXED_NOW)

    assert summary["auto_dispatch_paused"] is True
    assert summary["dispatched"] == 0
    case = session.query(Case).one()
    assert case.playbook_name is None  # sync_cases still ran (case exists), nothing was ever dispatched


def test_run_batch_still_syncs_cases_and_scores_while_paused(session, make_invoice):
    # sync_cases()/refresh_all_reliability_scores() never touch Razorpay or
    # send anything, so they should still run even while dispatch is paused
    # — only the actual send loop is gated.
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="PAUSED-2")
    get_settings(session).auto_dispatch_paused = True
    session.commit()

    run_batch(session, now=FIXED_NOW)

    assert session.query(Case).count() == 1  # sync_cases created it despite the pause


def test_force_dispatch_case_ignores_the_pause(session, make_invoice):
    # a human explicitly clicking "Send now (test)" on one case is a
    # deliberate action, not the automatic sweep the pause guards against.
    invoice = make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="PAUSED-3")
    sync_cases(session, today=FIXED_NOW.date())
    case = session.query(Case).one()
    get_settings(session).auto_dispatch_paused = True
    session.commit()

    outcome = force_dispatch_case(session, case, now=FIXED_NOW)

    assert outcome == "dispatched"
    session.refresh(case)
    assert case.playbook_name is not None


def test_first_dispatch_starts_at_spoc_for_early_bucket(session, make_invoice):
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="EARLY-1")
    summary = run_batch(session, now=FIXED_NOW)
    assert summary["dispatched"] == 1
    assert summary["failed"] == 0
    case = session.query(Case).one()
    assert case.playbook_name == "receivables_escalation"
    assert case.level_index == 0  # spoc
    assert case.next_action_at is not None


def test_first_dispatch_jumps_to_skip_level_for_very_late_bucket(session, make_invoice):
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=95), invoice_no="LATE-1")
    run_batch(session, now=FIXED_NOW)
    case = session.query(Case).one()
    assert case.level_index == 2  # skip_level, never jumps straight to voice (3)


def test_case_advances_through_the_chain_when_wait_elapses(session, make_invoice):
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="ADV-1")
    now = FIXED_NOW
    run_batch(session, now=now)
    case = session.query(Case).one()
    level_after_first = case.level_index

    for _ in range(3):
        now += dt.timedelta(days=6)  # comfortably past any wait_days (max 5)
        run_batch(session, now=now)
        session.refresh(case)

    assert case.level_index >= level_after_first  # monotonic — never regresses


def test_case_reaches_voice_only_after_skip_level_and_then_exhausts(session, make_invoice):
    # a chronically-late, high-value case should reach skip_level fast, then
    # voice, then exhaust — never skipping straight to voice from an earlier level
    make_invoice(outstanding=900_000.0, due_date=dt.date.today() - dt.timedelta(days=95), invoice_no="EXH-1")
    now = FIXED_NOW
    run_batch(session, now=now)
    case = session.query(Case).one()
    assert case.level_index == 2  # starts at skip_level, not voice

    now += dt.timedelta(days=6)
    run_batch(session, now=now)
    session.refresh(case)
    assert case.level_index == 3  # advanced to voice mechanically
    assert case.status == "open"

    now += dt.timedelta(days=6)
    run_batch(session, now=now)
    session.refresh(case)
    assert case.status == "exhausted"
    assert case.close_reason == "exhausted"


def test_max_touch_cap_pauses_case(session, make_invoice, monkeypatch):
    monkeypatch.setenv("MAX_TOUCH_CAP", "1")
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="CAP-1")
    now = FIXED_NOW
    run_batch(session, now=now)  # touch_count -> 1
    now += dt.timedelta(days=6)
    summary = run_batch(session, now=now)  # cap already reached, should pause instead of dispatching
    assert summary["paused"] == 1
    case = session.query(Case).one()
    assert case.status == "paused"


def test_due_cases_only_returns_open_cases_past_next_action(session, make_invoice):
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="DUE-1")
    now = FIXED_NOW
    run_batch(session, now=now)
    assert due_cases(session, now) == []  # just dispatched, not due again yet
    later = now + dt.timedelta(days=10)
    assert len(due_cases(session, later)) == 1


def test_one_failing_case_does_not_sink_the_rest_of_the_batch(session, make_invoice, monkeypatch):
    # regression test: a real external-API failure on one case (e.g. Razorpay
    # erroring out on create_payment_link) must not roll back every other
    # case's progress in the same run — see the "dispatch_error" handling
    # in run_batch().
    good = make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="OK-1")
    bad = make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="BOOM-1")

    import app.cases.engine as engine_module

    real_create_link = engine_module.create_payment_link

    def flaky_create_link(*, invoice_no, **kwargs):
        if invoice_no == "BOOM-1":
            raise RuntimeError("simulated Razorpay outage")
        return real_create_link(invoice_no=invoice_no, **kwargs)

    monkeypatch.setattr(engine_module, "create_payment_link", flaky_create_link)

    summary = run_batch(session, now=FIXED_NOW)
    assert summary["dispatched"] == 1
    assert summary["failed"] == 1

    good_case = session.query(Case).filter(Case.invoice_id == good.id).one()
    bad_case = session.query(Case).filter(Case.invoice_id == bad.id).one()
    assert good_case.playbook_name is not None  # unaffected by the other case's failure
    assert bad_case.playbook_name is None  # never got past the failed dispatch
    assert any((e.payload or {}).get("reason") == "dispatch_error" for e in bad_case.events)


def test_preview_does_not_mutate_case_state(session, make_invoice):
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="PREVIEW-1")
    sync_cases(session)
    case = session.query(Case).one()
    events_before = len(case.events)  # sync_cases() itself logs a "case_created" event

    preview = preview_next_message(session, case, now=FIXED_NOW)
    assert preview["would"] == "dispatch"
    assert preview["level_name"] == "spoc"
    assert "[a real Razorpay payment link" in preview["body"]

    # nothing about the case itself changed
    session.refresh(case)
    assert case.playbook_name is None
    assert case.touch_count == 0
    assert len(case.events) == events_before


def test_force_dispatch_sends_immediately_bypassing_next_action_at(session, make_invoice):
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="FORCE-1")
    run_batch(session, now=FIXED_NOW)  # first dispatch sets a future next_action_at
    case = session.query(Case).one()
    level_after_first = case.level_index
    assert case.next_action_at > FIXED_NOW  # not due again yet

    outcome = force_dispatch_case(session, case, now=FIXED_NOW + dt.timedelta(minutes=1))
    assert outcome == "dispatched"
    session.refresh(case)
    assert case.level_index > level_after_first  # advanced despite not being "due"


def test_preview_voice_call_uses_real_case_data_regardless_of_level(session, make_customer, make_invoice):
    # voice is normally only reached after spoc/manager/skip_level are all
    # exhausted — this must render the real voice script for THIS case even
    # though it's still sitting at level 0 (or hasn't been dispatched at all).
    customer = make_customer(name="Voice Preview Co", phone="+919876512345")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=45), invoice_no="VOICE-1")
    sync_cases(session)
    case = session.query(Case).one()
    assert case.playbook_name is None  # never dispatched

    preview = preview_voice_call(session, case, today=dt.date.today())
    assert preview["to"] == "+919876512345"
    assert "Voice Preview Co" in preview["body"]
    assert "VOICE-1" in preview["body"]

    session.refresh(case)
    assert case.playbook_name is None  # read-only — untouched
    assert len(case.events) == 1  # only sync_cases' case_created event


def test_send_voice_call_test_does_not_advance_the_case(session, make_customer, make_invoice):
    customer = make_customer(name="Voice Call Co", phone="+919876512346")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=10), invoice_no="VOICE-2")
    sync_cases(session)
    case = session.query(Case).one()

    result = send_voice_call_test(session, case, today=dt.date.today())
    assert result["to"] == "+919876512346"
    assert result["status"] in ("logged", "sent")  # "logged" since no real Twilio creds in tests

    session.refresh(case)
    assert case.playbook_name is None  # a test call is not a real escalation touch
    assert case.level_index == 0
    assert case.touch_count == 0
    assert case.next_action_at is None
    assert any(e.type == "dispatch" and e.channel == "voice" and (e.payload or {}).get("test") is True for e in case.events)


def test_set_case_level_updates_index_and_playbook(session, make_invoice):
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="LEVEL-1")
    sync_cases(session)
    case = session.query(Case).one()
    assert case.playbook_name is None

    set_case_level(session, case, "skip_level")

    session.refresh(case)
    assert case.playbook_name is not None
    assert case.level_index == 2
    assert any((e.payload or {}).get("reason") == "level_override" and e.payload.get("to") == "skip_level" for e in case.events)


def test_set_case_level_then_send_now_advances_to_voice(session, make_invoice):
    # the whole point of setting a case to "skip_level": the next real
    # dispatch mechanically advances one rung further, landing on voice.
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="LEVEL-2")
    sync_cases(session)
    case = session.query(Case).one()
    set_case_level(session, case, "skip_level")

    outcome = force_dispatch_case(session, case, now=FIXED_NOW)
    assert outcome == "dispatched"
    session.refresh(case)
    assert case.level_index == 3  # voice


def test_set_case_level_to_voice_then_send_now_marks_exhausted(session, make_invoice):
    # setting straight to "voice" means the case looks like it already had
    # its final call — the next dispatch attempt correctly treats that as
    # exhausted rather than placing a second voice call silently.
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="LEVEL-3")
    sync_cases(session)
    case = session.query(Case).one()
    set_case_level(session, case, "voice")

    outcome = force_dispatch_case(session, case, now=FIXED_NOW)
    assert outcome == "exhausted"


def test_set_case_level_rejects_unknown_level(session, make_invoice):
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="LEVEL-4")
    sync_cases(session)
    case = session.query(Case).one()
    try:
        set_case_level(session, case, "not_a_real_level")
        assert False, "expected a ValueError"
    except ValueError:
        pass


def test_preview_reflects_test_email_override(session, make_invoice, monkeypatch):
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", "tester@example.com")
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="OVERRIDE-1")
    sync_cases(session)
    case = session.query(Case).one()

    preview = preview_next_message(session, case, now=FIXED_NOW)
    assert preview["to"] == "tester@example.com"
    assert preview["cc"] is None
    assert preview["original_to"] is not None
    assert "@ourcompany.example.com" in preview["original_to"]


def test_force_dispatch_still_respects_max_touch_cap(session, make_invoice, monkeypatch):
    monkeypatch.setenv("MAX_TOUCH_CAP", "1")
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="CAPFORCE-1")
    run_batch(session, now=FIXED_NOW)
    case = session.query(Case).one()

    outcome = force_dispatch_case(session, case, now=FIXED_NOW + dt.timedelta(minutes=1))
    assert outcome == "paused"
    session.refresh(case)
    assert case.status == "paused"


def test_payment_link_is_reused_across_levels_not_recreated(session, make_invoice, monkeypatch):
    # regression test: Razorpay's reference_id (the invoice number) must be
    # unique per account, so calling create_payment_link() again for a case
    # that already has one always fails — the case's existing pay_link_id
    # must be reused on every subsequent dispatch, not recreated.
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="REUSE-1")

    import app.cases.engine as engine_module

    calls = []
    real_create_link = engine_module.create_payment_link

    def counting_create_link(**kwargs):
        calls.append(kwargs["invoice_no"])
        return real_create_link(**kwargs)

    monkeypatch.setattr(engine_module, "create_payment_link", counting_create_link)

    now = FIXED_NOW
    run_batch(session, now=now)  # level 0, creates the link
    case = session.query(Case).one()
    first_link = case.pay_link_url

    now += dt.timedelta(days=6)
    run_batch(session, now=now)  # level 1, should reuse it
    session.refresh(case)

    assert len(calls) == 1  # create_payment_link only ever called once
    assert case.pay_link_url == first_link


def test_consolidated_pay_all_link_only_created_with_multiple_open_invoices(session, make_customer, make_invoice):
    customer = make_customer(name="Single Invoice Co")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="SINGLE-1")

    run_batch(session, now=FIXED_NOW)
    case = session.query(Case).one()
    dispatch_events = [e for e in case.events if e.type == "dispatch"]
    assert dispatch_events[0].payload["status"] in ("logged", "sent")
    assert not any(e.payload and "consolidated_pay_link_id" in (e.payload or {}) for e in case.events)


def test_consolidated_pay_all_link_created_with_multiple_open_invoices(session, make_customer, make_invoice):
    customer = make_customer(name="Multi Invoice Co")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="MULTI-1")
    make_invoice(customer=customer, outstanding=20_000.0, due_date=dt.date.today() - dt.timedelta(days=8), invoice_no="MULTI-2")

    run_batch(session, now=FIXED_NOW)
    case = session.query(Case).filter(Case.invoice.has(invoice_no="MULTI-1")).one()
    assert any((e.payload or {}).get("consolidated_pay_link_id") for e in case.events)


def test_preview_shows_invoice_table_and_pay_all_flag(session, make_customer, make_invoice):
    customer = make_customer(name="Preview Multi Co")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="PMULTI-1")
    make_invoice(customer=customer, outstanding=20_000.0, due_date=dt.date.today() - dt.timedelta(days=8), invoice_no="PMULTI-2")
    sync_cases(session)
    case = session.query(Case).filter(Case.invoice.has(invoice_no="PMULTI-1")).one()

    preview = preview_next_message(session, case, now=FIXED_NOW)
    assert preview["would_include_pay_all"] is True
    assert {row["invoice_no"] for row in preview["invoice_rows"]} == {"PMULTI-1", "PMULTI-2"}
    # preview must not create a real consolidated link — no new events beyond case creation
    assert not any((e.payload or {}).get("consolidated_pay_link_id") for e in case.events)


def test_render_sample_message_for_voice():
    from app.cases.engine import render_sample_message

    sample = render_sample_message("voice")
    assert sample["channel"] == "voice"
    assert sample["recipients_role"] == "customer"
    assert "Sample Customer Pvt Ltd" in sample["body"]
    assert "45" in sample["body"]


def test_render_sample_message_for_email():
    from app.cases.engine import render_sample_message

    sample = render_sample_message("email")
    assert sample["channel"] == "email"
    assert sample["recipients_role"] == "spoc"


def test_render_sample_message_unknown_channel_raises():
    from app.cases.engine import render_sample_message

    try:
        render_sample_message("carrier_pigeon")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_oversized_primary_link_not_shown_as_dead_button_in_email(session, make_customer, make_invoice, monkeypatch):
    # regression test: same issue as the WhatsApp bot — an oversized-stub
    # payment link must not be rendered as a clickable button in the
    # escalation email, nor substituted into the plain-text {{pay_link}}
    # token as if it were real.
    customer = make_customer(name="Oversized Invoice Co")
    make_invoice(customer=customer, outstanding=50_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="OVR-1")

    import app.cases.engine as engine_module

    def fake_create_payment_link(**kwargs):
        ref = kwargs.get("invoice_no", "x")
        return {"id": f"stub_oversized_{ref}", "short_url": f"https://rzp.io/l/stub_oversized_{ref}", "stub": True}

    monkeypatch.setattr(engine_module, "create_payment_link", fake_create_payment_link)

    from app.channels.base import ChannelResult
    import app.channels.log_channel as log_channel_module

    captured = {}

    def fake_send(self, *, to, cc, subject, body, html=None):
        captured.update(body=body, html=html)
        return ChannelResult(status="logged", detail="ok")

    monkeypatch.setattr(log_channel_module.LogChannel, "send", fake_send)

    run_batch(session, now=FIXED_NOW)

    assert "payment link unavailable" in captured["body"]
    assert "stub_oversized" not in captured["body"]
    assert captured["html"] is not None
    assert "href=" not in captured["html"]  # no dead button rendered

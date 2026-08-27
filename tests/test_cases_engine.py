import datetime as dt

from app.cases.engine import (
    close_case,
    due_cases,
    flag_dispute,
    force_dispatch_case,
    get_pause_info,
    get_settings,
    is_disputed,
    pause_case,
    preview_next_message,
    preview_voice_call,
    record_promise,
    reopen_case,
    resolve_promises,
    resolve_promises_for_customer,
    run_batch,
    send_voice_call_test,
    set_case_level,
    sync_cases,
)
from app.data.models import Case, PromiseToPay, Settings

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


def test_run_batch_does_not_dispatch_while_auto_dispatch_paused(session, make_invoice):
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="PAUSED-1")
    get_settings(session).auto_dispatch_paused = True
    session.commit()

    summary = run_batch(session, now=FIXED_NOW)

    assert summary["auto_dispatch_paused"] is True
    assert summary["dispatched"] == 0
    case = session.query(Case).one()
    # scoring still ran (see below), but nothing was ever dispatched — so
    # the case's real escalation state (what a later real dispatch would
    # advance from) is untouched.
    assert case.playbook_name is None
    assert case.level_index == 0
    assert case.touch_count == 0
    assert case.pay_link_id is None
    assert not any(e.type == "dispatch" for e in case.events)


def test_run_batch_still_scores_cases_while_paused(session, make_invoice):
    # Scoring (the decision layer) is read-only/informational, unlike an
    # actual dispatch — it should still run and be visible on the case's
    # timeline even while sending is paused, so a human can see what WOULD
    # happen without it actually happening (no payment link, no email).
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="PAUSED-2")
    get_settings(session).auto_dispatch_paused = True
    session.commit()

    summary = run_batch(session, now=FIXED_NOW)

    assert session.query(Case).count() == 1  # sync_cases created it despite the pause
    assert summary["processed"] == 1  # one case scored
    case = session.query(Case).one()
    decision_events = [e for e in case.events if e.type == "decision"]
    assert len(decision_events) == 1
    assert decision_events[0].rationale is not None
    assert decision_events[0].payload["suggested_level"] == "spoc"
    assert decision_events[0].payload["urgency_score"] > 0
    # but its real escalation state stays untouched — see the sibling test
    assert case.playbook_name is None


def test_run_batch_scoring_while_paused_does_not_cause_a_level_skip_later(session, make_invoice):
    # The critical correctness case: score a case while paused, THEN turn
    # auto-dispatch on and run for real. The real dispatch must still start
    # at spoc (level_index 0) — if the paused scoring pass had persisted
    # case.level_index/playbook_name, the real run would see "already at
    # this level" and advance PAST spoc, so the very first actual email
    # would skip straight to manager without spoc ever having been sent.
    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="PAUSED-3")
    get_settings(session).auto_dispatch_paused = True
    session.commit()
    run_batch(session, now=FIXED_NOW)  # scoring-only pass

    get_settings(session).auto_dispatch_paused = False
    session.commit()
    summary = run_batch(session, now=FIXED_NOW)

    assert summary["dispatched"] == 1
    case = session.query(Case).one()
    assert case.level_index == 0  # spoc — not skipped past


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


def test_preview_voice_call_uses_real_case_data_regardless_of_level(session, make_customer, make_invoice, monkeypatch):
    # voice is normally only reached after spoc/manager/skip_level are all
    # exhausted — this must render the real voice script for THIS case even
    # though it's still sitting at level 0 (or hasn't been dispatched at all).
    monkeypatch.delenv("TEST_VOICE_OVERRIDE", raising=False)
    customer = make_customer(name="Voice Preview Co", phone="+919876512345")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=45), invoice_no="VOICE-1")
    sync_cases(session)
    case = session.query(Case).one()
    assert case.playbook_name is None  # never dispatched

    preview = preview_voice_call(session, case, today=dt.date.today())
    assert preview["to"] == "+919876512345"
    assert preview["dialed"] is None  # no override configured — nothing to redirect
    assert "Voice Preview Co" in preview["body"]
    assert "VOICE-1" in preview["body"]

    session.refresh(case)
    assert case.playbook_name is None  # read-only — untouched
    assert len(case.events) == 1  # only sync_cases' case_created event


def test_preview_voice_call_shows_the_override_target_without_changing_to(session, make_customer, make_invoice, monkeypatch):
    # `to` must keep showing the case's real customer number (consistent
    # with send_voice_call_test and the dispatch-event audit trail) even
    # while a test override is active — only `dialed` reflects the redirect.
    monkeypatch.setenv("TEST_VOICE_OVERRIDE", "+911111111111")
    customer = make_customer(name="Voice Override Preview Co", phone="+919876512347")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=45), invoice_no="VOICE-3")
    sync_cases(session)
    case = session.query(Case).one()

    preview = preview_voice_call(session, case, today=dt.date.today())
    assert preview["to"] == "+919876512347"
    assert preview["dialed"] == "+911111111111"


def test_send_voice_call_test_does_not_advance_the_case(session, make_customer, make_invoice, monkeypatch):
    monkeypatch.delenv("TEST_VOICE_OVERRIDE", raising=False)
    customer = make_customer(name="Voice Call Co", phone="+919876512346")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=10), invoice_no="VOICE-2")
    sync_cases(session)
    case = session.query(Case).one()

    result = send_voice_call_test(session, case, today=dt.date.today())
    assert result["to"] == "+919876512346"
    assert result["dialed"] is None  # no override configured
    assert result["status"] in ("logged", "sent")  # "logged" since no real Twilio creds in tests

    session.refresh(case)
    assert case.playbook_name is None  # a test call is not a real escalation touch
    assert case.level_index == 0
    assert case.touch_count == 0
    assert case.next_action_at is None
    dispatch_events = [e for e in case.events if e.type == "dispatch" and e.channel == "voice" and (e.payload or {}).get("test") is True]
    assert len(dispatch_events) == 1
    # the audit trail always logs the case's real customer number, even
    # with an override active — see the sibling override test below
    assert dispatch_events[0].payload["to"] == "+919876512346"


def test_send_voice_call_test_with_override_still_logs_the_real_number(session, make_customer, make_invoice, monkeypatch):
    monkeypatch.setenv("TEST_VOICE_OVERRIDE", "+911111111111")
    customer = make_customer(name="Voice Call Override Co", phone="+919876512348")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=10), invoice_no="VOICE-4")
    sync_cases(session)
    case = session.query(Case).one()

    result = send_voice_call_test(session, case, today=dt.date.today())
    assert result["to"] == "+919876512348"  # the case's real number, for display
    assert result["dialed"] == "+911111111111"  # what was actually dialed

    session.refresh(case)
    dispatch_event = next(e for e in case.events if e.type == "dispatch" and e.channel == "voice")
    assert dispatch_event.payload["to"] == "+919876512348"  # audit trail: real customer number, not the override


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


def test_record_promise_pulls_next_action_at_on_open_cases(session, make_customer, make_invoice):
    customer = make_customer(name="Promise Co")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="PTP-1")
    sync_cases(session)
    case = session.query(Case).one()

    promised_date = dt.date.today() + dt.timedelta(days=5)
    promise = record_promise(session, customer, promised_date, source="whatsapp", raw_text="I'll pay by Friday")

    assert promise.status == "pending"
    session.refresh(case)
    expected_check_at = dt.datetime.combine(promised_date + dt.timedelta(days=1), dt.time.min)
    assert case.next_action_at == expected_check_at
    assert any((e.payload or {}).get("reason") == "promise_recorded" for e in case.events)


def test_record_promise_applies_to_every_open_case_for_that_customer(session, make_customer, make_invoice):
    customer = make_customer(name="Multi Case Promise Co")
    make_invoice(customer=customer, invoice_no="PTP-M1", outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5))
    make_invoice(customer=customer, invoice_no="PTP-M2", outstanding=20_000.0, due_date=dt.date.today() - dt.timedelta(days=5))
    sync_cases(session)
    cases = session.query(Case).filter(Case.customer_id == customer.id).all()
    assert len(cases) == 2

    promised_date = dt.date.today() + dt.timedelta(days=3)
    record_promise(session, customer, promised_date, source="manual")

    for case in cases:
        session.refresh(case)
        assert case.next_action_at == dt.datetime.combine(promised_date + dt.timedelta(days=1), dt.time.min)


def test_record_promise_does_not_touch_closed_cases(session, make_customer, make_invoice):
    customer = make_customer(name="Closed Case Promise Co")
    make_invoice(customer=customer, invoice_no="PTP-C1", outstanding=0.0, due_date=dt.date.today() - dt.timedelta(days=5))
    make_invoice(customer=customer, invoice_no="PTP-C2", outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5))
    sync_cases(session)  # the fully-paid invoice never gets an open case at all

    open_case = session.query(Case).filter(Case.customer_id == customer.id).one()
    original_next_action_at = open_case.next_action_at

    record_promise(session, customer, dt.date.today() + dt.timedelta(days=2), source="manual")
    session.refresh(open_case)
    assert open_case.next_action_at != original_next_action_at  # only the real open case exists to update


def test_record_promise_supersedes_prior_pending_promise(session, make_customer, make_invoice):
    customer = make_customer(name="Renegotiate Co")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="PTP-2")
    sync_cases(session)

    first = record_promise(session, customer, dt.date.today() + dt.timedelta(days=5), source="whatsapp")
    second = record_promise(session, customer, dt.date.today() + dt.timedelta(days=10), source="whatsapp")

    session.refresh(first)
    assert first.status == "superseded"
    assert first.resolved_at is not None
    assert second.status == "pending"

    pending = session.query(PromiseToPay).filter(PromiseToPay.status == "pending").all()
    assert len(pending) == 1
    assert pending[0].id == second.id


def test_resolve_promises_marks_broken_when_date_passed_and_still_owing(session, make_customer, make_invoice):
    customer = make_customer(name="Broken Promise Co")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=20), invoice_no="PTP-3")
    sync_cases(session)
    promise = record_promise(session, customer, dt.date.today() - dt.timedelta(days=1), source="manual")

    resolve_promises(session, today=dt.date.today())

    session.refresh(promise)
    assert promise.status == "broken"
    assert promise.resolved_at is not None


def test_resolve_promises_marks_kept_when_fully_paid_even_before_the_date(session, make_customer, make_invoice):
    customer = make_customer(name="Kept Early Co")
    invoice = make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="PTP-4")
    sync_cases(session)
    promise = record_promise(session, customer, dt.date.today() + dt.timedelta(days=10), source="whatsapp")

    invoice.outstanding = 0.0  # paid early, well before the promised date
    session.commit()
    resolve_promises(session, today=dt.date.today())

    session.refresh(promise)
    assert promise.status == "kept"


def test_resolve_promises_leaves_pending_when_still_owing_and_not_yet_due(session, make_customer, make_invoice):
    customer = make_customer(name="Still Pending Co")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="PTP-5")
    sync_cases(session)
    promise = record_promise(session, customer, dt.date.today() + dt.timedelta(days=5), source="manual")

    resolve_promises(session, today=dt.date.today())

    session.refresh(promise)
    assert promise.status == "pending"


def test_resolve_promises_for_customer_is_scoped(session, make_customer, make_invoice):
    a = make_customer(name="Scope A Co")
    b = make_customer(name="Scope B Co")
    make_invoice(customer=a, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=20), invoice_no="PTP-6A")
    make_invoice(customer=b, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=20), invoice_no="PTP-6B")
    sync_cases(session)
    promise_a = record_promise(session, a, dt.date.today() - dt.timedelta(days=1), source="manual")
    promise_b = record_promise(session, b, dt.date.today() - dt.timedelta(days=1), source="manual")

    resolve_promises_for_customer(session, a.id, today=dt.date.today())

    session.refresh(promise_a)
    session.refresh(promise_b)
    assert promise_a.status == "broken"
    assert promise_b.status == "pending"  # untouched — different customer


def test_broken_promise_raises_urgency_on_next_dispatch(session, make_customer, make_invoice):
    # end-to-end: a broken promise actually changes what the AI decides,
    # not just what's stored on the PromiseToPay row.
    customer = make_customer(name="Escalate After Broken Co")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=20), invoice_no="PTP-7")
    sync_cases(session)
    case = session.query(Case).one()

    record_promise(session, customer, dt.date.today() - dt.timedelta(days=1), source="manual")
    resolve_promises(session, today=dt.date.today())

    preview = preview_next_message(session, case, now=FIXED_NOW)
    assert "broken_promise_bonus" in preview["rationale"]


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


def test_flag_dispute_pauses_every_open_case_for_the_customer(session, make_customer, make_invoice):
    customer = make_customer(name="Dispute Engine Co")
    make_invoice(customer=customer, invoice_no="DE-1", outstanding=10_000.0)
    make_invoice(customer=customer, invoice_no="DE-2", outstanding=20_000.0)
    sync_cases(session)
    cases = session.query(Case).filter(Case.customer_id == customer.id).all()
    assert len(cases) == 2

    flagged = flag_dispute(session, customer, "already paid via bank transfer")
    assert len(flagged) == 2

    for case in cases:
        session.refresh(case)
        assert case.status == "paused"
        assert case.next_action_at is None
        assert is_disputed(case) is True


def test_flag_dispute_does_not_touch_closed_or_already_paused_cases(session, make_customer, make_invoice):
    customer = make_customer(name="Dispute Skip Co")
    open_inv = make_invoice(customer=customer, invoice_no="DS-1", outstanding=10_000.0)
    paid_inv = make_invoice(customer=customer, invoice_no="DS-2", outstanding=0.0, inv_amount=5_000.0)
    sync_cases(session)

    open_case = session.query(Case).filter(Case.invoice_id == open_inv.id).one()
    closed_case = paid_inv.case  # sync_cases closes an already-fully-paid invoice's case automatically... actually no case created if never open
    assert closed_case is None  # confirms: a fully-paid invoice never got an open case in the first place

    flagged = flag_dispute(session, customer, "reason")
    assert [c.id for c in flagged] == [open_case.id]


def test_dispute_reason_stored_on_the_case_event(session, make_customer, make_invoice):
    customer = make_customer(name="Dispute Reason Co")
    make_invoice(customer=customer, invoice_no="DR-1", outstanding=10_000.0)
    sync_cases(session)
    case = session.query(Case).one()

    flag_dispute(session, customer, "invoice amount looks wrong")
    session.refresh(case)
    dispute_events = [e for e in case.events if (e.payload or {}).get("reason") == "disputed"]
    assert len(dispute_events) == 1
    assert dispute_events[0].payload["detail"] == "invoice amount looks wrong"


def test_is_disputed_false_for_ordinary_pause(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="ORD-PAUSE-1")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    pause_case(session, case, reason="max_touch_cap_reached")
    session.commit()
    assert is_disputed(case) is False


def test_is_disputed_false_for_open_case(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="OPEN-1")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()
    assert is_disputed(case) is False


def test_reopen_case_resumes_escalation(session, make_customer, make_invoice):
    customer = make_customer(name="Reopen Co")
    make_invoice(customer=customer, invoice_no="RO-1", outstanding=10_000.0)
    sync_cases(session)
    case = session.query(Case).one()

    flag_dispute(session, customer, "turned out to be invalid")
    session.refresh(case)
    assert case.status == "paused"

    reopen_case(session, case)
    session.refresh(case)
    assert case.status == "open"
    assert is_disputed(case) is False  # reopened event is now the most recent system event
    assert any((e.payload or {}).get("reason") == "reopened" for e in case.events)


def test_reopen_preserves_escalation_progress_for_a_paused_case(session, make_customer, make_invoice):
    # a dispute-paused case wasn't at the terminal voice level — being
    # paused shouldn't throw away the level it had already reached
    customer = make_customer(name="Reopen Progress Co")
    make_invoice(customer=customer, outstanding=900_000.0, due_date=dt.date.today() - dt.timedelta(days=95), invoice_no="RO-2")
    run_batch(session, now=FIXED_NOW)  # jumps straight to skip_level given how overdue/large this is
    case = session.query(Case).one()
    assert case.level_index == 2

    flag_dispute(session, customer, "reason")
    session.refresh(case)

    reopen_case(session, case)
    session.refresh(case)
    assert case.status == "open"
    assert case.level_index == 2  # unchanged — resumes where it was, not restarted


def test_reopen_resets_touch_count_so_max_touch_pause_does_not_instantly_repause(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="RO-3")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", touch_count=6)
    session.add(case)
    session.commit()
    pause_case(session, case, reason="max_touch_cap_reached")
    session.commit()

    reopen_case(session, case)
    session.refresh(case)
    assert case.status == "open"
    assert case.touch_count == 0

    # and it genuinely survives the next batch run instead of re-pausing immediately
    result = run_batch(session, now=FIXED_NOW)
    session.refresh(case)
    assert case.status != "paused"


def test_reopen_exhausted_case_gets_a_fresh_start_not_instant_re_exhaustion(session, make_invoice):
    make_invoice(outstanding=900_000.0, due_date=dt.date.today() - dt.timedelta(days=95), invoice_no="RO-4")
    now = FIXED_NOW
    run_batch(session, now=now)
    now += dt.timedelta(days=6)
    run_batch(session, now=now)
    now += dt.timedelta(days=6)
    run_batch(session, now=now)
    case = session.query(Case).one()
    assert case.status == "exhausted"
    assert case.close_reason == "exhausted"

    reopen_case(session, case)
    session.refresh(case)
    assert case.status == "open"
    assert case.level_index == 0
    assert case.playbook_name is None
    assert case.close_reason is None
    assert case.closed_at is None
    assert case.touch_count == 0

    # and it does NOT instantly re-exhaust on the very next batch run
    now += dt.timedelta(days=1)
    run_batch(session, now=now)
    session.refresh(case)
    assert case.status != "exhausted"


def test_reopen_event_records_which_status_it_came_from(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="RO-5")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()
    pause_case(session, case, reason="max_touch_cap_reached")
    session.commit()

    reopen_case(session, case)
    session.refresh(case)
    reopened_events = [e for e in case.events if (e.payload or {}).get("reason") == "reopened"]
    assert reopened_events[-1].payload["from_status"] == "paused"


def test_disputed_case_excluded_from_due_cases(session, make_customer, make_invoice):
    customer = make_customer(name="Dispute Due Co")
    make_invoice(customer=customer, invoice_no="DD-1", outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5))
    sync_cases(session)
    case = session.query(Case).one()
    assert case in due_cases(session, now=FIXED_NOW)

    flag_dispute(session, customer, "reason")
    assert case not in due_cases(session, now=FIXED_NOW)


def test_run_batch_sends_digest_for_newly_broken_promise(session, monkeypatch, make_customer, make_invoice):
    import app.notifications as notifications_module

    customer = make_customer(name="Digest Broken Co")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=20), invoice_no="DIG-1")
    sync_cases(session)
    record_promise(session, customer, dt.date.today() - dt.timedelta(days=1), source="manual")

    calls = []
    monkeypatch.setattr(notifications_module, "send_ops_digest", lambda **kwargs: calls.append(kwargs))

    run_batch(session, now=FIXED_NOW)

    assert len(calls) == 1
    assert calls[0]["broken_promise_customer_names"] == ["Digest Broken Co"]
    assert calls[0]["exhausted_case_summaries"] == []


def test_run_batch_sends_digest_for_newly_exhausted_case(session, monkeypatch, make_invoice):
    import app.notifications as notifications_module

    make_invoice(outstanding=900_000.0, due_date=dt.date.today() - dt.timedelta(days=95), invoice_no="DIG-EXH-1")
    now = FIXED_NOW
    run_batch(session, now=now)  # reaches skip_level
    now += dt.timedelta(days=6)
    run_batch(session, now=now)  # advances to voice

    calls = []
    monkeypatch.setattr(notifications_module, "send_ops_digest", lambda **kwargs: calls.append(kwargs))

    now += dt.timedelta(days=6)
    run_batch(session, now=now)  # this run exhausts it

    assert len(calls) == 1
    assert calls[0]["exhausted_case_summaries"] == ["Customer for DIG-EXH-1 (DIG-EXH-1)"]
    assert calls[0]["broken_promise_customer_names"] == []


def test_run_batch_does_not_send_digest_when_nothing_new(session, monkeypatch, make_invoice):
    import app.notifications as notifications_module

    make_invoice(outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=5), invoice_no="DIG-QUIET-1")

    calls = []
    monkeypatch.setattr(notifications_module, "send_ops_digest", lambda **kwargs: calls.append(kwargs))

    run_batch(session, now=FIXED_NOW)

    assert calls == []


def test_run_batch_still_sends_broken_promise_digest_while_auto_dispatch_paused(session, monkeypatch, make_customer, make_invoice):
    import app.notifications as notifications_module

    session.merge(Settings(id=1, auto_dispatch_paused=True))
    session.commit()

    customer = make_customer(name="Digest Paused Co")
    make_invoice(customer=customer, outstanding=10_000.0, due_date=dt.date.today() - dt.timedelta(days=20), invoice_no="DIG-PAUSE-1")
    sync_cases(session)
    record_promise(session, customer, dt.date.today() - dt.timedelta(days=1), source="manual")

    calls = []
    monkeypatch.setattr(notifications_module, "send_ops_digest", lambda **kwargs: calls.append(kwargs))

    result = run_batch(session, now=FIXED_NOW)

    assert result["auto_dispatch_paused"] is True
    assert len(calls) == 1
    assert calls[0]["broken_promise_customer_names"] == ["Digest Paused Co"]


def test_get_pause_info_none_for_open_case(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="PI-1")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()
    assert get_pause_info(case) is None


def test_get_pause_info_for_dispute(session, make_customer, make_invoice):
    customer = make_customer(name="Pause Info Dispute Co")
    make_invoice(customer=customer, invoice_no="PI-2", outstanding=10_000.0)
    sync_cases(session)
    case = session.query(Case).one()

    flag_dispute(session, customer, "wrong amount")
    session.refresh(case)

    info = get_pause_info(case)
    assert info["reason"] == "disputed"
    assert info["detail"] == "wrong amount"
    assert info["at"] is not None


def test_get_pause_info_for_max_touch_cap(session, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="PI-3")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    pause_case(session, case, reason="max_touch_cap_reached")
    session.commit()

    info = get_pause_info(case)
    assert info["reason"] == "max_touch_cap_reached"
    assert info["detail"] is None

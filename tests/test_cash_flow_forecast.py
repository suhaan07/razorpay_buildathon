import datetime as dt

from app.cases.engine import record_promise
from app.data.models import ReliabilityScore
from app.reports.cash_flow_forecast import build_cash_flow_forecast

TODAY = dt.date(2026, 8, 26)  # Wednesday


def _set_score(session, customer, avg_days_late):
    session.add(ReliabilityScore(customer_id=customer.id, score=70.0, band="Fair", avg_days_late=avg_days_late, on_time_rate=0.5))
    session.commit()


def _bucket(forecast, key):
    return next(b for b in forecast.buckets if b.key == key)


def test_empty_state_no_crash(session):
    forecast = build_cash_flow_forecast(session, today=TODAY)
    assert forecast.total_outstanding == 0.0
    assert all(b.total_amount == 0.0 for b in forecast.buckets)
    assert forecast.no_due_date_amount == 0.0


def test_due_today_with_zero_avg_days_late_lands_this_week(session, make_customer, make_invoice):
    customer = make_customer(name="Forecast Co 1")
    make_invoice(customer=customer, invoice_no="CF-1", outstanding=10_000.0, due_date=TODAY)
    _set_score(session, customer, avg_days_late=0.0)

    forecast = build_cash_flow_forecast(session, today=TODAY)
    assert _bucket(forecast, "this_week").total_amount == 10_000.0
    assert _bucket(forecast, "overdue").total_amount == 0.0


def test_predicted_date_before_today_is_overdue_bucket(session, make_customer, make_invoice):
    customer = make_customer(name="Forecast Co 2")
    make_invoice(customer=customer, invoice_no="CF-2", outstanding=10_000.0, due_date=TODAY - dt.timedelta(days=10))
    _set_score(session, customer, avg_days_late=2.0)  # predicted = due+2 = 8 days ago, still overdue

    forecast = build_cash_flow_forecast(session, today=TODAY)
    assert _bucket(forecast, "overdue").total_amount == 10_000.0


def test_avg_days_late_pushes_prediction_into_next_week(session, make_customer, make_invoice):
    customer = make_customer(name="Forecast Co 3")
    # due Friday this week (2026-08-28), avg 5 days late -> predicted 2026-09-02 (next Wed, in "next week")
    make_invoice(customer=customer, invoice_no="CF-3", outstanding=25_000.0, due_date=dt.date(2026, 8, 28))
    _set_score(session, customer, avg_days_late=5.0)

    forecast = build_cash_flow_forecast(session, today=TODAY)
    assert _bucket(forecast, "next_week").total_amount == 25_000.0
    assert _bucket(forecast, "this_week").total_amount == 0.0


def test_no_due_date_excluded_from_buckets_and_tallied_separately(session, make_customer, make_invoice):
    customer = make_customer(name="Forecast Co 4")
    make_invoice(customer=customer, invoice_no="CF-4", outstanding=15_000.0, due_date=None)
    _set_score(session, customer, avg_days_late=0.0)

    forecast = build_cash_flow_forecast(session, today=TODAY)
    assert forecast.no_due_date_amount == 15_000.0
    assert forecast.no_due_date_count == 1
    assert sum(b.total_amount for b in forecast.buckets) == 0.0


def test_customer_with_no_payment_history_flagged_low_confidence(session, make_customer, make_invoice):
    customer = make_customer(name="Forecast Co 5")
    make_invoice(customer=customer, invoice_no="CF-5", outstanding=10_000.0, due_date=TODAY, paid_at=None)
    # no ReliabilityScore row at all AND no paid invoices anywhere for this customer

    forecast = build_cash_flow_forecast(session, today=TODAY)
    this_week = _bucket(forecast, "this_week")
    assert this_week.total_amount == 10_000.0
    assert this_week.low_confidence_amount == 10_000.0


def test_customer_with_real_payment_history_not_flagged_low_confidence(session, make_customer, make_invoice):
    customer = make_customer(name="Forecast Co 6")
    make_invoice(customer=customer, invoice_no="CF-6-paid", outstanding=0.0, inv_amount=5_000.0, due_date=TODAY - dt.timedelta(days=60), paid_at=TODAY - dt.timedelta(days=55))
    make_invoice(customer=customer, invoice_no="CF-6-open", outstanding=10_000.0, due_date=TODAY)
    _set_score(session, customer, avg_days_late=0.0)

    forecast = build_cash_flow_forecast(session, today=TODAY)
    this_week = _bucket(forecast, "this_week")
    assert this_week.total_amount == 10_000.0
    assert this_week.low_confidence_amount == 0.0


def test_pending_future_promise_overrides_the_heuristic(session, make_customer, make_invoice):
    customer = make_customer(name="Forecast Co 7")
    # due long ago with high avg_days_late -> heuristic alone would say "overdue"
    make_invoice(customer=customer, invoice_no="CF-7", outstanding=10_000.0, due_date=TODAY - dt.timedelta(days=60))
    _set_score(session, customer, avg_days_late=90.0)
    record_promise(session, customer, TODAY + dt.timedelta(days=3), source="whatsapp")

    forecast = build_cash_flow_forecast(session, today=TODAY)
    assert _bucket(forecast, "this_week").total_amount == 10_000.0
    assert _bucket(forecast, "overdue").total_amount == 0.0


def test_stale_pending_promise_does_not_override_falls_back_to_heuristic(session, make_customer, make_invoice):
    # a promise whose date has already passed but hasn't been resolved to
    # "broken" yet (no batch run has caught it) must not be trusted as the
    # predicted date — the forecast falls back to the normal heuristic
    # rather than showing money as arriving "in the past".
    customer = make_customer(name="Forecast Co 8")
    make_invoice(customer=customer, invoice_no="CF-8", outstanding=10_000.0, due_date=TODAY)
    _set_score(session, customer, avg_days_late=0.0)
    record_promise(session, customer, TODAY + dt.timedelta(days=1), source="whatsapp")

    # manually roll the promise's date into the past without resolving it,
    # simulating "the batch run hasn't caught this yet"
    from app.data.models import PromiseToPay
    promise = session.query(PromiseToPay).filter(PromiseToPay.customer_id == customer.id).one()
    promise.promised_date = TODAY - dt.timedelta(days=5)
    session.commit()

    forecast = build_cash_flow_forecast(session, today=TODAY)
    # heuristic (due_date=TODAY, avg_days_late=0) says "this_week", not the stale promise date
    assert _bucket(forecast, "this_week").total_amount == 10_000.0
    assert _bucket(forecast, "overdue").total_amount == 0.0


def test_multiple_invoices_same_customer_share_cached_lookup_but_bucket_independently(session, make_customer, make_invoice):
    customer = make_customer(name="Forecast Co 9")
    make_invoice(customer=customer, invoice_no="CF-9A", outstanding=5_000.0, due_date=TODAY)  # this week
    make_invoice(customer=customer, invoice_no="CF-9B", outstanding=7_000.0, due_date=TODAY - dt.timedelta(days=60))  # overdue even with 0 avg late
    _set_score(session, customer, avg_days_late=0.0)

    forecast = build_cash_flow_forecast(session, today=TODAY)
    assert _bucket(forecast, "this_week").total_amount == 5_000.0
    assert _bucket(forecast, "overdue").total_amount == 7_000.0


def test_buckets_plus_no_due_date_reconciles_to_total_outstanding(session, make_customer, make_invoice):
    c1 = make_customer(name="Reconcile Co 1")
    c2 = make_customer(name="Reconcile Co 2")
    make_invoice(customer=c1, invoice_no="REC-1", outstanding=12_345.67, due_date=TODAY)
    make_invoice(customer=c1, invoice_no="REC-2", outstanding=999.01, due_date=TODAY - dt.timedelta(days=200))
    make_invoice(customer=c2, invoice_no="REC-3", outstanding=50_000.0, due_date=None)
    _set_score(session, c1, avg_days_late=3.0)
    _set_score(session, c2, avg_days_late=1.0)

    forecast = build_cash_flow_forecast(session, today=TODAY)
    reconciled = sum(b.total_amount for b in forecast.buckets) + forecast.no_due_date_amount
    assert round(reconciled, 2) == forecast.total_outstanding
    assert forecast.total_outstanding == round(12_345.67 + 999.01 + 50_000.0, 2)


def test_predicted_date_exactly_today_is_this_week_not_overdue(session, make_customer, make_invoice):
    customer = make_customer(name="Forecast Co 10")
    make_invoice(customer=customer, invoice_no="CF-10", outstanding=10_000.0, due_date=TODAY - dt.timedelta(days=3))
    _set_score(session, customer, avg_days_late=3.0)  # predicted exactly today

    forecast = build_cash_flow_forecast(session, today=TODAY)
    assert _bucket(forecast, "this_week").total_amount == 10_000.0
    assert _bucket(forecast, "overdue").total_amount == 0.0


def test_far_out_prediction_lands_beyond_30(session, make_customer, make_invoice):
    customer = make_customer(name="Forecast Co 11")
    make_invoice(customer=customer, invoice_no="CF-11", outstanding=10_000.0, due_date=TODAY + dt.timedelta(days=40))
    _set_score(session, customer, avg_days_late=0.0)

    forecast = build_cash_flow_forecast(session, today=TODAY)
    assert _bucket(forecast, "beyond_30").total_amount == 10_000.0


def test_fully_paid_invoices_excluded_entirely(session, make_customer, make_invoice):
    customer = make_customer(name="Forecast Co 12")
    make_invoice(customer=customer, invoice_no="CF-12", outstanding=0.0, inv_amount=5_000.0, due_date=TODAY, paid_at=TODAY)

    forecast = build_cash_flow_forecast(session, today=TODAY)
    assert forecast.total_outstanding == 0.0
    assert all(b.count == 0 for b in forecast.buckets)
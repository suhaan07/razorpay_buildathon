import datetime as dt

from app.scoring.reliability import compute_score


def test_no_history_gives_neutral_score(session, make_customer):
    customer = make_customer()
    record = compute_score(session, customer)
    assert record.band in ("Good", "Excellent")
    assert record.avg_days_late == 0.0


def test_always_on_time_customer_scores_high(session, make_customer, make_invoice):
    customer = make_customer()
    for i in range(3):
        due = dt.date.today() - dt.timedelta(days=30 + i)
        make_invoice(customer=customer, invoice_no=f"PAID-{i}", outstanding=0.0, due_date=due, paid_at=due)
    record = compute_score(session, customer)
    assert record.score >= 90
    assert record.band == "Excellent"
    assert record.on_time_rate == 1.0


def test_chronically_late_customer_scores_low(session, make_customer, make_invoice):
    customer = make_customer()
    for i in range(3):
        due = dt.date.today() - dt.timedelta(days=60 + i)
        paid = due + dt.timedelta(days=45)
        make_invoice(customer=customer, invoice_no=f"LATE-{i}", outstanding=0.0, due_date=due, paid_at=paid)
    record = compute_score(session, customer)
    assert record.avg_days_late == 45.0
    assert record.band in ("Poor", "Fair")
    assert record.score < 50

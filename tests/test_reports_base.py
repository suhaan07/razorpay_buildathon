import datetime as dt

from app.reports.base import get_customer_report_data


def test_not_found_customer(session, make_customer):
    make_customer(name="Alpha Textiles Pvt Ltd")
    result = get_customer_report_data(session, "completely unrelated corp")
    assert result.status == "not_found"


def test_ambiguous_customer(session, make_customer, make_invoice):
    c1 = make_customer(name="Kumar Enterprises Mumbai")
    c2 = make_customer(name="Kumar Enterprises Delhi")
    make_invoice(customer=c1, invoice_no="A-1")
    make_invoice(customer=c2, invoice_no="A-2")
    result = get_customer_report_data(session, "Kumar Enterprises")
    assert result.status == "ambiguous"
    assert len(result.candidates) == 2


def test_matched_customer_computes_expected_numbers(session, make_customer, make_invoice):
    customer = make_customer(name="Alpha Textiles Pvt Ltd", spoc="Aditi Rao")
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())

    make_invoice(customer=customer, invoice_no="OVERDUE-1", outstanding=10_000.0, due_date=today - dt.timedelta(days=20))
    make_invoice(customer=customer, invoice_no="THISWEEK-1", outstanding=5_000.0, due_date=monday + dt.timedelta(days=2))
    make_invoice(customer=customer, invoice_no="PAID-1", outstanding=0.0, due_date=today - dt.timedelta(days=5))
    make_invoice(customer=customer, invoice_no="UNCLASSIFIED-1", outstanding=2_000.0, due_date=None)

    result = get_customer_report_data(session, "alpha textiles")
    assert result.status == "matched"
    data = result.data
    assert data.overdue_amount == 10_000.0
    assert data.due_this_week_amount == 5_000.0
    assert data.total_outstanding == 17_000.0  # excludes the paid invoice
    assert data.unclassified_count == 1
    assert data.unclassified_amount == 2_000.0
    assert len(data.invoices) == 3  # paid invoice excluded from the list too

import datetime as dt

from app.cases.engine import close_case
from app.data.models import Case
from app.reports.batch_report import build_report


def _make_case(session, invoice, **kwargs):
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15", **kwargs)
    session.add(case)
    session.flush()
    return case


def test_recovery_rate_and_avg_days(session, make_invoice):
    inv1 = make_invoice(outstanding=0.0, inv_amount=10_000.0, invoice_no="R-1")
    case1 = _make_case(session, inv1, created_at=dt.datetime.utcnow() - dt.timedelta(days=10))
    close_case(session, case1, reason="paid", now=dt.datetime.utcnow())

    inv2 = make_invoice(outstanding=20_000.0, inv_amount=20_000.0, invoice_no="R-2")
    _make_case(session, inv2, status="open")

    session.commit()

    report = build_report(session)
    assert report.total_cases == 2
    assert report.closed_paid == 1
    assert report.recovery_rate == 0.5
    assert report.avg_days_to_recovery == 10.0
    assert report.recovered_amount == 10_000.0


def test_exhausted_cases_appear_as_exceptions_with_reached_level(session, make_invoice):
    inv = make_invoice(outstanding=15_000.0, inv_amount=15_000.0, invoice_no="EXC-1")
    _make_case(session, inv, status="exhausted", playbook_name="receivables_escalation", level_index=3)
    session.commit()

    report = build_report(session)
    assert report.exhausted_cases == 1
    assert len(report.exceptions) == 1
    assert report.exceptions[0].invoice_no == "EXC-1"
    assert report.exceptions[0].reached_level == "voice"


def test_escalated_beyond_spoc_and_reached_voice_counts(session, make_invoice):
    inv1 = make_invoice(outstanding=5_000.0, inv_amount=5_000.0, invoice_no="ESC-1")
    _make_case(session, inv1, status="open", playbook_name="receivables_escalation", level_index=0)  # still at spoc

    inv2 = make_invoice(outstanding=5_000.0, inv_amount=5_000.0, invoice_no="ESC-2")
    _make_case(session, inv2, status="open", playbook_name="receivables_escalation", level_index=1)  # manager

    inv3 = make_invoice(outstanding=5_000.0, inv_amount=5_000.0, invoice_no="ESC-3")
    _make_case(session, inv3, status="open", playbook_name="receivables_escalation", level_index=3)  # voice
    session.commit()

    report = build_report(session)
    assert report.escalated_beyond_spoc == 2  # manager + voice cases
    assert report.reached_voice == 1

import io

import pandas as pd
import pytest

from app.data.ingest import IngestError, ingest_xlsx
from app.data.models import Customer, Invoice


def _xlsx_bytes(rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    return buf.getvalue()


def test_ingest_rejects_missing_columns(session):
    body = _xlsx_bytes([{"Customer": "Acme", "Invoice No": "INV-1"}])
    with pytest.raises(IngestError) as excinfo:
        ingest_xlsx(session, body, "bad.xlsx")
    assert "SPOC" in excinfo.value.missing_columns


def test_ingest_creates_customers_and_invoices(session):
    rows = [
        {
            "Customer": "Acme Pvt Ltd",
            "SPOC": "Aditi",
            "Invoice No": "INV-1",
            "Invoice Date": "2026-06-01",
            "Due Date": "2026-07-01",
            "Inv Amount": 1000.0,
            "Received": 0.0,
            "Outstanding": 1000.0,
        },
        {
            "Customer": "Acme Pvt Ltd",
            "SPOC": "Aditi",
            "Invoice No": "INV-2",
            "Invoice Date": "2026-06-01",
            "Due Date": None,
            "Inv Amount": 500.0,
            "Received": 0.0,
            "Outstanding": 500.0,
        },
    ]
    result = ingest_xlsx(session, _xlsx_bytes(rows), "sheet.xlsx")
    assert result.row_count == 2
    assert result.customer_count == 1
    assert result.missing_due_date_count == 1
    assert session.query(Customer).count() == 1
    assert session.query(Invoice).count() == 2


def test_ingest_populates_internal_escalation_chain_when_present(session):
    rows = [
        {
            "Customer": "Acme Pvt Ltd",
            "SPOC": "Aditi Rao",
            "SPOC Email": "aditi.rao@ourcompany.example.com",
            "Manager Name": "Priya Nair",
            "Manager Email": "priya.nair@ourcompany.example.com",
            "Skip Level Name": "Ananya Bose",
            "Skip Level Email": "ananya.bose@ourcompany.example.com",
            "Invoice No": "INV-1",
            "Invoice Date": "2026-06-01",
            "Due Date": "2026-07-01",
            "Inv Amount": 1000.0,
            "Received": 0.0,
            "Outstanding": 1000.0,
        }
    ]
    ingest_xlsx(session, _xlsx_bytes(rows), "sheet.xlsx")
    customer = session.query(Customer).one()
    assert customer.spoc_email == "aditi.rao@ourcompany.example.com"
    assert customer.manager_name == "Priya Nair"
    assert customer.manager_email == "priya.nair@ourcompany.example.com"
    assert customer.skip_level_name == "Ananya Bose"
    assert customer.skip_level_email == "ananya.bose@ourcompany.example.com"


def test_ingest_leaves_escalation_chain_blank_when_columns_absent(session):
    rows = [
        {
            "Customer": "Acme Pvt Ltd",
            "SPOC": "Aditi Rao",
            "Invoice No": "INV-1",
            "Invoice Date": "2026-06-01",
            "Due Date": "2026-07-01",
            "Inv Amount": 1000.0,
            "Received": 0.0,
            "Outstanding": 1000.0,
        }
    ]
    ingest_xlsx(session, _xlsx_bytes(rows), "sheet.xlsx")
    customer = session.query(Customer).one()
    assert customer.manager_email is None
    assert customer.skip_level_email is None


def test_reupload_fully_replaces_prior_data(session):
    first = [
        {
            "Customer": "Old Co",
            "SPOC": "X",
            "Invoice No": "OLD-1",
            "Invoice Date": "2026-01-01",
            "Due Date": "2026-02-01",
            "Inv Amount": 100.0,
            "Received": 0.0,
            "Outstanding": 100.0,
        }
    ]
    ingest_xlsx(session, _xlsx_bytes(first), "a.xlsx")
    assert session.query(Invoice).count() == 1

    second = [
        {
            "Customer": "New Co",
            "SPOC": "Y",
            "Invoice No": "NEW-1",
            "Invoice Date": "2026-01-01",
            "Due Date": "2026-02-01",
            "Inv Amount": 200.0,
            "Received": 0.0,
            "Outstanding": 200.0,
        }
    ]
    ingest_xlsx(session, _xlsx_bytes(second), "b.xlsx")
    assert session.query(Invoice).count() == 1
    assert session.query(Invoice).first().invoice_no == "NEW-1"
    assert session.query(Customer).count() == 1

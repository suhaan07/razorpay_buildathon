from __future__ import annotations

import io
from dataclasses import dataclass

import pandas as pd
from sqlalchemy.orm import Session

from app.data.models import Customer, Invoice, UploadLog
from app.matching.resolver import normalize

REQUIRED_COLUMNS = [
    "Customer",
    "SPOC",
    "Invoice No",
    "Invoice Date",
    "Due Date",
    "Inv Amount",
    "Received",
    "Outstanding",
]
# Optional — if present, populate the internal escalation chain and the
# customer's own contact. Missing columns just leave those fields blank.
OPTIONAL_COLUMNS = [
    "Email",
    "Phone",
    "SPOC Email",
    "Manager Name",
    "Manager Email",
    "Skip Level Name",
    "Skip Level Email",
    "Paid Date",
]


class IngestError(Exception):
    def __init__(self, missing_columns: list[str]):
        self.missing_columns = missing_columns
        super().__init__(f"Missing required columns: {', '.join(missing_columns)}")


@dataclass
class IngestResult:
    row_count: int
    customer_count: int
    missing_due_date_count: int


def _clean_date(value):
    if pd.isna(value):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _clean_float(value) -> float:
    return 0.0 if pd.isna(value) else float(value)


def _clean_str(row, column: str) -> str | None:
    if column not in row.index or pd.isna(row.get(column)):
        return None
    return str(row[column]).strip()


def ingest_xlsx(session: Session, file_bytes: bytes, filename: str) -> IngestResult:
    df = pd.read_excel(io.BytesIO(file_bytes))

    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise IngestError(missing)

    # Full replace: deleting Customer objects (not a bulk query) cascades through
    # ORM relationships to Invoice -> Case -> CaseEvent and ReliabilityScore, so
    # a re-upload deliberately resets case history too.
    for customer in session.query(Customer).all():
        session.delete(customer)
    session.flush()

    customers_by_name: dict[str, Customer] = {}
    missing_due_date_count = 0

    for _, row in df.iterrows():
        customer_name = str(row["Customer"]).strip()
        customer = customers_by_name.get(customer_name)
        if customer is None:
            customer = Customer(
                name=customer_name,
                normalized_name=normalize(customer_name),
                email=_clean_str(row, "Email"),
                phone=_clean_str(row, "Phone"),
                spoc=_clean_str(row, "SPOC"),
                spoc_email=_clean_str(row, "SPOC Email"),
                manager_name=_clean_str(row, "Manager Name"),
                manager_email=_clean_str(row, "Manager Email"),
                skip_level_name=_clean_str(row, "Skip Level Name"),
                skip_level_email=_clean_str(row, "Skip Level Email"),
            )
            session.add(customer)
            session.flush()
            customers_by_name[customer_name] = customer

        due_date = _clean_date(row["Due Date"])
        if due_date is None:
            missing_due_date_count += 1

        paid_at = _clean_date(row["Paid Date"]) if "Paid Date" in df.columns else None

        invoice = Invoice(
            customer_id=customer.id,
            invoice_no=str(row["Invoice No"]).strip(),
            invoice_date=_clean_date(row["Invoice Date"]),
            due_date=due_date,
            inv_amount=_clean_float(row["Inv Amount"]),
            received=_clean_float(row["Received"]),
            outstanding=_clean_float(row["Outstanding"]),
            paid_at=paid_at,
        )
        session.add(invoice)

    session.flush()

    upload_log = UploadLog(
        filename=filename,
        row_count=len(df),
        missing_due_date_count=missing_due_date_count,
    )
    session.add(upload_log)
    session.commit()

    return IngestResult(
        row_count=len(df),
        customer_count=len(customers_by_name),
        missing_due_date_count=missing_due_date_count,
    )

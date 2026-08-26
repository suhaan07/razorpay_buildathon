from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.data.models import Customer, Invoice, Settings
from app.db import Base


@pytest.fixture(autouse=True)
def _no_external_credentials(monkeypatch):
    """Tests must never hit real external services, no matter what's sitting
    in the developer's local .env — python-dotenv loads it for every process,
    tests included. (This is exactly the bug that let a real Razorpay test
    account get rate-limited by earlier test runs before this fixture existed.)"""
    for var in (
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
        "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET",
        "SENDGRID_API_KEY", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def session():
    # StaticPool: a single shared connection for the whole in-memory DB, so
    # requests handled on the TestClient's worker thread see the same tables
    # and data as the fixture that created them.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    db = TestSession()
    # The real app defaults a fresh Settings row to auto_dispatch_paused=True
    # (see app/data/models.py) so a real deployment doesn't burn a limited
    # Razorpay link quota unattended. Tests exercising run_batch()'s actual
    # dispatch behavior need it unpaused by default — a test that wants the
    # paused path can still flip this row itself.
    db.add(Settings(id=1, auto_dispatch_paused=False))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def make_customer(session):
    def _make(
        name="Acme Pvt Ltd",
        spoc="Aditi Rao",
        spoc_email="aditi.rao@ourcompany.example.com",
        manager_name="Priya Nair",
        manager_email="priya.nair@ourcompany.example.com",
        skip_level_name="Ananya Bose",
        skip_level_email="ananya.bose@ourcompany.example.com",
        email="acme@example.com",
        phone="+919876500001",
    ):
        customer = Customer(
            name=name,
            normalized_name=name.lower(),
            spoc=spoc,
            spoc_email=spoc_email,
            manager_name=manager_name,
            manager_email=manager_email,
            skip_level_name=skip_level_name,
            skip_level_email=skip_level_email,
            email=email,
            phone=phone,
        )
        session.add(customer)
        session.flush()
        return customer

    return _make


_UNSET = object()  # distinguishes "caller didn't pass due_date" from an explicit due_date=None (Unclassified)


@pytest.fixture()
def make_invoice(session, make_customer):
    def _make(customer=None, outstanding=100_000.0, due_date=_UNSET, invoice_no="INV-1", paid_at=None, inv_amount=None):
        # default customer is named after the invoice number so calling
        # make_invoice() more than once per test doesn't collide on the
        # unique Customer.name constraint unless a shared customer is passed
        customer = customer or make_customer(name=f"Customer for {invoice_no}", email=f"{invoice_no.lower()}@example.com")
        if due_date is _UNSET:
            due_date = dt.date.today() - dt.timedelta(days=10)
        amount = inv_amount if inv_amount is not None else outstanding
        invoice_date = (due_date - dt.timedelta(days=30)) if due_date is not None else dt.date.today() - dt.timedelta(days=40)
        invoice = Invoice(
            customer_id=customer.id,
            invoice_no=invoice_no,
            invoice_date=invoice_date,
            due_date=due_date,
            inv_amount=amount,
            received=amount - outstanding,
            outstanding=outstanding,
            paid_at=paid_at,
        )
        session.add(invoice)
        session.flush()
        return invoice

    return _make

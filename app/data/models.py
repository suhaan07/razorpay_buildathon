from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)  # customer's own contact (WhatsApp lookup uses phone)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Whoever last WhatsApp-queried this customer's report (e.g. "whatsapp:
    # +919876500001") — the payment-received alert replies to this same
    # number rather than a fixed operator number, since that's who actually
    # asked about this account. None until someone has queried at least once.
    last_whatsapp_query_from: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Internal escalation chain — our own team, not the customer's staff.
    # The email escalation engine walks spoc -> manager -> skip_level.
    spoc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    spoc_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manager_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manager_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skip_level_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skip_level_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # A single "pay everything outstanding" Razorpay link, reused across the
    # escalation engine AND the WhatsApp Q&A bot (see app/billing.py) rather
    # than minting a fresh one on every touch. Regenerated only when the
    # cached amount no longer matches current total outstanding.
    consolidated_pay_link_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consolidated_pay_link_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    consolidated_pay_link_amount: Mapped[float | None] = mapped_column(Float, nullable=True)

    invoices: Mapped[list["Invoice"]] = relationship(back_populates="customer", cascade="all, delete-orphan")
    reliability: Mapped["ReliabilityScore | None"] = relationship(
        back_populates="customer", uselist=False, cascade="all, delete-orphan"
    )
    promises: Mapped[list["PromiseToPay"]] = relationship(back_populates="customer", cascade="all, delete-orphan")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    invoice_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    invoice_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    due_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    inv_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    received: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    outstanding: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    paid_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="invoices")
    case: Mapped["Case | None"] = relationship(back_populates="invoice", uselist=False, cascade="all, delete-orphan")


class UploadLog(Base):
    __tablename__ = "upload_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_due_date_count: Mapped[int] = mapped_column(Integer, default=0)


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), unique=True, nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="open")  # open | paused | closed | exhausted
    bucket: Mapped[str] = mapped_column(String(16), default="Unclassified")
    playbook_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    level_index: Mapped[int] = mapped_column(Integer, default=0)  # 0=spoc 1=manager 2=skip_level 3=voice
    touch_count: Mapped[int] = mapped_column(Integer, default=0)

    pay_link_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pay_link_url: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    last_action_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    next_action_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)  # paid | exhausted

    invoice: Mapped["Invoice"] = relationship(back_populates="case")
    customer: Mapped["Customer"] = relationship()
    events: Mapped[list["CaseEvent"]] = relationship(back_populates="case", cascade="all, delete-orphan")


class CaseEvent(Base):
    __tablename__ = "case_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # decision | dispatch | webhook | system
    channel: Mapped[str | None] = mapped_column(String(16), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    case: Mapped["Case"] = relationship(back_populates="events")


class ReliabilityScore(Base):
    __tablename__ = "reliability_scores"

    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), primary_key=True)
    score: Mapped[float] = mapped_column(Float, default=70.0)
    band: Mapped[str] = mapped_column(String(16), default="Fair")
    avg_days_late: Mapped[float] = mapped_column(Float, default=0.0)
    on_time_rate: Mapped[float] = mapped_column(Float, default=1.0)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    customer: Mapped["Customer"] = relationship(back_populates="reliability")


class Settings(Base):
    """A single row (id is always 1). Defaults to paused: a fresh DB starts
    with automatic dispatch OFF, since "Run batch" can mint real Razorpay
    payment links and a test account's link quota is precious and finite —
    requiring an explicit opt-in is safer than requiring an explicit
    opt-out. Per-case "Send now (test)" on the Cases page is deliberate,
    one-off, and unaffected by this — it's the automatic batch sweep this
    guards, not manual testing."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    auto_dispatch_paused: Mapped[bool] = mapped_column(Boolean, default=True)


class PromiseToPay(Base):
    """A customer's stated commitment to clear their account by a given
    date — scoped to the whole customer, not one invoice, since both the
    WhatsApp bot and a SPOC logging a phone call naturally think in terms
    of "when will THIS CUSTOMER pay", not one line item. Only ever one
    "pending" promise per customer at a time — a newer one supersedes the
    older (see app/cases/engine.py::record_promise); "kept"/"broken" are
    resolved by whether the customer's total outstanding hit zero by the
    promised date (see resolve_promises)."""

    __tablename__ = "promises_to_pay"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    promised_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(16))  # "whatsapp" | "manual"
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | kept | broken | superseded
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="promises")

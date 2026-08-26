"""Manual end-to-end smoke test: ingest the synthetic sheet, run a few batch
passes (simulating time advancing), simulate some payments via the webhook
handler function directly, and print the batch report. Not a pytest file —
run directly: python scripts/smoke_test.py
"""

from __future__ import annotations

import datetime as dt
import os
import random
from pathlib import Path

from app.cases.engine import close_case, record_event
from app.cases.engine import run_batch
from app.data.ingest import ingest_xlsx
from app.data.models import Case
from app.db import Base, SessionLocal, engine
from app.reports.batch_report import build_report
from app.scoring.reliability import recompute_for_customer_id

DB_PATH = Path(__file__).parent.parent / "recovery.db"


def simulate_payment(session, case: Case) -> None:
    """Stand-in for the Razorpay payment.captured webhook."""
    invoice = case.invoice
    invoice.received = invoice.inv_amount
    invoice.outstanding = 0.0
    invoice.paid_at = dt.date.today()
    close_case(session, case, reason="paid")
    record_event(session, case, type="webhook", payload={"event": "payment.captured", "simulated": True})
    session.commit()
    recompute_for_customer_id(session, case.customer_id)


def main() -> None:
    # This script must never hit real external services, no matter what's
    # sitting in the developer's local .env — python-dotenv (via app.db,
    # already imported above) has loaded it into os.environ by this point,
    # so stripping here — after import, before any dispatch — is what
    # actually takes effect.
    for var in (
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VOICE_FROM",
        "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET",
        "SENDGRID_API_KEY", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
    ):
        os.environ.pop(var, None)

    if DB_PATH.exists():
        DB_PATH.unlink()
    Base.metadata.create_all(bind=engine)

    session = SessionLocal()
    sheet_path = Path(__file__).parent.parent / "sample_ar_sheet.xlsx"
    result = ingest_xlsx(session, sheet_path.read_bytes(), sheet_path.name)
    print(f"ingested: {result}")

    random.seed(7)
    now = dt.datetime.combine(dt.date.today(), dt.time(10, 0))  # dodge the default quiet-hours window

    for i in range(8):
        summary = run_batch(session, now=now)
        print(f"batch run {i}: {summary}")

        # after the first dispatch wave, simulate ~40% of still-open cases paying
        if i >= 1:
            open_cases = session.query(Case).filter(Case.status == "open").all()
            for case in open_cases:
                if random.random() < 0.15:
                    simulate_payment(session, case)

        now += dt.timedelta(days=4)

    by_status: dict[str, int] = {}
    for case in session.query(Case).all():
        by_status[case.status] = by_status.get(case.status, 0) + 1
    print(f"final case status counts: {by_status}")

    report = build_report(session)
    print(report)


if __name__ == "__main__":
    main()

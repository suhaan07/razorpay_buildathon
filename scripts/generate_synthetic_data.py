"""Generates a synthetic AR invoice sheet: ~55 currently-outstanding invoices
spread across every aging bucket, plus paid history rows (with a "Paid Date")
per customer so the reliability score has something real to compute from.

Payment-behavior distribution is deliberate, not random per-row, so the demo
batch report shows a real recovery curve: a handful of "reliable" customers
pay fast, most are middling, and a few are chronically late or exhausted.
"""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import pandas as pd
from openpyxl.utils import get_column_letter

random.seed(42)

OUT_PATH = Path(__file__).parent.parent / "sample_ar_sheet.xlsx"
TODAY = dt.date.today()

CUSTOMERS = [
    ("Alpha Textiles Pvt Ltd", "reliable"),
    ("Beta Logistics", "reliable"),
    ("Gamma Technologies Private Limited", "reliable"),
    ("Delta Retail Co", "average"),
    ("Epsilon Foods LLC", "average"),
    ("Zeta Hardware Corp", "average"),
    ("Eta Chemicals Ltd", "average"),
    ("Theta Packaging Inc", "average"),
    ("Iota Electronics", "average"),
    ("Kappa Constructions", "chronic_late"),
    ("Lambda Pharma Pvt Ltd", "chronic_late"),
    ("Mu Steel Traders", "chronic_late"),
    ("Nu Agro Exports", "chronic_late"),
    ("Xi Furniture Co", "unresponsive"),
    ("Omicron Traders", "unresponsive"),
    ("Pi Plastics Ltd", "unresponsive"),
]

# Kept deliberately small: every outstanding invoice here, and the total
# per customer, stays under Razorpay's real per-link cap (confirmed against
# the live test account at exactly ₹5,00,000 — ₹5,00,001 and up gets
# rejected, see app/integrations/razorpay_client.py's oversized-stub
# fallback). These customers exist so "pay this invoice" AND "pay
# everything" always produce a real, clickable Razorpay link instead of the
# stub fallback — every other customer above is large enough to hit that
# fallback on its consolidated total.
SMALL_ACCOUNT_CUSTOMERS = [
    ("Sigma Bearings Co", "reliable"),
    ("Rho Textiles Mills", "average"),
    ("Tau Interiors Pvt Ltd", "average"),
    ("Upsilon Fasteners Ltd", "chronic_late"),
    ("Phi Components Inc", "reliable"),
    ("Chi Packaging Solutions", "unresponsive"),
]

SPOCS = ["Aditi Rao", "Rohan Mehta", "Sana Iyer", "Karan Shah"]

# Internal escalation chain — our own collections team, not the customer's
# staff. A handful of SPOCs report to one of two managers, both under one
# skip-level exec, so the roster is small and reused across customers.
MANAGERS = {
    "Aditi Rao": "Priya Nair",
    "Rohan Mehta": "Priya Nair",
    "Sana Iyer": "Vikram Desai",
    "Karan Shah": "Vikram Desai",
}
SKIP_LEVEL_NAME = "Ananya Bose"


def _internal_email(name: str) -> str:
    return name.lower().replace(" ", ".") + "@ourcompany.example.com"


_lateness_by_profile = {
    "reliable": (-5, 2),
    "average": (0, 20),
    "chronic_late": (15, 60),
    "unresponsive": (40, 120),
}


def _phone_for(idx: int) -> str:
    return f"+9198765{idx:05d}"


def _email_for(name: str) -> str:
    return name.lower().replace(" ", ".").replace(",", "") + "@example.com"


def _rand_amount() -> float:
    return round(random.uniform(25_000, 950_000), 2)


def _rand_small_amount() -> float:
    # 2 invoices at the top of this range (150,000 each) still total well
    # under the 500,000 cap — see SMALL_ACCOUNT_CUSTOMERS above.
    return round(random.uniform(25_000, 150_000), 2)


def _rows_for_customer(
    name: str,
    profile: str,
    idx: int,
    invoice_counter: int,
    outstanding_count_range: tuple[int, int],
    outstanding_amount_fn,
) -> tuple[list[dict], int]:
    rows = []
    spoc = SPOCS[idx % len(SPOCS)]
    manager = MANAGERS[spoc]
    phone = _phone_for(idx)
    email = _email_for(name)
    contact_cols = {
        "SPOC": spoc,
        "SPOC Email": _internal_email(spoc),
        "Manager Name": manager,
        "Manager Email": _internal_email(manager),
        "Skip Level Name": SKIP_LEVEL_NAME,
        "Skip Level Email": _internal_email(SKIP_LEVEL_NAME),
    }

    # 2-3 paid historical invoices, lateness sampled per the customer's
    # profile — always the wide amount range since these never touch the
    # live payment-link cap (no link is ever created for a paid invoice).
    for _ in range(random.randint(2, 3)):
        invoice_counter += 1
        amount = _rand_amount()
        due_date = TODAY - dt.timedelta(days=random.randint(30, 200))
        lo, hi = _lateness_by_profile[profile]
        paid_date = due_date + dt.timedelta(days=max(0, random.randint(lo, hi)))
        rows.append(
            {
                "Customer": name,
                **contact_cols,
                "Email": email,
                "Phone": phone,
                "Invoice No": f"INV-{invoice_counter}",
                "Invoice Date": due_date - dt.timedelta(days=30),
                "Due Date": due_date,
                "Inv Amount": amount,
                "Received": amount,
                "Outstanding": 0.0,
                "Paid Date": paid_date,
            }
        )

    # currently outstanding invoices, spread across buckets by profile
    for _ in range(random.randint(*outstanding_count_range)):
        invoice_counter += 1
        amount = outstanding_amount_fn()
        if profile == "reliable":
            days_overdue = random.choice([-10, -3, 5])
        elif profile == "average":
            days_overdue = random.choice([5, 12, 20, 28])
        elif profile == "chronic_late":
            days_overdue = random.choice([35, 45, 65, 80])
        else:  # unresponsive
            days_overdue = random.choice([70, 95, 110, 130])
        due_date = TODAY - dt.timedelta(days=days_overdue)
        received = 0.0
        rows.append(
            {
                "Customer": name,
                **contact_cols,
                "Email": email,
                "Phone": phone,
                "Invoice No": f"INV-{invoice_counter}",
                "Invoice Date": due_date - dt.timedelta(days=30),
                "Due Date": due_date,
                "Inv Amount": amount,
                "Received": received,
                "Outstanding": amount - received,
                "Paid Date": None,
            }
        )

    return rows, invoice_counter


def build_rows() -> list[dict]:
    rows = []
    invoice_counter = 1000

    for idx, (name, profile) in enumerate(CUSTOMERS):
        customer_rows, invoice_counter = _rows_for_customer(name, profile, idx, invoice_counter, (3, 5), _rand_amount)
        rows += customer_rows

    # Small accounts, deliberately capped so their outstanding total never
    # approaches the real Razorpay link cap — see SMALL_ACCOUNT_CUSTOMERS.
    # Exactly 2 outstanding invoices each (never 3-5): even at this range's
    # max (150,000 x 2 = 300,000) the consolidated total stays well under
    # 500,000, so "pay this invoice" and "pay everything" both always
    # produce a real link instead of the oversized-stub fallback.
    for offset, (name, profile) in enumerate(SMALL_ACCOUNT_CUSTOMERS):
        idx = len(CUSTOMERS) + offset
        customer_rows, invoice_counter = _rows_for_customer(name, profile, idx, invoice_counter, (2, 2), _rand_small_amount)
        rows += customer_rows

    # a couple of rows with a missing due date — the deliberate "Unclassified" edge case
    invoice_counter += 1
    rows.append(
        {
            "Customer": CUSTOMERS[0][0],
            "SPOC": SPOCS[0],
            "SPOC Email": _internal_email(SPOCS[0]),
            "Manager Name": MANAGERS[SPOCS[0]],
            "Manager Email": _internal_email(MANAGERS[SPOCS[0]]),
            "Skip Level Name": SKIP_LEVEL_NAME,
            "Skip Level Email": _internal_email(SKIP_LEVEL_NAME),
            "Email": _email_for(CUSTOMERS[0][0]),
            "Phone": _phone_for(0),
            "Invoice No": f"INV-{invoice_counter}",
            "Invoice Date": TODAY - dt.timedelta(days=40),
            "Due Date": None,
            "Inv Amount": 120000.0,
            "Received": 0.0,
            "Outstanding": 120000.0,
            "Paid Date": None,
        }
    )

    return rows


# Default (unset) column widths leave the three date columns too narrow for
# their own "YYYY-MM-DD" format — unlike text, Excel never overflows a
# number/date into an empty neighboring cell, so a too-narrow date column
# renders as "####" instead of just looking cramped. Set explicit widths for
# every column so nothing hashes out and the sheet is legible on open.
_COLUMN_WIDTHS = {
    "Customer": 32, "SPOC": 16, "SPOC Email": 30, "Manager Name": 16, "Manager Email": 30,
    "Skip Level Name": 16, "Skip Level Email": 30, "Email": 30, "Phone": 16, "Invoice No": 12,
    "Invoice Date": 14, "Due Date": 14, "Inv Amount": 14, "Received": 14, "Outstanding": 14, "Paid Date": 14,
}


def main() -> None:
    rows = build_rows()
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(OUT_PATH, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
        ws = writer.sheets["Sheet1"]
        for i, col_name in enumerate(df.columns, start=1):
            ws.column_dimensions[get_column_letter(i)].width = _COLUMN_WIDTHS.get(col_name, 16)
    outstanding_rows = df[df["Outstanding"] > 0]
    print(f"wrote {len(df)} rows ({len(outstanding_rows)} currently outstanding) to {OUT_PATH}")


if __name__ == "__main__":
    main()

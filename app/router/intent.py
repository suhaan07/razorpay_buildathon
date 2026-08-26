"""Free-text WhatsApp message -> (report type, customer name), a
promise-to-pay, or a dispute flag. The bot answers exactly four kinds of
message:
  "Give me a weekly payment schedule for <customer>"
  "Give me a weekly collection follow-up for <customer>"
  "Promise to pay for <customer> by <date phrase>"
  "Dispute for <customer>: <reason>" (reason optional)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "collection follow-up" is checked before generic "payment" so a message
# like "collection follow-up ... payment ..." isn't misclassified.
_REPORT_KEYWORDS: list[tuple[str, list[str]]] = [
    ("collection_followup", ["collection follow-up", "collection followup", "collection", "follow-up", "followup"]),
    ("payment_schedule", ["payment schedule", "payment", "weekly schedule"]),
]

_NOISE_WORDS = {"please", "asap", "thanks", "thank", "you", "now", "today", "urgently"}
_FOR_RE = re.compile(r"\bfor\s+(.+)$", re.IGNORECASE)

_PROMISE_KEYWORD = "promise to pay"
_PROMISE_RE = re.compile(r"\bfor\s+(.+)\s+by\s+(.+)$", re.IGNORECASE)


def classify_report_type(text: str) -> str | None:
    lowered = text.lower()
    for report_type, keywords in _REPORT_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return report_type
    return None


def extract_customer_name(text: str) -> str | None:
    match = _FOR_RE.search(text)
    if not match:
        return None
    name = match.group(1).strip().rstrip(".?!")
    tokens = [t for t in name.split() if t.lower() not in _NOISE_WORDS]
    name = " ".join(tokens).strip()
    return name or None


@dataclass
class ParsedMessage:
    report_type: str | None
    customer_name: str | None


def parse_message(text: str) -> ParsedMessage:
    return ParsedMessage(report_type=classify_report_type(text), customer_name=extract_customer_name(text))


@dataclass
class ParsedPromise:
    customer_name: str | None  # None if the "for ... by ..." shape wasn't found at all
    date_phrase: str | None  # raw text, still needs app.router.date_phrase.parse_date_phrase()


def is_promise_message(text: str) -> bool:
    return _PROMISE_KEYWORD in text.lower()


def parse_promise_message(text: str) -> ParsedPromise:
    """Only meaningful when is_promise_message(text) is True. Splits on the
    LAST "by" so a customer name that itself happens to contain "by" (e.g.
    "Bytewise Solutions") still splits correctly, since the date phrase is
    always the trailing clause."""
    match = _PROMISE_RE.search(text)
    if not match:
        return ParsedPromise(customer_name=None, date_phrase=None)
    name = match.group(1).strip().rstrip(".?!")
    date_phrase = match.group(2).strip().rstrip(".?!")
    return ParsedPromise(customer_name=name or None, date_phrase=date_phrase or None)


_DISPUTE_KEYWORD = "dispute"
_DISPUTE_FOR_RE = re.compile(r"\bfor\s+(.+)$", re.IGNORECASE)


@dataclass
class ParsedDispute:
    customer_name: str | None
    reason: str | None


def is_dispute_message(text: str) -> bool:
    return _DISPUTE_KEYWORD in text.lower()


def parse_dispute_message(text: str) -> ParsedDispute:
    """Format: "Dispute for <customer>: <reason>" — the reason is optional
    (everything after the FIRST colon), the customer name is everything
    between "for" and the colon (or end of string if there's no reason)."""
    match = _DISPUTE_FOR_RE.search(text)
    if not match:
        return ParsedDispute(customer_name=None, reason=None)

    rest = match.group(1).strip()
    if ":" in rest:
        name_part, _, reason_part = rest.partition(":")
        name = name_part.strip().rstrip(".?!")
        reason = reason_part.strip().rstrip(".?!") or None
    else:
        name = rest.rstrip(".?!")
        reason = None
    return ParsedDispute(customer_name=name or None, reason=reason)

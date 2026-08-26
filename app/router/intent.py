"""Free-text WhatsApp message -> (report type, customer name). The bot
answers exactly two questions:
  "Give me a weekly payment schedule for <customer>"
  "Give me a weekly collection follow-up for <customer>"
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

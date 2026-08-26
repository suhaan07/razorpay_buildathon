"""normalize() + fuzzy resolve() for matching informal customer names
(WhatsApp senders, free text) against canonical sheet names."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process
from sqlalchemy.orm import Session

from app.data.models import Customer

MATCH_THRESHOLD = float(os.getenv("MATCH_THRESHOLD", "72"))
MATCH_AMBIGUITY_GAP = float(os.getenv("MATCH_AMBIGUITY_GAP", "5"))

_SUFFIX_TOKENS = {"pvt", "ltd", "private", "limited", "llc", "co", "corp", "inc", "customer"}
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize(name: str) -> str:
    value = _PUNCT_RE.sub(" ", name.lower()).strip()
    while True:
        tokens = value.split()
        if tokens and tokens[-1] in _SUFFIX_TOKENS:
            tokens = tokens[:-1]
            new_value = " ".join(tokens).strip()
            if new_value == value:
                break
            value = new_value
            continue
        break
    return value


@dataclass
class ResolveResult:
    status: str  # matched | ambiguous | not_found
    customer_name: str | None = None
    candidates: list[str] | None = None


def resolve(query: str, candidate_names: list[str]) -> ResolveResult:
    if not candidate_names:
        return ResolveResult(status="not_found")

    normalized_query = normalize(query)
    normalized_candidates = {name: normalize(name) for name in candidate_names}

    scored = process.extract(
        normalized_query,
        normalized_candidates,
        scorer=fuzz.WRatio,
        limit=len(normalized_candidates),
    )
    # scored: list[(normalized_value, score, original_name_key)]
    scored = sorted(scored, key=lambda item: item[1], reverse=True)

    if not scored or scored[0][1] < MATCH_THRESHOLD:
        return ResolveResult(status="not_found")

    top_score = scored[0][1]
    close = [name for _, score, name in scored if top_score - score <= MATCH_AMBIGUITY_GAP]
    if len(close) >= 2:
        return ResolveResult(status="ambiguous", candidates=close)

    return ResolveResult(status="matched", customer_name=scored[0][2])


@dataclass
class CustomerLookup:
    status: str  # matched | ambiguous | not_found
    customer: Customer | None = None
    candidates: list[str] = field(default_factory=list)


def resolve_customer(session: Session, name_query: str) -> CustomerLookup:
    """DB-aware counterpart to resolve() — matches name_query against every
    Customer row and returns the ORM object directly, so callers don't each
    reimplement the query-all/match/look-up-by-name dance."""
    all_customers = session.query(Customer).all()
    match = resolve(name_query, [c.name for c in all_customers])
    if match.status == "not_found":
        return CustomerLookup(status="not_found")
    if match.status == "ambiguous":
        return CustomerLookup(status="ambiguous", candidates=match.candidates or [])
    customer = next(c for c in all_customers if c.name == match.customer_name)
    return CustomerLookup(status="matched", customer=customer)

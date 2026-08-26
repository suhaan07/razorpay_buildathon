"""The AI decision layer — fully homegrown, no external LLM call.

decide() computes a transparent "urgency score" (0-100) from four signals
already sitting in the case context, and maps it to two outputs: which rung
of the internal escalation chain (spoc -> manager -> skip_level) a case
should be AT, and how many days to wait before the next escalation if this
one goes unanswered. It never suggests jumping straight to voice — voice is
reached only mechanically, after skip_level's wait elapses unpaid (see
cases/engine.py) — matching the product rule that the AI call is a last
resort, not a shortcut.

Pure function: same inputs always produce the same output, no network call,
no latency, no API cost, and the score breakdown IS the audit-trail
rationale (see DESIGN.md for the worked formula).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

LEVEL_NAMES = ["spoc", "manager", "skip_level", "voice"]

BUCKET_SEVERITY = {
    "Unclassified": 0,
    "Not Due": 0,
    "0-15": 10,
    "16-30": 25,
    "31-60": 50,
    "61-90": 65,
    "90+": 80,
}

RELIABILITY_BONUS_MAX = float(os.getenv("URGENCY_RELIABILITY_BONUS_MAX", "25"))
MATERIALITY_BONUS_MAX = float(os.getenv("URGENCY_MATERIALITY_BONUS_MAX", "10"))
TOUCH_BONUS_MAX = float(os.getenv("URGENCY_TOUCH_BONUS_MAX", "10"))
LARGE_INVOICE_THRESHOLD = float(os.getenv("LARGE_INVOICE_THRESHOLD", "300000"))
TOUCH_CAP_FOR_SCORING = float(os.getenv("URGENCY_TOUCH_CAP_FOR_SCORING", "5"))

MANAGER_THRESHOLD = float(os.getenv("URGENCY_MANAGER_THRESHOLD", "35"))
SKIP_LEVEL_THRESHOLD = float(os.getenv("URGENCY_SKIP_LEVEL_THRESHOLD", "60"))
MAX_WAIT_DAYS = int(os.getenv("URGENCY_MAX_WAIT_DAYS", "5"))
MIN_WAIT_DAYS = int(os.getenv("URGENCY_MIN_WAIT_DAYS", "1"))


@dataclass
class DecisionResult:
    suggested_level: int  # index into LEVEL_NAMES — capped at 2 (skip_level); never suggests 3 (voice)
    wait_days: int
    urgency_score: float
    rationale: str
    source: str = "homegrown"


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def decide(case_context: dict) -> DecisionResult:
    bucket_severity = BUCKET_SEVERITY.get(case_context["bucket"], 0)

    reliability_score = case_context.get("reliability_score", 100.0)
    reliability_bonus = ((100.0 - reliability_score) / 100.0) * RELIABILITY_BONUS_MAX

    outstanding = case_context.get("outstanding_amount", 0.0)
    materiality_bonus = min(outstanding / LARGE_INVOICE_THRESHOLD, 1.0) * MATERIALITY_BONUS_MAX

    touch_count = case_context.get("touch_count", 0)
    touch_bonus = min(touch_count / TOUCH_CAP_FOR_SCORING, 1.0) * TOUCH_BONUS_MAX

    urgency = _clip(bucket_severity + reliability_bonus + materiality_bonus + touch_bonus)

    if urgency >= SKIP_LEVEL_THRESHOLD:
        suggested_level = 2
    elif urgency >= MANAGER_THRESHOLD:
        suggested_level = 1
    else:
        suggested_level = 0

    span = MAX_WAIT_DAYS - MIN_WAIT_DAYS
    wait_days = round(MAX_WAIT_DAYS - (urgency / 100.0) * span)
    wait_days = max(MIN_WAIT_DAYS, min(MAX_WAIT_DAYS, wait_days))

    rationale = (
        f"urgency={urgency:.1f} = bucket[{case_context['bucket']}]={bucket_severity:.0f} + "
        f"reliability_bonus={reliability_bonus:.1f} (score={reliability_score:.0f}) + "
        f"materiality_bonus={materiality_bonus:.1f} (outstanding={outstanding:.0f}) + "
        f"touch_bonus={touch_bonus:.1f} (touches={touch_count}) "
        f"-> suggested_level={LEVEL_NAMES[suggested_level]}, wait_days={wait_days}"
    )
    return DecisionResult(suggested_level=suggested_level, wait_days=wait_days, urgency_score=urgency, rationale=rationale)

"""Routes free text to one of app/chatbot/tools.py's functions via Claude's
tool-calling — the model NEVER computes a number, an escalation decision,
or an amount itself; every fact in a reply comes from a tool result. That
keeps this consistent with the rest of the app's "AI decides nothing on
its own" principle: the homegrown decision layer still owns every real
business decision, this only owns turning "what does Acme owe" into a
function call.

WRITE_TOOLS (generate_payment_link, send_reminder_email) are deliberately
NEVER auto-executed by the model loop — see chat() below. Confirmation is
handled entirely at this layer, under fully deterministic control, not
delegated to the model's own judgment about what counts as consent."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.chatbot.tools import TOOL_SCHEMAS, WRITE_TOOLS, describe_pending_action, execute_tool

_client = None

_SYSTEM_PROMPT = (
    "You are an assistant for a B2B accounts-receivable recovery system. You can look up what a customer "
    "owes, forecast cash flow, filter customers by outstanding amount, generate a real Razorpay payment "
    "link, or send a real reminder email. You never compute a number, a date, or a decision yourself — "
    "always call a tool for anything involving real data, and only ever state facts a tool actually "
    "returned to you. Never invent a customer name, invoice number, or amount. Call at most one tool per "
    "response. Keep replies short and use ₹ for rupee amounts."
)

_AFFIRMATIVE_WORDS = {"yes", "y", "yeah", "yep", "yup", "confirm", "confirmed", "sure", "ok", "okay", "go", "do"}
_NEGATIVE_WORDS = {"no", "n", "nope", "nah", "cancel", "stop", "don't", "dont", "never"}


def is_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


def _model() -> str:
    return os.getenv("CHATBOT_MODEL", "claude-sonnet-5")


def _normalize(text: str) -> str:
    return text.strip().lower().rstrip(".!")


def _word_set(text: str) -> set[str]:
    return set(_normalize(text).split())


def _looks_affirmative(text: str) -> bool:
    words = _word_set(text)
    # "yes thanks" -> affirmative; "please don't" -> not affirmative even
    # though it has no explicit "no", since it also carries a negative word;
    # a message with BOTH signals (or neither) falls through to the
    # ambiguous re-ask instead of guessing.
    return bool(words & _AFFIRMATIVE_WORDS) and not (words & _NEGATIVE_WORDS)


def _looks_negative(text: str) -> bool:
    words = _word_set(text)
    return bool(words & _NEGATIVE_WORDS) and not (words & _AFFIRMATIVE_WORDS)


def _narrate_write_result(name: str, result: dict) -> str:
    """Deterministic, not another model call — the action already happened;
    narrating it doesn't need judgment, and skipping a second API round
    trip keeps this fast and keeps the model from editorializing about an
    action it didn't just decide to take in this exact moment.

    Branches by tool name FIRST, not by status alone — different tools
    shape a "not_found" result differently (an invoice-keyed tool returns
    invoice_no, a customer-keyed one returns query), so a single generic
    "status == not_found" check across all of them would mislabel one."""
    status = result.get("status")

    if name in ("generate_payment_link", "send_reminder_email", "resolve_case"):
        if status == "not_found":
            return f"Couldn't find an invoice numbered {result.get('invoice_no')}."
        if status == "not_open":
            return f"Invoice {result.get('invoice_no')}'s case is already {result.get('case_status')} — nothing to send."

    if name == "generate_payment_link":
        if result.get("unavailable"):
            return (
                f"Tried to generate a link for invoice {result.get('invoice_no')}, but it couldn't be "
                "created (amount limit or account quota) — an honest note will show instead of a dead link."
            )
        return f"Done — payment link for invoice {result.get('invoice_no')}: {result.get('pay_link_url')}"

    if name == "send_reminder_email":
        return f"Done — dispatch outcome for invoice {result.get('invoice_no')}: {result.get('outcome')}."

    if name == "flag_dispute":
        if status == "not_found":
            return f"Couldn't find a customer matching '{result.get('query')}'."
        if status == "ambiguous":
            return f"That name matches more than one account: {', '.join(result.get('candidates', []))}. Please be more specific."
        return f"Done — paused {result.get('cases_paused')} case(s) for {result.get('customer_name')} pending review."

    if name == "resolve_case":
        if status == "not_reopenable":
            return f"Invoice {result.get('invoice_no')}'s case is {result.get('case_status')} — only a paused or exhausted case can be reopened."
        if status == "already_terminal":
            return f"Invoice {result.get('invoice_no')}'s case is already {result.get('case_status')}."
        if result.get("action") == "reopen":
            return f"Done — invoice {result.get('invoice_no')}'s case is reopened and will resume escalation."
        return f"Done — invoice {result.get('invoice_no')}'s case is closed."

    return f"Done: {result}"


@dataclass
class ChatTurnResult:
    reply: str
    messages: list = field(default_factory=list)
    pending_action: dict | None = None


def chat(session: Session, messages: list, pending_action: dict | None, user_text: str) -> ChatTurnResult:
    # A pending WRITE action gates everything else — the person's next
    # message is interpreted as yes/no for THAT action, not as a new
    # question, until it's resolved one way or the other. Nothing about
    # this exchange goes back into the model's own message history (see
    # module docstring) — Claude never "sees" that it asked for
    # confirmation, so there's no dangling tool_use to account for later.
    if pending_action is not None:
        if _looks_affirmative(user_text):
            result = execute_tool(session, pending_action["name"], pending_action["args"])
            reply = _narrate_write_result(pending_action["name"], result)
            return ChatTurnResult(reply=reply, messages=messages, pending_action=None)
        if _looks_negative(user_text):
            return ChatTurnResult(reply="Okay, cancelled — I won't do that.", messages=messages, pending_action=None)
        description = describe_pending_action(pending_action["name"], pending_action["args"])
        return ChatTurnResult(
            reply=f"I still need a yes or no — should I {description}?",
            messages=messages, pending_action=pending_action,
        )

    if not is_configured():
        return ChatTurnResult(
            reply="The chatbot isn't configured yet — set ANTHROPIC_API_KEY to enable it.",
            messages=messages, pending_action=None,
        )

    messages = [*messages, {"role": "user", "content": user_text}]
    client = get_client()
    response = client.messages.create(model=_model(), max_tokens=1024, system=_SYSTEM_PROMPT, tools=TOOL_SCHEMAS, messages=messages)

    tool_use_block = next((b for b in response.content if b.type == "tool_use"), None)

    if tool_use_block is None:
        reply_text = "".join(b.text for b in response.content if b.type == "text")
        messages.append({"role": "assistant", "content": response.content})
        return ChatTurnResult(reply=reply_text or "I'm not sure how to answer that.", messages=messages, pending_action=None)

    if tool_use_block.name in WRITE_TOOLS:
        # Deliberately NOT appended to `messages` — see module docstring.
        pending = {"name": tool_use_block.name, "args": tool_use_block.input}
        description = describe_pending_action(tool_use_block.name, tool_use_block.input)
        return ChatTurnResult(
            reply=f"About to {description}. Reply 'yes' to confirm, or 'no' to cancel.",
            messages=messages, pending_action=pending,
        )

    # READ tool: safe to run immediately, then let the model narrate it.
    result = execute_tool(session, tool_use_block.name, tool_use_block.input)
    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_block.id, "content": json.dumps(result)}]})

    response2 = client.messages.create(model=_model(), max_tokens=1024, system=_SYSTEM_PROMPT, tools=TOOL_SCHEMAS, messages=messages)
    reply_text = "".join(b.text for b in response2.content if b.type == "text")
    messages.append({"role": "assistant", "content": response2.content})
    return ChatTurnResult(reply=reply_text or "Done.", messages=messages, pending_action=None)

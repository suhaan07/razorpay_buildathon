import app.chatbot.agent as agent
from app.data.models import Case


class FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, name, input, id="tool_1"):  # noqa: A002
        self.name = name
        self.input = input
        self.id = id


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        # snapshot `messages` now — the caller mutates that same list object
        # further after this returns (appending the reply), same as the
        # real SDK call already sent its payload before that happens
        snapshot = {**kwargs, "messages": list(kwargs["messages"])}
        self.calls.append(snapshot)
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def _configure(monkeypatch, responses):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    fake_client = FakeClient(responses)
    monkeypatch.setattr(agent, "get_client", lambda: fake_client)
    return fake_client


def test_not_configured_replies_gracefully_without_calling_the_client(session, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = agent.chat(session, [], None, "what does Acme owe")
    assert "not configured" in result.reply.lower() or "isn't configured" in result.reply.lower()
    assert result.pending_action is None


def test_plain_text_reply_no_tool_call(session, monkeypatch):
    _configure(monkeypatch, [FakeResponse([FakeTextBlock("Hi, how can I help?")])])
    result = agent.chat(session, [], None, "hello")
    assert result.reply == "Hi, how can I help?"
    assert result.pending_action is None


def test_read_tool_executes_immediately_and_model_narrates(session, monkeypatch, make_customer, make_invoice):
    customer = make_customer(name="Chat Read Co")
    make_invoice(customer=customer, invoice_no="CR-1", outstanding=10_000.0)

    client = _configure(monkeypatch, [
        FakeResponse([FakeToolUseBlock("get_account_status", {"customer_name": "Chat Read Co"}, id="t1")]),
        FakeResponse([FakeTextBlock("Chat Read Co owes ₹10,000.")]),
    ])

    result = agent.chat(session, [], None, "what does Chat Read Co owe")
    assert result.reply == "Chat Read Co owes ₹10,000."
    assert result.pending_action is None
    assert len(client.messages.calls) == 2  # one to get the tool call, one to narrate the result

    # the tool_result actually contains real data, not a guess
    second_call_messages = client.messages.calls[1]["messages"]
    tool_result_content = second_call_messages[-1]["content"][0]["content"]
    assert "10000" in tool_result_content or "10,000" in tool_result_content or "Chat Read Co" in tool_result_content


def test_write_tool_does_not_execute_without_confirmation(session, monkeypatch, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="CW-1")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    client = _configure(monkeypatch, [
        FakeResponse([FakeToolUseBlock("generate_payment_link", {"invoice_no": "CW-1"}, id="t1")]),
    ])

    result = agent.chat(session, [], None, "generate a payment link for CW-1")
    assert "confirm" in result.reply.lower()
    assert result.pending_action == {"name": "generate_payment_link", "args": {"invoice_no": "CW-1"}}
    assert len(client.messages.calls) == 1  # never made a second call to narrate — nothing ran yet

    session.refresh(case)
    assert case.pay_link_id is None  # definitely not executed


def test_confirming_a_pending_write_action_executes_it(session, monkeypatch, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="CW-2")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    _configure(monkeypatch, [])  # no client calls expected for this turn at all
    pending = {"name": "generate_payment_link", "args": {"invoice_no": "CW-2"}}

    result = agent.chat(session, [], pending, "yes")
    assert result.pending_action is None
    assert "CW-2" in result.reply

    session.refresh(case)
    assert case.pay_link_id is not None  # actually executed this time


def test_negation_phrases_are_detected_correctly():
    assert agent._looks_negative("no thanks") is True
    assert agent._looks_negative("please don't") is True
    assert agent._looks_negative("nah") is True
    assert agent._looks_affirmative("no thanks") is False


def test_affirmation_phrases_are_detected_correctly():
    assert agent._looks_affirmative("yes please") is True
    assert agent._looks_affirmative("yeah go ahead") is True
    assert agent._looks_negative("yes please") is False


def test_conflicting_or_unclear_signals_are_neither():
    # both an affirmative and a negative word present -> don't guess
    assert agent._looks_affirmative("yes actually no wait") is False
    assert agent._looks_negative("yes actually no wait") is False
    assert agent._looks_affirmative("what will this cost") is False
    assert agent._looks_negative("what will this cost") is False


def test_declining_a_pending_write_action_does_not_execute_it(session, monkeypatch, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="CW-3")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    _configure(monkeypatch, [])
    pending = {"name": "generate_payment_link", "args": {"invoice_no": "CW-3"}}

    result = agent.chat(session, [], pending, "no thanks")
    assert result.pending_action is None
    assert "cancel" in result.reply.lower()

    session.refresh(case)
    assert case.pay_link_id is None


def test_ambiguous_reply_to_pending_action_re_asks_without_executing(session, monkeypatch, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, invoice_no="CW-4")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="0-15")
    session.add(case)
    session.commit()

    _configure(monkeypatch, [])
    pending = {"name": "generate_payment_link", "args": {"invoice_no": "CW-4"}}

    result = agent.chat(session, [], pending, "what will this cost")
    assert result.pending_action == pending  # still pending, unresolved
    assert "yes or no" in result.reply.lower()

    session.refresh(case)
    assert case.pay_link_id is None


def test_send_reminder_email_write_tool_also_gated(session, monkeypatch, make_invoice):
    invoice = make_invoice(outstanding=10_000.0, due_date=None, invoice_no="CW-5")
    case = Case(invoice_id=invoice.id, customer_id=invoice.customer_id, bucket="Unclassified")
    session.add(case)
    session.commit()

    _configure(monkeypatch, [
        FakeResponse([FakeToolUseBlock("send_reminder_email", {"invoice_no": "CW-5"}, id="t1")]),
    ])
    result = agent.chat(session, [], None, "send a reminder for CW-5")
    assert result.pending_action["name"] == "send_reminder_email"

    session.refresh(case)
    assert case.playbook_name is None  # not dispatched yet

    confirmed = agent.chat(session, result.messages, result.pending_action, "confirm")
    session.refresh(case)
    assert case.playbook_name is not None  # dispatched now
    assert confirmed.pending_action is None


def test_narrate_flag_dispute_not_found_does_not_mention_invoice_none():
    # regression test: flag_dispute's not_found result is keyed by "query"
    # (a customer name lookup), not "invoice_no" like the other write tools
    # — the generic branch must not misreport this as a missing invoice.
    reply = agent._narrate_write_result("flag_dispute", {"status": "not_found", "query": "Ghost Co"})
    assert "Ghost Co" in reply
    assert "invoice" not in reply.lower()


def test_narrate_flag_dispute_ambiguous():
    reply = agent._narrate_write_result("flag_dispute", {"status": "ambiguous", "candidates": ["Acme Mumbai", "Acme Delhi"]})
    assert "Acme Mumbai" in reply and "Acme Delhi" in reply


def test_narrate_flag_dispute_success():
    reply = agent._narrate_write_result("flag_dispute", {"status": "ok", "customer_name": "Acme", "cases_paused": 3})
    assert "Acme" in reply and "3" in reply


def test_narrate_resolve_case_reopen_and_close():
    reopen_reply = agent._narrate_write_result("resolve_case", {"status": "ok", "invoice_no": "INV-1", "action": "reopen"})
    assert "INV-1" in reopen_reply and "reopen" in reopen_reply.lower()

    close_reply = agent._narrate_write_result("resolve_case", {"status": "ok", "invoice_no": "INV-2", "action": "close"})
    assert "INV-2" in close_reply and "closed" in close_reply.lower()


def test_narrate_resolve_case_not_reopenable_and_already_terminal():
    not_reopenable = agent._narrate_write_result("resolve_case", {"status": "not_reopenable", "invoice_no": "INV-3", "case_status": "open"})
    assert "INV-3" in not_reopenable and "open" in not_reopenable

    terminal = agent._narrate_write_result("resolve_case", {"status": "already_terminal", "invoice_no": "INV-4", "case_status": "exhausted"})
    assert "INV-4" in terminal and "exhausted" in terminal


def test_flag_dispute_write_tool_gated_end_to_end(session, monkeypatch, make_customer, make_invoice):
    customer = make_customer(name="Chat Dispute Co")
    invoice = make_invoice(customer=customer, invoice_no="CD-1", outstanding=10_000.0)
    case = Case(invoice_id=invoice.id, customer_id=customer.id, bucket="0-15")
    session.add(case)
    session.commit()

    _configure(monkeypatch, [
        FakeResponse([FakeToolUseBlock("flag_dispute", {"customer_name": "Chat Dispute Co", "reason": "wrong invoice"}, id="t1")]),
    ])
    result = agent.chat(session, [], None, "flag Chat Dispute Co as disputed")
    assert result.pending_action["name"] == "flag_dispute"

    session.refresh(case)
    assert case.status == "open"  # not paused yet — still pending confirmation

    confirmed = agent.chat(session, result.messages, result.pending_action, "yes")
    session.refresh(case)
    assert case.status == "paused"
    assert confirmed.pending_action is None

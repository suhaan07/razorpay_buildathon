from app.router.intent import (
    classify_report_type,
    extract_customer_name,
    is_dispute_message,
    is_promise_message,
    parse_dispute_message,
    parse_message,
    parse_promise_message,
)


def test_classify_payment_schedule():
    assert classify_report_type("Give me a weekly payment schedule for Acme Pvt Ltd") == "payment_schedule"


def test_classify_collection_followup():
    assert classify_report_type("Give me a weekly collection follow-up for Acme Pvt Ltd") == "collection_followup"


def test_collection_followup_checked_before_generic_payment():
    # phrasing that mentions "payment" but is clearly a follow-up request
    # must not be misclassified as payment_schedule
    text = "weekly collection follow-up with payment status for Acme"
    assert classify_report_type(text) == "collection_followup"


def test_classify_unrecognized_returns_none():
    assert classify_report_type("what time is it") is None


def test_extract_customer_name_basic():
    assert extract_customer_name("Give me a weekly payment schedule for Gamma Technologies") == "Gamma Technologies"


def test_extract_customer_name_strips_noise_words():
    assert extract_customer_name("weekly payment schedule for Gamma Technologies please") == "Gamma Technologies"


def test_extract_customer_name_missing_returns_none():
    assert extract_customer_name("give me a weekly payment schedule") is None


def test_parse_message_combines_both():
    parsed = parse_message("Give me a weekly collection follow-up for Omicron Traders")
    assert parsed.report_type == "collection_followup"
    assert parsed.customer_name == "Omicron Traders"


def test_is_promise_message_detects_keyword():
    assert is_promise_message("Promise to pay for Acme by Friday") is True
    assert is_promise_message("PROMISE TO PAY for Acme by Friday") is True
    assert is_promise_message("Give me a weekly payment schedule for Acme") is False


def test_parse_promise_message_basic():
    parsed = parse_promise_message("Promise to pay for Acme Pvt Ltd by Friday")
    assert parsed.customer_name == "Acme Pvt Ltd"
    assert parsed.date_phrase == "Friday"


def test_parse_promise_message_numeric_date():
    parsed = parse_promise_message("Promise to pay for Gamma Technologies by 30-08-2026")
    assert parsed.customer_name == "Gamma Technologies"
    assert parsed.date_phrase == "30-08-2026"


def test_parse_promise_message_customer_name_containing_by():
    # the split must use the LAST " by ", not the first, so a customer name
    # containing " by " as a substring doesn't get truncated
    parsed = parse_promise_message("Promise to pay for Stand by Me Corp by tomorrow")
    assert parsed.customer_name == "Stand by Me Corp"
    assert parsed.date_phrase == "tomorrow"


def test_parse_promise_message_missing_by_clause_returns_none_date():
    parsed = parse_promise_message("Promise to pay for Acme Pvt Ltd")
    assert parsed.customer_name is None
    assert parsed.date_phrase is None


def test_parse_promise_message_missing_for_clause_returns_none():
    parsed = parse_promise_message("Promise to pay by Friday")
    assert parsed.customer_name is None
    assert parsed.date_phrase is None


def test_parse_promise_message_trailing_punctuation_stripped():
    parsed = parse_promise_message("Promise to pay for Acme by Friday.")
    assert parsed.date_phrase == "Friday"


def test_is_dispute_message_detects_keyword():
    assert is_dispute_message("Dispute for Acme: wrong amount") is True
    assert is_dispute_message("DISPUTE for Acme") is True
    assert is_dispute_message("Give me a weekly payment schedule for Acme") is False


def test_parse_dispute_message_with_reason():
    parsed = parse_dispute_message("Dispute for Acme Pvt Ltd: invoice amount is wrong")
    assert parsed.customer_name == "Acme Pvt Ltd"
    assert parsed.reason == "invoice amount is wrong"


def test_parse_dispute_message_without_reason():
    parsed = parse_dispute_message("Dispute for Acme Pvt Ltd")
    assert parsed.customer_name == "Acme Pvt Ltd"
    assert parsed.reason is None


def test_parse_dispute_message_missing_for_clause():
    parsed = parse_dispute_message("Dispute please")
    assert parsed.customer_name is None
    assert parsed.reason is None


def test_parse_dispute_message_reason_with_multiple_colons():
    parsed = parse_dispute_message("Dispute for Acme: they say: already paid last week")
    assert parsed.customer_name == "Acme"
    assert parsed.reason == "they say: already paid last week"


def test_parse_dispute_message_trailing_punctuation_stripped():
    parsed = parse_dispute_message("Dispute for Acme.")
    assert parsed.customer_name == "Acme"

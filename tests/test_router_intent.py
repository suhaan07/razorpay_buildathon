from app.router.intent import classify_report_type, extract_customer_name, parse_message


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

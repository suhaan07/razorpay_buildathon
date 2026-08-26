from app.billing import get_or_create_consolidated_link


def test_no_link_when_nothing_owed(session, make_customer):
    customer = make_customer()
    assert get_or_create_consolidated_link(session, customer, 0.0) is None


def test_creates_and_caches_link(session, make_customer):
    customer = make_customer()
    first = get_or_create_consolidated_link(session, customer, 50_000.0)
    assert first is not None
    session.refresh(customer)
    assert customer.consolidated_pay_link_url == first["short_url"]
    assert customer.consolidated_pay_link_amount == 50_000.0

    second = get_or_create_consolidated_link(session, customer, 50_000.0)
    assert second["short_url"] == first["short_url"]  # same amount -> reused, not recreated


def test_regenerates_when_amount_changes(session, make_customer):
    customer = make_customer()
    first = get_or_create_consolidated_link(session, customer, 50_000.0)
    second = get_or_create_consolidated_link(session, customer, 30_000.0)  # e.g. a partial payment landed
    assert second["short_url"] != first["short_url"]
    session.refresh(customer)
    assert customer.consolidated_pay_link_amount == 30_000.0

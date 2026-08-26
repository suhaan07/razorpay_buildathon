from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from app.db import Base, sync_missing_columns


def test_sync_missing_columns_adds_new_columns_without_losing_data():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    # Simulate a database created before consolidated_pay_link_* existed —
    # a bare-bones customers table with just a few of the real columns.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name VARCHAR(255) NOT NULL UNIQUE,
                normalized_name VARCHAR(255) NOT NULL
            )
        """))
        conn.execute(text("INSERT INTO customers (id, name, normalized_name) VALUES (1, 'Acme Pvt Ltd', 'acme pvt ltd')"))

    sync_missing_columns(target_engine=engine)

    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("customers")}
    assert "consolidated_pay_link_id" in columns
    assert "consolidated_pay_link_url" in columns
    assert "consolidated_pay_link_amount" in columns
    assert "spoc_email" in columns

    with engine.connect() as conn:
        row = conn.execute(text("SELECT name, consolidated_pay_link_id FROM customers WHERE id=1")).fetchone()
        assert row[0] == "Acme Pvt Ltd"  # original data untouched
        assert row[1] is None  # new column, no backfill, just NULL


def test_sync_missing_columns_is_a_no_op_on_a_fresh_database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)

    sync_missing_columns(target_engine=engine)  # must not raise on a schema that's already current

    inspector = inspect(engine)
    assert "consolidated_pay_link_id" in {col["name"] for col in inspector.get_columns("customers")}


def test_sync_missing_columns_skips_tables_that_do_not_exist_yet():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sync_missing_columns(target_engine=engine)  # no tables at all yet — must not raise

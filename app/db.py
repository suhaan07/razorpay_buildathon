import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv()

logger = logging.getLogger("recovery.db")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./recovery.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_session() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def sync_missing_columns(target_engine=None) -> None:
    """A model gaining a new column (which has happened repeatedly during
    this build) otherwise means every existing recovery.db must be deleted
    and recreated — create_all() only creates missing tables, never adds
    columns to ones that already exist. This patches the gap for SQLite's
    dev/demo setup with plain ALTER TABLE ADD COLUMN, so the app keeps
    working against a database from an earlier schema version without
    losing its data. Not a real migration system (no renames, no drops,
    no data backfill) — just enough for additive model changes."""

    target_engine = target_engine or engine
    inspector = inspect(target_engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(target_engine.dialect)
            logger.warning("adding missing column %s.%s (%s) to an existing database", table.name, column.name, col_type)
            with target_engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))

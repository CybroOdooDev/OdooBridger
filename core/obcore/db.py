"""Database engine, session and the declarative base."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import DateTime, String, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from obcore.config import settings


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class UUIDPk:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


def _engine(url: str):
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False, "timeout": 20}, future=True)
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20, future=True)


engine = _engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def create_schema() -> None:
    import obcore.models  # noqa: F401 — registers the tables

    Base.metadata.create_all(engine)
    add_missing_columns()


def add_missing_columns() -> None:
    """Add columns new to the model to tables that already exist.

    ``create_all`` only creates missing tables. Until the core has proper
    migrations, a new column must be nullable or carry a server default, and
    this adds it (plain ``ALTER TABLE ... ADD COLUMN``; foreign keys and
    indexes on such a column are left to the model's next real migration).
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateColumn

    with engine.begin() as conn:
        inspector = inspect(conn)
        existing_tables = set(inspector.get_table_names())
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                if not column.nullable and column.server_default is None:
                    raise RuntimeError(f"Can't add NOT NULL column {table.name}.{column.name} without a default")
                ddl = CreateColumn(column).compile(dialect=engine.dialect)
                # Only the column itself: the FK constraint is not added in place.
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {str(ddl).split(" REFERENCES")[0]}'))

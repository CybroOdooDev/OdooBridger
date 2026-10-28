"""Bring the core's database to the current schema, whatever state it is in.

* **Empty**: ``upgrade head`` builds everything from the migrations.
* **Already versioned**: ``upgrade head`` applies what is missing.
* **Legacy**: tables exist (made by the old ``create_all`` + add-missing-columns
  startup) but Alembic has never seen the database. It is topped up the old
  way one last time (so it matches the current models), cleared of what the
  removed OdooBridger Agent left behind, checked, and *stamped* at head. Later
  migrations then apply to it like to any other database.

Runs at every start (``obcore.main``) and from ``tools/db_upgrade.py``. On
PostgreSQL it holds an advisory lock, so instances starting together take turns.

Making a schema change:
    1. edit the model
    2. cd core && alembic revision --autogenerate -m "what changed"
    3. read the file it wrote: autogenerate is a draft, not an authority
    4. keep it compatible with the previous release (a rollback does not undo it)
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.schema import CreateColumn

import obcore.models  # noqa: F401 — registers every table on Base.metadata
from obcore.db import Base

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
#: Arbitrary and fixed (BioBridge uses a different one): concurrent starts take turns.
ADVISORY_LOCK_KEY = 727275


def alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def is_legacy(engine: Engine) -> bool:
    """Has the core's tables but no Alembic version table."""
    tables = set(inspect(engine).get_table_names())
    return "alembic_version" not in tables and bool(tables & set(Base.metadata.tables))


def missing_from_database(connection) -> list[str]:
    """Tables and columns the models have and the database lacks (what would
    break the code). Extra tables or columns in the database are not counted."""
    diffs = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    out = []
    for diff in diffs:
        if isinstance(diff, tuple) and diff[0] == "add_table":
            out.append(f"table {diff[1].name}")
        elif isinstance(diff, tuple) and diff[0] == "add_column":
            out.append(f"column {diff[2]}.{diff[3].name}")
    return out


def _add_missing_columns(connection) -> None:
    """The old startup top-up: add model columns missing from existing tables
    (nullable or with a server default only)."""
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            if not column.nullable and column.server_default is None:
                raise RuntimeError(f"Can't add NOT NULL column {table.name}.{column.name} without a default")
            ddl = CreateColumn(column).compile(dialect=connection.dialect)
            connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {str(ddl).split(' REFERENCES')[0]}"))


def drop_agent_leftovers(op) -> None:
    """Drop ``relay_agent`` and ``odoo_connection.relay_agent_id`` (with its
    index and foreign key) where they exist. Used by migration 0002 and when
    adopting a legacy database."""
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "odoo_connection" in tables and "relay_agent_id" in {c["name"] for c in inspector.get_columns("odoo_connection")}:
        foreign_keys = [fk["name"] for fk in inspector.get_foreign_keys("odoo_connection")
                        if fk.get("referred_table") == "relay_agent" and fk.get("name")]
        indexes = [ix["name"] for ix in inspector.get_indexes("odoo_connection")
                   if ix.get("column_names") == ["relay_agent_id"]]
        with op.batch_alter_table("odoo_connection") as batch:
            for name in foreign_keys:
                batch.drop_constraint(name, type_="foreignkey")
            for name in indexes:
                batch.drop_index(name)
            batch.drop_column("relay_agent_id")
    if "relay_agent" in tables:
        op.drop_table("relay_agent")


def _adopt_legacy(connection) -> None:
    from alembic.operations import Operations

    log.info("Adopting an existing database that predates migrations")
    Base.metadata.create_all(connection)          # tables new since it was made
    _add_missing_columns(connection)
    drop_agent_leftovers(Operations(MigrationContext.configure(connection)))
    missing = missing_from_database(connection)
    if missing:
        raise RuntimeError("The existing database still lacks: " + ", ".join(missing))


def upgrade(engine: Engine) -> str:
    """Bring ``engine``'s database to head; return the revision it is at."""
    config = alembic_config()
    with engine.connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        if postgres:
            connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
            connection.commit()
        try:
            config.attributes["connection"] = connection
            if is_legacy(engine):
                _adopt_legacy(connection)
                connection.commit()
                # It now matches the current models: everything up to head is in it.
                command.stamp(config, "head")
                connection.commit()
            command.upgrade(config, "head")
            connection.commit()
        finally:
            if postgres:
                connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
                connection.commit()
    return current_revision(engine) or ""

"""The core's migrations: a fresh database, an upgrade, and adopting one made
before migrations existed (including the agent's leftovers)."""

from __future__ import annotations

import os
import tempfile

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from obcore import migrations
from obcore.db import Base


def _engine():
    path = os.path.join(tempfile.mkdtemp(prefix="ob-mig-"), "db.sqlite")
    return create_engine(f"sqlite:///{path}")


def _diffs(engine):
    with engine.connect() as conn:
        return compare_metadata(MigrationContext.configure(conn), Base.metadata)


def test_a_fresh_database_matches_the_models_exactly():
    engine = _engine()
    assert migrations.upgrade(engine) == migrations.head_revision()
    # No model change is missing a migration (CI runs the same check).
    assert _diffs(engine) == []
    # Running again is a no-op.
    assert migrations.upgrade(engine) == migrations.head_revision()


def test_a_database_from_before_migrations_is_adopted_and_cleaned_up():
    engine = _engine()
    # What the old startup made: tables from the models, minus a newer column,
    # minus a newer table, plus what the OdooBridger Agent left behind.
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alert_state"))
        conn.execute(text("ALTER TABLE odoo_connection DROP COLUMN failing_since"))
        conn.execute(text("CREATE TABLE relay_agent (id VARCHAR(32) PRIMARY KEY, name VARCHAR(120))"))
        conn.execute(text("ALTER TABLE odoo_connection ADD COLUMN relay_agent_id VARCHAR(32) REFERENCES relay_agent(id)"))
        conn.execute(text("CREATE INDEX ix_odoo_connection_relay_agent_id ON odoo_connection (relay_agent_id)"))
        conn.execute(text("INSERT INTO service (id, slug, name, base_url, sso_callback_path, is_active, is_public, "
                          "uses_odoo, sort_order, created_at, updated_at) VALUES ('s1', 'keep', 'Keep me', "
                          "'http://x', '/', 1, 1, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    assert migrations.is_legacy(engine)

    assert migrations.upgrade(engine) == migrations.head_revision()

    tables = set(inspect(engine).get_table_names())
    assert "alert_state" in tables and "relay_agent" not in tables
    columns = {c["name"] for c in inspect(engine).get_columns("odoo_connection")}
    assert "failing_since" in columns and "relay_agent_id" not in columns
    with engine.connect() as conn:          # the data stayed
        assert conn.execute(text("SELECT name FROM service WHERE id = 's1'")).scalar() == "Keep me"
    assert not migrations.is_legacy(engine)

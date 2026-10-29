"""Database locks for work that must happen once, however many core
processes run (several uvicorn workers, or several containers).

PostgreSQL advisory locks; on SQLite (tests, local runs) there is only ever
one process, so they are no-ops that always succeed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from obcore.db import engine

#: Fixed, arbitrary keys; the migration lock is obcore.migrations.ADVISORY_LOCK_KEY.
BOOT = 727276          # startup bootstrap: static staff, catalog, signing key
SWEEP = 727277         # the background sweep (trials, provisioning, Odoo checks)
ALERTS = 727278        # the alert check


def _postgres() -> bool:
    return engine.dialect.name == "postgresql"


@contextmanager
def held(key: int) -> Iterator[None]:
    """Wait for ``key`` and hold it for the block (its own connection)."""
    if not _postgres():
        yield
        return
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        connection.commit()
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            connection.commit()


def try_for_transaction(db: Session, key: int) -> bool:
    """Take ``key`` for the rest of ``db``'s transaction if nobody holds it.
    False means another process is doing this work right now: skip it."""
    if not _postgres():
        return True
    return bool(db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key}).scalar())

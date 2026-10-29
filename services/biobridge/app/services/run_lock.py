"""One sync cycle per account at a time, across every BioBridge process.

A PostgreSQL session-level advisory lock, keyed by the tenant, held on its
own connection for as long as the cycle runs. Being a lock rather than a
row, it disappears with the process: a cycle killed by a deploy or a crash
does not block the account's syncs afterwards, and the "running" run it left
behind is recognisable as abandoned (nobody holds the lock) and closed.

On SQLite (tests, single-process local runs) it is a no-op that always
succeeds; ``sync_engine.active_run`` still guards there.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from app.db.session import engine

log = logging.getLogger(__name__)

#: First half of the two-part advisory key: "a BioBridge sync cycle".
CYCLE = 7272


class TenantRunLock:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self._conn = None
        self.held = False

    @staticmethod
    def supported() -> bool:
        return engine.dialect.name == "postgresql"

    def acquire(self) -> bool:
        """Take the lock if nobody holds it; False if a cycle is running."""
        if not self.supported():
            self.held = True
            return True
        conn = engine.connect()
        try:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:c, hashtext(:t))"),
                               {"c": CYCLE, "t": self.tenant_id}).scalar()
            conn.commit()
        except Exception:
            conn.invalidate()
            conn.close()
            raise
        if not got:
            conn.close()
            return False
        self._conn = conn
        self.held = True
        return True

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:c, hashtext(:t))"),
                         {"c": CYCLE, "t": self.tenant_id})
            conn.commit()
        except Exception:  # noqa: BLE001 — never hand a pooled connection back still locked
            log.warning("Could not release the sync lock for %s; dropping the connection", self.tenant_id)
            conn.invalidate()
        finally:
            conn.close()

    @classmethod
    def is_free(cls, tenant_id: str) -> bool:
        """Whether no cycle holds the tenant's lock right now."""
        lock = cls(tenant_id)
        if not lock.acquire():
            return False
        lock.release()
        return True

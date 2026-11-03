"""A sync interrupted by a crash must not block the account's next syncs."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.models import SyncRun, SyncStatus
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo
from tests.test_sync_engine import punch, run


def test_without_the_lock_a_leftover_running_run_blocks_the_next_sync(db, tenant, local_day, monkeypatch):
    """The situation on SQLite: the crashed process's run is still 'running'."""
    db.add(SyncRun(tenant_id=tenant.id, status=SyncStatus.running.value, triggered_by="scheduled",
                   started_at=datetime.now(timezone.utc), log=[]))
    db.commit()
    stuck = run(db, tenant, FakeOdoo(), [punch("a", "1001", local_day.replace(hour=8))], monkeypatch)
    assert stuck.status == "running" and stuck.punches_fetched == 0, "no sync ran: the stale run was returned"


def test_startup_closes_runs_left_by_a_stopped_process(db, tenant, local_day, monkeypatch):
    db.add(SyncRun(tenant_id=tenant.id, status=SyncStatus.running.value, triggered_by="scheduled",
                   started_at=datetime.now(timezone.utc), log=[]))
    db.commit()
    assert engine_mod.close_runs_left_by_a_stopped_process(db) == 1
    db.commit()
    leftover = db.scalars(select(SyncRun).where(SyncRun.status == "failed")).one()
    assert "Interrupted" in leftover.error_message
    fresh = run(db, tenant, FakeOdoo(), [punch("a", "1001", local_day.replace(hour=8))], monkeypatch)
    assert fresh.status == "success" and fresh.punches_new == 1

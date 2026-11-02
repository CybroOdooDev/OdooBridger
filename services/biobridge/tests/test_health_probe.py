"""Connections that stopped working are re-checked without waiting for a sync.

Three things are pinned here: a failure marks only the side that failed (an
Odoo that answers must not read "Degraded" because the biometric server is
down), the recheck turns a recovered connection back to Connected and clears
the failure streak, and a sign-in starts that recheck in the background.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import connections as api
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import ConnectionStatus, DeviceSource, OdooConnection, PunchRecord, PunchState, SyncRun
from app.schemas import TestResult as ProbeResult
from app.services import health_probe
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo
from tests.test_source_outage import DeadProvider


def _rows(db, tenant):
    odoo = db.scalars(select(OdooConnection).where(OdooConnection.tenant_id == tenant.id)).first()
    source = db.scalars(select(DeviceSource).where(DeviceSource.tenant_id == tenant.id)).first()
    return odoo, source


def _long_ago():
    return datetime.now(timezone.utc) - timedelta(minutes=30)


# --------------------------------------------------------------------------- #
# Only the side that failed is marked
# --------------------------------------------------------------------------- #
def test_a_biometric_outage_does_not_badge_a_working_odoo_degraded(db, tenant, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: DeadProvider())
    for _ in range(settings.max_consecutive_failures):
        engine_mod.SyncEngine(db, tenant, "test").run_cycle()

    odoo, source = _rows(db, tenant)
    db.refresh(odoo); db.refresh(source)
    assert source.status == ConnectionStatus.degraded.value
    assert odoo.status != ConnectionStatus.degraded.value


def test_an_odoo_failure_marks_odoo_and_leaves_the_sources(db, tenant):
    odoo, source = _rows(db, tenant)
    odoo.status = source.status = ConnectionStatus.connected.value
    tenant.consecutive_failures = settings.max_consecutive_failures
    engine_mod.SyncEngine(db, tenant, "test")._mark_degraded_if_needed(sources=False)
    assert odoo.status == ConnectionStatus.degraded.value
    assert source.status == ConnectionStatus.connected.value


# --------------------------------------------------------------------------- #
# The recheck
# --------------------------------------------------------------------------- #
@pytest.fixture
def probes(monkeypatch):
    """Both probes answer as set: ``probes.up`` True means the server is back."""
    state = type("State", (), {"up": True, "calls": []})()

    def fake(row, label):
        state.calls.append(label)
        row.last_checked_at = datetime.now(timezone.utc)
        if state.up:
            row.status = ConnectionStatus.connected.value
            return ProbeResult(ok=True, message="ok")
        row.status = ConnectionStatus.failed.value
        return ProbeResult(ok=False, message="down")

    monkeypatch.setattr(api, "_probe_odoo", lambda seat, row: fake(row, "odoo"))
    monkeypatch.setattr(api, "_probe_source", lambda seat, row: fake(row, "source"))
    return state


def _break(db, tenant, streak=7):
    odoo, source = _rows(db, tenant)
    for row in (odoo, source):
        row.status = ConnectionStatus.degraded.value
        row.last_checked_at = _long_ago()
    tenant.consecutive_failures = streak
    db.commit()
    return odoo, source


def test_recovered_connections_turn_connected_and_end_the_slow_lane(db, tenant, probes):
    odoo, source = _break(db, tenant)
    result = health_probe.recheck(db, tenant)
    assert (odoo.status, source.status) == (ConnectionStatus.connected.value,) * 2
    assert result["healthy"] and result["streak_cleared"]
    assert tenant.consecutive_failures == 0


def test_still_down_stays_down_and_keeps_the_streak(db, tenant, probes):
    probes.up = False
    odoo, source = _break(db, tenant)
    result = health_probe.recheck(db, tenant)
    assert odoo.status == source.status == ConnectionStatus.failed.value
    assert not result["healthy"] and tenant.consecutive_failures == 7


def test_one_side_back_and_the_other_not_keeps_the_streak(db, tenant, monkeypatch):
    odoo, source = _break(db, tenant)

    def odoo_ok(seat, row):
        row.status = ConnectionStatus.connected.value
        row.last_checked_at = datetime.now(timezone.utc)
        return ProbeResult(ok=True, message="ok")

    def source_down(seat, row):
        row.status = ConnectionStatus.failed.value
        row.last_checked_at = datetime.now(timezone.utc)
        return ProbeResult(ok=False, message="down")

    monkeypatch.setattr(api, "_probe_odoo", odoo_ok)
    monkeypatch.setattr(api, "_probe_source", source_down)
    health_probe.recheck(db, tenant)
    assert odoo.status == ConnectionStatus.connected.value      # shown as it really is
    assert source.status == ConnectionStatus.failed.value
    assert tenant.consecutive_failures == 7


def test_a_probe_that_reaches_the_server_but_is_refused_is_not_recovery(db, tenant, monkeypatch):
    """Connected, but the Odoo user cannot create attendance: not healthy."""
    _break(db, tenant)

    def refused(seat, row):
        row.status = ConnectionStatus.connected.value
        row.last_checked_at = datetime.now(timezone.utc)
        return ProbeResult(ok=False, message="cannot create attendance")

    monkeypatch.setattr(api, "_probe_odoo", refused)
    monkeypatch.setattr(api, "_probe_source", refused)
    result = health_probe.recheck(db, tenant)
    assert not result["healthy"] and tenant.consecutive_failures == 7


def test_a_connection_checked_a_moment_ago_is_left_alone(db, tenant, probes):
    odoo, source = _break(db, tenant)
    odoo.last_checked_at = source.last_checked_at = datetime.now(timezone.utc)
    db.commit()
    result = health_probe.recheck(db, tenant)
    assert probes.calls == [] and result["probed"] == 0
    assert tenant.consecutive_failures == 7


def test_healthy_connections_cost_nothing_and_a_streak_with_another_cause_stays(db, tenant, probes):
    odoo, source = _rows(db, tenant)
    odoo.status = source.status = ConnectionStatus.connected.value
    tenant.consecutive_failures = 3
    db.commit()
    result = health_probe.recheck(db, tenant)
    assert probes.calls == [] and result["probed"] == 0
    assert tenant.consecutive_failures == 3


def test_only_accounts_in_trouble_are_listed_for_the_recovery_tick(db, tenant):
    odoo, source = _rows(db, tenant)
    odoo.status = source.status = ConnectionStatus.connected.value
    db.commit()
    assert health_probe.tenants_in_trouble(db) == []
    source.status = ConnectionStatus.failed.value
    db.commit()
    assert health_probe.tenants_in_trouble(db) == [tenant.id]


def test_a_catch_up_sync_is_wanted_after_a_failed_run_or_with_punches_waiting(db, tenant):
    assert not health_probe._catch_up_needed(db, tenant)
    db.add(SyncRun(tenant_id=tenant.id, status="failed", triggered_by="schedule",
                   started_at=datetime.now(timezone.utc)))
    db.commit()
    assert health_probe._catch_up_needed(db, tenant)


# --------------------------------------------------------------------------- #
# Signing in starts it
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        c.Session = Session
        yield c
    app.dependency_overrides.clear()


def test_signing_in_rechecks_the_account_in_the_background(client, monkeypatch):
    seen = []
    monkeypatch.setattr(health_probe, "recheck_tenant", lambda tenant_id: seen.append(tenant_id))
    body = {"company_name": "Acme", "email": "owner@acme.com",
            "password": "a-long-enough-password", "timezone": "Asia/Kolkata"}
    assert client.post("/api/v1/auth/signup", json=body).status_code == 201
    r = client.post("/api/v1/auth/login", json={"email": body["email"], "password": body["password"]})
    assert r.status_code == 200, r.text
    assert len(seen) == 1 and seen[0]

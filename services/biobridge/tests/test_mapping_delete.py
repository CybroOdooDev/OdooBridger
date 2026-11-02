"""Deleting an unmatched badge from the list."""

from __future__ import annotations

from sqlalchemy import select

from app.db.session import get_db
from app.main import app
from app.models import EmployeeMapping, MappingStatus, PunchRecord
from tests.test_api_isolation import auth, client, signup  # noqa: F401


def _tenant_id(client, token):
    return client.get("/api/v1/tenant", headers=auth(token)).json()["id"]


def test_unmatched_badge_can_be_deleted_and_stays_gone(client):
    token = signup(client, "Acme", "owner@example.com")
    tid = _tenant_id(client, token)
    db = next(app.dependency_overrides[get_db]())
    m = EmployeeMapping(tenant_id=tid, emp_code="9001", status=MappingStatus.unmapped.value)
    db.add(m); db.commit()
    assert [x["emp_code"] for x in client.get("/api/v1/mappings", headers=auth(token)).json()] == ["9001"]

    r = client.delete(f"/api/v1/mappings/{m.id}", headers=auth(token))
    assert r.status_code == 200
    assert client.get("/api/v1/mappings", headers=auth(token)).json() == []
    db.expire_all()
    # Kept hidden so a later sync does not recreate it.
    assert db.get(EmployeeMapping, m.id).status == MappingStatus.removed.value


def test_a_matched_employee_cannot_be_deleted_this_way(client):
    token = signup(client, "Acme", "owner@example.com")
    tid = _tenant_id(client, token)
    db = next(app.dependency_overrides[get_db]())
    m = EmployeeMapping(tenant_id=tid, emp_code="9002", status=MappingStatus.mapped.value,
                        odoo_employee_id=5)
    db.add(m); db.commit()
    assert client.delete(f"/api/v1/mappings/{m.id}", headers=auth(token)).status_code == 400


def test_another_tenants_badge_is_not_deletable(client):
    a = signup(client, "A Co", "a@example.com")
    b = signup(client, "B Co", "b@example.org")
    db = next(app.dependency_overrides[get_db]())
    m = EmployeeMapping(tenant_id=_tenant_id(client, a), emp_code="1", status="unmapped")
    db.add(m); db.commit()
    assert client.delete(f"/api/v1/mappings/{m.id}", headers=auth(b)).status_code == 404


def _deleted_badge(client, token, code="9001"):
    tid = _tenant_id(client, token)
    db = next(app.dependency_overrides[get_db]())
    m = EmployeeMapping(tenant_id=tid, emp_code=code, status=MappingStatus.unmapped.value)
    db.add(m); db.commit()
    return db, tid, m.id


def _punch(db, tid, code, state, note=None, pid=1):
    from datetime import datetime, timezone

    from app.models import DeviceSource

    source = db.scalars(select(DeviceSource).where(DeviceSource.tenant_id == tid)).first()
    if source is None:
        source = DeviceSource(tenant_id=tid, name="BioTime", provider="biotime", connection_kind="server",
                              base_url="https://bt.example.test", username="u")
        db.add(source); db.flush()
    p = PunchRecord(tenant_id=tid, source_id=source.id, external_id=str(pid), emp_code=code,
                    process_state=state, error_message=note,
                    punch_time_utc=datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    db.add(p); db.commit()
    return p.id


def test_a_deleted_badge_can_be_restored_with_its_waiting_punches(client):
    token = signup(client, "Acme", "owner@example.com")
    db, tid, mid = _deleted_badge(client, token)
    waiting = _punch(db, tid, "9001", "unmapped", pid=1)
    other = _punch(db, tid, "9001", "skipped", "Mapping is ignored", pid=2)       # skipped for another reason
    client.delete(f"/api/v1/mappings/{mid}", headers=auth(token))
    assert [x["emp_code"] for x in client.get("/api/v1/mappings?status=removed", headers=auth(token)).json()] == ["9001"]

    r = client.post(f"/api/v1/mappings/{mid}/restore", headers=auth(token))
    assert r.status_code == 200 and "1 punch waiting" in r.json()["message"]
    db.expire_all()
    assert db.get(EmployeeMapping, mid).status == MappingStatus.unmapped.value
    assert db.get(PunchRecord, waiting).process_state == "unmapped" and db.get(PunchRecord, waiting).error_message is None
    assert db.get(PunchRecord, other).process_state == "skipped"               # not ours to bring back
    assert [x["emp_code"] for x in client.get("/api/v1/mappings", headers=auth(token)).json()] == ["9001"]
    assert client.get("/api/v1/mappings?status=removed", headers=auth(token)).json() == []


def test_only_a_deleted_badge_can_be_restored(client):
    token = signup(client, "Acme", "owner@example.com")
    _, _, mid = _deleted_badge(client, token)
    assert client.post(f"/api/v1/mappings/{mid}/restore", headers=auth(token)).status_code == 409
    assert client.post("/api/v1/mappings/nope/restore", headers=auth(token)).status_code == 404


def test_a_restored_badge_is_not_recreated_twice_and_can_be_deleted_again(client):
    token = signup(client, "Acme", "owner@example.com")
    _, _, mid = _deleted_badge(client, token)
    client.delete(f"/api/v1/mappings/{mid}", headers=auth(token))
    client.post(f"/api/v1/mappings/{mid}/restore", headers=auth(token))
    assert client.delete(f"/api/v1/mappings/{mid}", headers=auth(token)).status_code == 200

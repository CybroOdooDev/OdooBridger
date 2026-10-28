"""A tenant moving to another Odoo database lets go of the old one's employees."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.core.crypto import encrypt
from app.models import AttendanceRecord, EmployeeMapping, MappingStatus, OdooConnection
from app.services import odoo_switch, platform_link
from app.services.roster_import import _release_vanished
from tests.test_platform_mode import client, put, state  # noqa: F401 — the fixture

OLD = {"id": "a" * 32, "name": "Odoo", "url": "https://old.odoo.com", "db": "old", "login": "bot@old.com",
       "api_key": "k1", "version": "19.0", "api": "json2", "companies": [], "status": "connected"}
NEW = {**OLD, "id": "b" * 32, "url": "https://new.example.com", "db": "new", "login": "bot@new.com", "api_key": "k2"}


def _mappings(db, tenant):
    return {m.emp_code: m for m in db.scalars(select(EmployeeMapping).where(EmployeeMapping.tenant_id == tenant.id))}


def _seed(db, tenant):
    db.add_all([
        EmployeeMapping(tenant_id=tenant.id, emp_code="5", odoo_employee_id=1, odoo_employee_name="beevi",
                        status=MappingStatus.mapped.value, match_method="barcode",
                        last_punch_at=datetime(2026, 10, 27, 9), open_attendance_id=40,
                        open_check_in=datetime(2026, 10, 27, 9)),
        EmployeeMapping(tenant_id=tenant.id, emp_code="6001", odoo_employee_id=2, odoo_employee_name="marc",
                        status=MappingStatus.mapped.value, match_method="barcode"),
        EmployeeMapping(tenant_id=tenant.id, emp_code="77", status=MappingStatus.ignored.value),
        AttendanceRecord(tenant_id=tenant.id, emp_code="5", odoo_employee_id=1, odoo_attendance_id=40,
                         check_in=datetime(2026, 10, 27, 9)),
    ])
    db.flush()


def test_changing_the_odoo_database_releases_the_old_employees(db, tenant):
    conn = db.scalar(select(OdooConnection).where(OdooConnection.tenant_id == tenant.id))
    assert odoo_switch.ensure_current(db, tenant) is None          # first record of which Odoo, no switch
    _seed(db, tenant)
    # Same database, new key: nothing is released.
    conn.api_key_enc = encrypt("other", tenant.crypto_key)
    assert odoo_switch.ensure_current(db, tenant) is None
    # Another database.
    conn.url, conn.db_name = "https://new.example.com", "new"
    released = odoo_switch.ensure_current(db, tenant)
    assert released == {"removed": 1, "reset": 1, "detached": 1}
    rows = _mappings(db, tenant)
    assert "6001" not in rows                                       # only ever came from the old roster
    beevi = rows["5"]
    assert beevi.status == "unmapped" and beevi.odoo_employee_id is None and beevi.open_attendance_id is None
    assert rows["77"].status == "ignored"                           # the user's own decision stays
    record = db.scalar(select(AttendanceRecord))
    assert record.odoo_attendance_id is None and record.emp_code == "5"   # history kept, not tied to old ids
    assert odoo_switch.ensure_current(db, tenant) is None           # done once


def test_reconnecting_another_odoo_in_odoobridger_releases_the_old_one(client):
    put(client, state(odoo=OLD))
    db = client.Session()
    conn = db.scalar(select(OdooConnection))
    tenant_id = conn.tenant_id
    db.add(EmployeeMapping(tenant_id=tenant_id, emp_code="6001", odoo_employee_id=2, odoo_employee_name="marc",
                           status=MappingStatus.mapped.value))
    db.commit()
    db.close()
    # Disconnected, then a different Odoo connected (the user's report).
    put(client, state(odoo=None))
    put(client, state(odoo=NEW))
    db = client.Session()
    assert db.scalars(select(EmployeeMapping).where(EmployeeMapping.emp_code == "6001")).first() is None
    db.close()


def test_an_employee_deleted_in_odoo_is_let_go(db, tenant):
    _seed(db, tenant)
    rows = [m for m in _mappings(db, tenant).values() if m.status == "mapped"]
    _release_vanished(db, rows, {1: {"id": 1}})                     # employee #2 is gone from Odoo
    db.flush()
    remaining = _mappings(db, tenant)
    assert "6001" not in remaining and remaining["5"].status == "mapped"


def test_platform_link_takes_over_without_release_for_the_same_database(db, tenant):
    _seed(db, tenant)
    tenant.odoo_identity = odoo_switch.identity("https://acme.odoo.com", "acme")
    platform_link.apply_odoo(db, tenant, {**OLD, "url": "https://acme.odoo.com", "db": "acme", "api_key": "key"})
    assert _mappings(db, tenant)["6001"].status == "mapped"

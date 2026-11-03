"""After the Odoo database name changes, the same shift must not appear twice."""

from __future__ import annotations

from sqlalchemy import select, update

from app.models import AttendanceRecord
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo
from tests.test_sync_engine import punch, run


def test_orphaned_row_is_merged_into_its_odoo_twin(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    run(db, tenant, odoo, [punch("in", "1001", local_day.replace(hour=1), direction=True)], monkeypatch)
    (row,) = db.scalars(select(AttendanceRecord)).all()
    att_id = row.odoo_attendance_id
    # What release_old_odoo does when the database name changes: the row loses its Odoo id...
    row.odoo_attendance_id = None
    db.flush()
    # ...and the same Odoo record comes back as a second row.
    twin = AttendanceRecord(tenant_id=tenant.id, odoo_attendance_id=att_id, emp_code=row.emp_code,
                            origin="odoo", check_in=row.check_in, check_out=row.check_out)
    db.add(twin)
    db.flush()
    row.device_serial = "GATE-01"

    engine_mod.SyncEngine(db, tenant, "test")._merge_detached_duplicates()
    db.flush()
    rows = db.scalars(select(AttendanceRecord)).all()
    assert len(rows) == 1 and rows[0].odoo_attendance_id == att_id
    assert rows[0].device_serial == "GATE-01"


def test_an_orphan_without_a_twin_is_kept(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    run(db, tenant, odoo, [punch("in", "1001", local_day.replace(hour=1), direction=True)], monkeypatch)
    db.execute(update(AttendanceRecord).values(odoo_attendance_id=None))
    engine_mod.SyncEngine(db, tenant, "test")._merge_detached_duplicates()
    assert len(db.scalars(select(AttendanceRecord)).all()) == 1

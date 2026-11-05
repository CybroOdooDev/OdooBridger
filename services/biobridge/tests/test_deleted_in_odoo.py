"""Records BioBridge wrote are deleted in Odoo; the day's punches must not stay stuck."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AttendanceRecord, PunchRecord
from tests.conftest import FakeOdoo
from tests.test_sync_engine import punch, run


def _retry_errors(db):
    for p in db.scalars(select(PunchRecord).where(PunchRecord.process_state == "error")):
        p.process_state, p.attempts, p.error_message = "pending", 0, None
    db.commit()


@pytest.mark.usefixtures("no_stale_close")
def test_first_last_day_is_written_again_after_its_record_is_deleted(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = FakeOdoo()
    t = local_day.replace(hour=9)
    rows = [punch("a", "1001", t, direction=True), punch("b", "1001", t + timedelta(hours=4), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    (old_id,) = odoo.attendances
    del odoo.attendances[old_id]                                   # deleted in Odoo by hand

    rows += [punch("c", "1001", t + timedelta(hours=8), direction=False)]
    result = run(db, tenant, odoo, rows, monkeypatch)
    _retry_errors(db)
    result = run(db, tenant, odoo, rows, monkeypatch)

    assert result.error_count == 0
    (new_id,) = odoo.attendances
    rec = odoo.attendances[new_id]
    assert rec["check_in"] == t - timedelta(hours=4) and rec["check_out"] == t + timedelta(hours=4)
    states = {p.external_id: (p.process_state, p.odoo_attendance_id) for p in db.scalars(select(PunchRecord))}
    assert states == {"a": ("synced", new_id), "b": ("synced", new_id), "c": ("synced", new_id)}
    mirror = db.scalars(select(AttendanceRecord)).all()
    assert [m.odoo_attendance_id for m in mirror] == [new_id], "no row left for the deleted record"


@pytest.mark.usefixtures("no_stale_close")
def test_a_new_punch_alone_rebuilds_the_deleted_day(db, tenant, local_day, monkeypatch):
    """No retry needed: the next punch of that day brings the whole day back."""
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = FakeOdoo()
    t = local_day.replace(hour=9)
    rows = [punch("a", "1001", t, direction=True), punch("b", "1001", t + timedelta(hours=4), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    odoo.attendances.clear()
    rows += [punch("c", "1001", t + timedelta(hours=8), direction=False)]
    result = run(db, tenant, odoo, rows, monkeypatch)
    assert result.error_count == 0
    (rec,) = odoo.attendances.values()
    assert rec["check_in"] == t - timedelta(hours=4) and rec["check_out"] == t + timedelta(hours=4)

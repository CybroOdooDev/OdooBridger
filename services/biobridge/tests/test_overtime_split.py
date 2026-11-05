"""Odoo 20 splits a long shift into attendance + overtime; BioBridge keeps one shift.

Odoo's time rules are the customer's workflow: BioBridge never undoes them, and never
shows the pieces as separate attendances either. Its record is the shift as punched.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AttendanceRecord, PunchRecord
from tests.conftest import FakeOdoo
from tests.test_sync_engine import punch, run

H = timedelta(hours=1)
OFFSET = timedelta(hours=4)   # the test tenant's timezone


def odoo20():
    odoo = FakeOdoo()
    odoo.split_after_hours = 9      # 08:00 + 9 h -> overtime from 17:00
    return odoo


def mirror(db):
    return sorted(db.scalars(select(AttendanceRecord)).all(), key=lambda r: r.check_in)


@pytest.mark.usefixtures("no_stale_close")
def test_first_last_day_stays_one_record_here(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = odoo20()
    t = local_day.replace(hour=8)
    rows = [punch("a", "1001", t, direction=True), punch("b", "1001", t + 10.5 * H, direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    run(db, tenant, odoo, rows, monkeypatch)    # the next cycle reads Odoo back

    assert len(odoo.attendances) == 2, "Odoo split it: attendance + overtime"
    (row,) = mirror(db)
    assert row.check_in == t - OFFSET and row.check_out == t + 10.5 * H - OFFSET
    assert round(row.worked_hours, 2) == 10.5
    assert round(row.overtime_hours, 2) == 1.5, "the A + OT remark: 9 h attendance + 1.5 h overtime"
    assert row.origin != "odoo"


@pytest.mark.usefixtures("no_stale_close")
def test_alternating_close_stays_one_record_here(db, tenant, local_day, monkeypatch):
    odoo = odoo20()
    t = local_day.replace(hour=8)
    rows = [punch("a", "1001", t, direction=True)]
    run(db, tenant, odoo, rows, monkeypatch)
    rows += [punch("b", "1001", t + 10.5 * H, direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    run(db, tenant, odoo, rows, monkeypatch)
    (row,) = mirror(db)
    assert row.check_out == t + 10.5 * H - OFFSET


@pytest.mark.usefixtures("no_stale_close")
def test_a_later_punch_extends_the_shift_without_undoing_the_split(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = odoo20()
    t = local_day.replace(hour=8)
    rows = [punch("a", "1001", t, direction=True), punch("b", "1001", t + 10.5 * H, direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    root = min(odoo.attendances, key=lambda i: odoo.attendances[i]["check_in"])
    piece = next(i for i in odoo.attendances if odoo.attendances[i].get("source_attendance_id") == root)

    rows += [punch("c", "1001", t + 10.75 * H, direction=False)]       # 18:45
    result = run(db, tenant, odoo, rows, monkeypatch)
    assert result.error_count == 0
    assert odoo.attendances[root]["check_out"] == t + 9 * H - OFFSET, "the customer's cut stays"
    assert odoo.attendances[piece]["check_out"] == t + 10.75 * H - OFFSET, "the overtime runs to the last punch"
    assert len(odoo.attendances) == 2
    run(db, tenant, odoo, rows, monkeypatch)
    (row,) = mirror(db)
    assert row.check_out == t + 10.75 * H - OFFSET
    assert round(row.overtime_hours, 2) == 1.75
    assert {p.process_state for p in db.scalars(select(PunchRecord))} == {"synced"}


@pytest.mark.usefixtures("no_stale_close")
def test_a_punch_inside_the_shift_writes_nothing(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = odoo20()
    t = local_day.replace(hour=8)
    rows = [punch("a", "1001", t, direction=True), punch("b", "1001", t + 10.5 * H, direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    before = {i: dict(r) for i, r in odoo.attendances.items()}
    calls = len(odoo.calls)

    rows += [punch("m", "1001", t + 10.4 * H, direction=False)]     # inside 08:00-18:30
    result = run(db, tenant, odoo, rows, monkeypatch)
    assert result.error_count == 0
    assert odoo.attendances == before
    assert not [c for c in odoo.calls[calls:] if c.startswith(("update", "close", "create"))]
    assert {p.process_state for p in db.scalars(select(PunchRecord))} == {"synced"}


@pytest.mark.usefixtures("no_stale_close")
def test_overtime_rows_copied_here_earlier_are_removed(db, tenant, local_day, monkeypatch):
    """Rows the previous version copied in as "added in Odoo" go away."""
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = odoo20()
    t = local_day.replace(hour=8)
    rows = [punch("a", "1001", t, direction=True), punch("b", "1001", t + 10.5 * H, direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    root = min(odoo.attendances, key=lambda i: odoo.attendances[i]["check_in"])
    piece = next(i for i in odoo.attendances if i != root)
    if db.scalars(select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == piece)).first() is None:
        db.add(AttendanceRecord(tenant_id=tenant.id, odoo_attendance_id=piece, emp_code="1001", origin="odoo",
                                check_in=odoo.attendances[piece]["check_in"],
                                check_out=odoo.attendances[piece]["check_out"]))
    own = db.scalars(select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == root)).one()
    own.check_out = odoo.attendances[root]["check_out"]           # cut back to 17:00 here too
    own.overtime_hours = None
    db.commit()

    odoo.changed_only = set()        # nothing changed in Odoo since: only the cleanup can fix it
    run(db, tenant, odoo, rows, monkeypatch)
    (row,) = mirror(db)
    assert row.odoo_attendance_id == root and row.check_out == t + 10.5 * H - OFFSET
    assert round(row.overtime_hours, 2) == 1.5


@pytest.mark.usefixtures("no_stale_close")
def test_odoo_without_time_rules_is_unchanged(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = FakeOdoo()
    t = local_day.replace(hour=8)
    rows = [punch("a", "1001", t, direction=True), punch("b", "1001", t + 10.5 * H, direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    run(db, tenant, odoo, rows, monkeypatch)
    assert len(odoo.attendances) == 1
    (row,) = mirror(db)
    assert row.check_out == t + 10.5 * H - OFFSET
    assert row.overtime_hours is None

"""Records deleted in Odoo leave BioBridge's Attendance page too (Paul Williams, 5 Nov)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AttendanceRecord
from tests.conftest import FakeOdoo
from tests.test_sync_engine import punch, run


def shown(db):
    return sorted(r.odoo_attendance_id for r in db.scalars(select(AttendanceRecord)))


@pytest.mark.usefixtures("no_stale_close")
def test_closed_and_open_records_deleted_in_odoo_are_dropped(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    t = local_day.replace(hour=12, minute=50)
    rows = [punch("a", "1001", t, direction=True), punch("b", "1001", t + timedelta(minutes=10), direction=False),
            punch("c", "1001", t + timedelta(minutes=20), direction=True),
            punch("d", "1001", t + timedelta(minutes=35), direction=False),
            punch("e", "1001", t + timedelta(minutes=60), direction=True)]          # left open
    run(db, tenant, odoo, rows, monkeypatch)
    first, second, open_one = sorted(odoo.attendances)
    assert shown(db) == [first, second, open_one]

    del odoo.attendances[second]
    del odoo.attendances[open_one]       # removed in Odoo by hand
    run(db, tenant, odoo, rows, monkeypatch)
    assert shown(db) == [first]


@pytest.mark.usefixtures("no_stale_close")
def test_nothing_is_dropped_when_odoo_suddenly_knows_none_of_them(db, tenant, local_day, monkeypatch):
    """Every record gone at once means the wrong database or no access, not deletions."""
    odoo = FakeOdoo()
    t = local_day.replace(hour=6)
    rows = []
    for i in range(6):
        rows += [punch(f"i{i}", "1001", t + timedelta(hours=2 * i), direction=True),
                 punch(f"o{i}", "1001", t + timedelta(hours=2 * i, minutes=30), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    before = shown(db)
    assert len(before) == 6
    odoo.existing_attendance_ids = lambda ids: set()
    run(db, tenant, odoo, rows, monkeypatch)
    assert shown(db) == before

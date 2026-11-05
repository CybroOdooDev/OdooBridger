"""Switching to first/last on a day that already holds several records (Marc, 5 Nov)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AttendanceRecord, DeviceSource, PunchRecord
from tests.conftest import FakeOdoo
from tests.test_sync_engine import punch, run


def _two_records_under_state_based(db, tenant, local_day, monkeypatch):
    """12:55-13:10 and 13:15-13:30, written while pairing was by punch state."""
    tenant.pairing_mode = "alternating"; db.commit()
    odoo = FakeOdoo()
    t = local_day.replace(hour=12, minute=55)
    rows = [punch("a", "1001", t, direction=True),
            punch("b", "1001", t + timedelta(minutes=15), direction=False),
            punch("c", "1001", t + timedelta(minutes=20), direction=True),
            punch("d", "1001", t + timedelta(minutes=35), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    first, second = sorted(odoo.attendances, key=lambda i: odoo.attendances[i]["check_in"])
    tenant.pairing_mode = "first_last"; db.commit()
    return odoo, rows, t, first, second


@pytest.mark.usefixtures("no_stale_close")
def test_a_later_punch_extends_the_latest_record(db, tenant, local_day, monkeypatch):
    odoo, rows, t, first, second = _two_records_under_state_based(db, tenant, local_day, monkeypatch)
    before_first = dict(odoo.attendances[first])

    rows += [punch("e", "1001", t + timedelta(minutes=60), direction=True)]       # 13:55
    result = run(db, tenant, odoo, rows, monkeypatch)

    assert result.error_count == 0
    assert odoo.attendances[first]["check_in"] == before_first["check_in"]
    assert odoo.attendances[first]["check_out"] == before_first["check_out"], "the earlier record is left alone"
    assert odoo.attendances[second]["check_out"] == t + timedelta(minutes=60) - timedelta(hours=4)
    assert len(odoo.attendances) == 2, "no new record"
    e = db.scalars(select(PunchRecord).where(PunchRecord.external_id == "e")).one()
    assert (e.process_state, e.odoo_attendance_id) == ("synced", second)
    mirror = db.scalars(select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == second)).one()
    assert mirror.check_in == odoo.attendances[second]["check_in"]
    assert mirror.check_out == odoo.attendances[second]["check_out"]

    rows += [punch("f", "1001", t + timedelta(minutes=75), direction=False)]      # 14:10
    run(db, tenant, odoo, rows, monkeypatch)
    assert odoo.attendances[second]["check_out"] == t + timedelta(minutes=75) - timedelta(hours=4)
    assert {p.process_state for p in db.scalars(select(PunchRecord))} == {"synced"}


@pytest.mark.usefixtures("no_stale_close")
def test_a_punch_before_the_days_records_is_skipped_not_errored(db, tenant, local_day, monkeypatch):
    odoo, rows, t, first, second = _two_records_under_state_based(db, tenant, local_day, monkeypatch)
    rows += [punch("early", "1001", t - timedelta(minutes=22), direction=True),  # 12:33, a late upload
             punch("e", "1001", t + timedelta(minutes=60), direction=True)]       # 13:55
    for source in db.scalars(select(DeviceSource)):
        source.cursor_punch_time = None     # read the whole window again, as for a late upload
    db.commit()
    result = run(db, tenant, odoo, rows, monkeypatch)

    assert result.error_count == 0
    assert odoo.attendances[second]["check_out"] == t + timedelta(minutes=60) - timedelta(hours=4)
    states = {p.external_id: (p.process_state, p.error_message or "") for p in db.scalars(select(PunchRecord))}
    assert states["early"][0] == "skipped" and "before the switch" in states["early"][1]
    assert states["e"][0] == "synced"

    run(db, tenant, odoo, rows, monkeypatch)   # nothing comes back on the next cycle
    assert len(odoo.attendances) == 2


@pytest.mark.usefixtures("no_stale_close")
def test_the_error_names_the_record_in_the_way_not_the_one_being_written(db, tenant, local_day, monkeypatch):
    """A day record that someone else's record sits inside: the message names that one."""
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = FakeOdoo()
    t = local_day.replace(hour=12, minute=55)
    rows = [punch("a", "1001", t, direction=True), punch("b", "1001", t + timedelta(minutes=15), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    (day_id,) = odoo.attendances
    start = odoo.attendances[day_id]["check_out"] + timedelta(minutes=5)
    blocker = odoo.create_attendance(11, start, start + timedelta(minutes=15))    # entered in Odoo
    rows += [punch("c", "1001", t - timedelta(minutes=22), direction=True),
             punch("d", "1001", t + timedelta(minutes=60), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    msg = {p.external_id: p.error_message or "" for p in db.scalars(select(PunchRecord))}
    assert f"(attendance {blocker}," in msg["d"] and "entered in Odoo" in msg["d"]
    assert f"Can't write attendance {day_id}" in msg["d"]
    assert f"(attendance {day_id}," not in msg["d"]

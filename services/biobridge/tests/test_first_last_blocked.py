"""First/last: the day's record can't be extended because another record sits in the way."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AttendanceRecord, PunchRecord
from tests.conftest import FakeOdoo
from tests.test_sync_engine import punch, run


@pytest.mark.usefixtures("no_stale_close")
def test_blocked_extension_is_reported_not_hidden(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = FakeOdoo()
    t0 = local_day.replace(hour=8, minute=43)
    rows = [punch("a", "1001", t0, direction=True), punch("b", "1001", t0 + timedelta(hours=8), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    (day_id,) = odoo.attendances
    day_end = odoo.attendances[day_id]["check_out"]
    # Someone enters a record in Odoo that starts where the day's record ends.
    blocker = odoo.create_attendance(11, day_end, day_end + timedelta(minutes=8))

    rows += [punch("c", "1001", t0 + timedelta(hours=8, minutes=22), direction=False)]
    result = run(db, tenant, odoo, rows, monkeypatch)

    assert odoo.attendances[day_id]["check_out"] == day_end, "the day record could not be extended"
    states = {p.external_id: (p.process_state, p.error_message or "") for p in db.scalars(select(PunchRecord))}
    assert states["a"][0] == "synced" and states["b"][0] == "synced"
    assert states["c"][0] == "error", states
    assert str(blocker) in states["c"][1] and "entered" in states["c"][1].lower() or "another record" in states["c"][1].lower()
    assert result.error_count == 1
    mirror = db.scalars(select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == day_id)).one()
    assert mirror.check_out == day_end


@pytest.mark.usefixtures("no_stale_close")
def test_the_error_names_the_blocking_record(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = FakeOdoo()
    t0 = local_day.replace(hour=8, minute=43)
    rows = [punch("a", "1001", t0, direction=True), punch("b", "1001", t0 + timedelta(hours=8), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    (day_id,) = odoo.attendances
    day_end = odoo.attendances[day_id]["check_out"]
    blocker = odoo.create_attendance(11, day_end, day_end + timedelta(minutes=8))
    rows += [punch("c", "1001", t0 + timedelta(hours=8, minutes=15), direction=True),
             punch("d", "1001", t0 + timedelta(hours=8, minutes=22), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    msg = {p.external_id: p.error_message for p in db.scalars(select(PunchRecord))}
    assert f"attendance {blocker}" in msg["d"] and "entered in Odoo" in msg["d"] and "Adjust or remove" in msg["d"]
    assert msg["c"] == msg["d"], "the covered punch carries the same explanation"
    states = {p.external_id: p.process_state for p in db.scalars(select(PunchRecord))}
    assert states == {"a": "synced", "b": "synced", "c": "error", "d": "error"}


@pytest.mark.usefixtures("no_stale_close")
def test_once_the_blocker_is_gone_retry_extends_the_day(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = FakeOdoo()
    t0 = local_day.replace(hour=8, minute=43)
    rows = [punch("a", "1001", t0, direction=True), punch("b", "1001", t0 + timedelta(hours=8), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    (day_id,) = odoo.attendances
    day_end = odoo.attendances[day_id]["check_out"]
    blocker = odoo.create_attendance(11, day_end, day_end + timedelta(minutes=8))
    rows += [punch("c", "1001", t0 + timedelta(hours=8, minutes=22), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    del odoo.attendances[blocker]            # removed in Odoo
    run(db, tenant, odoo, rows, monkeypatch)  # the error punch is retried on the next sync
    assert odoo.attendances[day_id]["check_out"] == t0 + timedelta(hours=8, minutes=22) - timedelta(hours=4)
    assert {p.process_state for p in db.scalars(select(PunchRecord))} == {"synced"}

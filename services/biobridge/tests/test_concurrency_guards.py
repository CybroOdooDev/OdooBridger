"""Storing what another process stored a moment ago is skipped, not an error
(tests_load/ runs the real thing on PostgreSQL)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.integrations.base import PunchEvent
from app.models import Device, DeviceSource, PunchRecord
from app.services.device_links import device_for_serial
from app.services.sync_engine import insert_punches, store_punch


def _event(i, sn="GATE-01"):
    return PunchEvent(external_id=f"x{i}", emp_code="1001", punch_time_local=datetime(2026, 10, 1, 9, i),
                      terminal_sn=sn, raw={})


def test_a_batch_with_punches_already_stored_keeps_the_new_ones(db, tenant):
    source = db.scalars(select(DeviceSource)).one()
    devices = {d.serial_number: d for d in db.scalars(select(Device))}
    when = datetime.utcnow() - timedelta(hours=1)
    store_punch(db, tenant, source, devices, _event(1), when)     # "the other process"
    db.commit()
    items = [(_event(i), when) for i in (1, 2, 3)]
    made = insert_punches(db, items, lambda e, t: store_punch(db, tenant, source, devices, e, t))
    db.commit()
    assert made == 2
    assert sorted(db.scalars(select(PunchRecord.external_id))) == ["x1", "x2", "x3"]


def test_a_terminal_added_by_someone_else_meanwhile_is_reused(db, tenant):
    source = db.scalars(select(DeviceSource)).one()
    first, created = device_for_serial(db, tenant.id, source.id, "NEW-1")
    db.commit()
    again, created_again = device_for_serial(db, tenant.id, source.id, "NEW-1")
    assert created and not created_again and again.id == first.id

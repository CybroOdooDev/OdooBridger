"""First/last: the day's record is found in Odoo even when the ledger no longer points at it."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update

from app.models import PunchRecord, PunchState
from tests.conftest import FakeOdoo
from tests.test_sync_engine import punch, run


def test_a_later_batch_extends_the_days_record_after_its_punches_were_deleted(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = FakeOdoo()
    t0 = local_day.replace(hour=8, minute=43)
    run(db, tenant, odoo, [punch("a", "1001", t0, direction=True), punch("b", "1001", t0 + timedelta(hours=8), direction=False)], monkeypatch)
    (day_id,) = odoo.attendances
    # The morning's punches are deleted on the Activity page (bulk delete), so the ledger
    # no longer knows which record is the day's.
    db.execute(update(PunchRecord).values(process_state=PunchState.deleted.value)); db.commit()

    later = [punch("c", "1001", t0 + timedelta(hours=8, minutes=1), direction=True),
             punch("d", "1001", t0 + timedelta(hours=8, minutes=9), direction=False)]
    r = run(db, tenant, odoo, later, monkeypatch)
    assert r.status == "success", r.error_message
    assert list(odoo.attendances) == [day_id], f"a second record was opened: {odoo.attendances}"
    assert odoo.attendances[day_id]["check_out"] == (t0 + timedelta(hours=8, minutes=9)) - timedelta(hours=4)
    assert {p.external_id: p.odoo_attendance_id for p in db.scalars(select(PunchRecord)) if p.external_id in "cd"} == {"c": day_id, "d": day_id}


def test_a_new_day_still_gets_its_own_record(db, tenant, local_day, monkeypatch):
    tenant.pairing_mode = "first_last"; db.commit()
    odoo = FakeOdoo()
    t0 = local_day.replace(hour=8)
    run(db, tenant, odoo, [punch("a", "1001", t0, direction=True), punch("b", "1001", t0 + timedelta(hours=8), direction=False)], monkeypatch)
    run(db, tenant, odoo, [punch("c", "1001", t0 + timedelta(days=1), direction=True)], monkeypatch)
    assert len(odoo.attendances) == 2

"""What the Overview shows about activity: today's punches and people, the week, the latest punches."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Device, DeviceSource, EmployeeMapping, PunchRecord
from app.services.dashboard_activity import LATEST, activity_summary
from tests.test_punch_ledger import api  # noqa: F401 — a seeded, signed-in tenant

# 12:00 UTC on 2026-11-06 is 17:30 in Kolkata: that site's day began at 18:30 UTC on the 5th.
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _punch(db, tenant, code, when, *, state="synced", n=[0]):
    n[0] += 1
    source = db.scalars(select(DeviceSource).where(DeviceSource.tenant_id == tenant.id)).first()
    db.add(PunchRecord(tenant_id=tenant.id, source_id=source.id, external_id=f"a{n[0]}", emp_code=code,
                       punch_time_utc=when.replace(tzinfo=None), punch_time_local=when.replace(tzinfo=None),
                       direction="in", process_state=state))


def _mapped(db, tenant, code, name):
    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code=code, status="mapped", odoo_employee_name=name))


def test_today_is_the_accounts_day_not_utcs(db, tenant):
    tenant.timezone = "Asia/Kolkata"
    # 19:00 UTC the evening before is 00:30 local today; 17:00 UTC the evening before is 22:30 local yesterday.
    _punch(db, tenant, "1", NOW - timedelta(hours=17))
    _punch(db, tenant, "1", NOW - timedelta(hours=19))
    db.commit()
    out = activity_summary(db, tenant, now=NOW)
    assert out["punches_today"] == 1
    assert [d["count"] for d in out["punches_week"]][-2:] == [1, 1] and len(out["punches_week"]) == 7
    assert out["punches_week"][-1]["date"] == "2026-11-06"


def test_people_counts_matched_badges_only_so_it_agrees_with_its_total(db, tenant):
    tenant.timezone = "UTC"
    _mapped(db, tenant, "10", "Sara Ali")
    _mapped(db, tenant, "11", "Ravi Menon")
    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code="99", status="unmapped"))
    for code in ("10", "10", "99"):
        _punch(db, tenant, code, NOW - timedelta(hours=1))
    db.commit()
    out = activity_summary(db, tenant, now=NOW)
    assert out["people_today"] == 1 and out["people_total"] == 2


def test_latest_punches_are_newest_first_named_and_say_where_they_stand(db, tenant):
    tenant.timezone = "UTC"
    _mapped(db, tenant, "10", "Sara Ali")
    _punch(db, tenant, "10", NOW - timedelta(minutes=30), state="synced")
    _punch(db, tenant, "77", NOW - timedelta(minutes=5), state="unmapped")
    for i in range(LATEST + 3):
        _punch(db, tenant, "10", NOW - timedelta(days=2, minutes=i))
    db.commit()
    latest = activity_summary(db, tenant, now=NOW)["latest_punches"]
    assert len(latest) == LATEST
    assert latest[0]["name"] == "Badge 77" and latest[0]["state"] == "unmapped"
    assert latest[1]["name"] == "Sara Ali" and latest[1]["state"] == "synced"
    assert latest[0]["time_local"] is not None


def test_the_dashboard_carries_the_activity_and_the_updated_count(api):  # noqa: F811
    body = api.get("/api/v1/dashboard").json()
    assert {"people_today", "people_total", "punches_week", "latest_punches"} <= set(body)
    assert len(body["punches_week"]) == 7
    assert all("punches_today" in t for t in body["connections"]["terminals"])
    if body["last_run"]:
        assert "attendances_updated" in body["last_run"]

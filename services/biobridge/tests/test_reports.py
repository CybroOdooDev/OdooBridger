"""Reports: exceptions, terminal health, daily summary and sync history."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    AttendanceRecord, Device, EmployeeMapping, PunchRecord, SyncRun,
)
from app.services import reports
from app.services.timeutils import utc_to_local, utcnow_naive

TZ = "Asia/Dubai"   # UTC+4


def _punch(db, tenant, n, when, state, code="100", sn="GATE-01", error=None):
    src = tenant_source(db, tenant)
    db.add(PunchRecord(tenant_id=tenant.id, source_id=src, external_id=f"x{n}", emp_code=code,
                       punch_time_utc=when, process_state=state, terminal_sn=sn, error_message=error))


def tenant_source(db, tenant):
    return db.scalar(select(Device.source_id).where(Device.tenant_id == tenant.id))


def _today(tenant):
    return utc_to_local(utcnow_naive(), tenant.timezone).date()


def test_dates_are_the_accounts_own_days(tenant):
    # Asia/Dubai is UTC+4: its 14th starts at 20:00 UTC on the 13th.
    start, end = reports.bounds(tenant, date(2026, 10, 14), date(2026, 10, 14))
    assert start == datetime(2026, 10, 13, 20, 0) and end == datetime(2026, 10, 14, 20, 0)
    with pytest.raises(reports.ReportError):
        reports.bounds(tenant, date(2026, 10, 14), date(2026, 10, 13))
    with pytest.raises(reports.ReportError):
        reports.bounds(tenant, date(2025, 1, 1), date(2026, 10, 14))


def test_exceptions_list_what_did_not_reach_odoo(db, tenant):
    now = utcnow_naive() - timedelta(hours=1)
    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code="777", source_name="Visitor", status="unmapped"))
    _punch(db, tenant, 1, now, "unmapped", code="777")
    _punch(db, tenant, 2, now - timedelta(minutes=5), "unmapped", code="777")
    _punch(db, tenant, 3, now, "error", error="Odoo said no")
    _punch(db, tenant, 4, now, "held", sn="GATE-99")
    _punch(db, tenant, 5, now, "synced")
    old_in = utcnow_naive() - timedelta(hours=30)   # open longer than the 16 h shift limit
    db.add(AttendanceRecord(tenant_id=tenant.id, emp_code="100", employee_name="Ann", check_in=old_in,
                            shift_date=_today(tenant).isoformat(), odoo_attendance_id=11))
    db.add(AttendanceRecord(tenant_id=tenant.id, emp_code="100", employee_name="Ann", check_in=now,
                            check_out=now + timedelta(minutes=1), is_orphan_out=True, odoo_attendance_id=12))
    db.add(AttendanceRecord(tenant_id=tenant.id, emp_code="100", employee_name="Ann", check_in=now,
                            odoo_attendance_id=13))   # open, but recently: not missing yet
    db.commit()

    out = reports.exceptions(db, tenant, _today(tenant) - timedelta(days=2), _today(tenant))
    t = out["totals"]
    assert t == {"unmatched_badges": 1, "unmatched_punches": 2, "failed": 1, "held": 1,
                 "missing_check_out": 1, "auto_closed": 0, "orphan_check_out": 1}
    assert out["unmatched"][0]["emp_code"] == "777" and out["unmatched"][0]["device_name"] == "Visitor"
    assert out["failed"][0]["error"] == "Odoo said no"
    assert out["missing_check_out"][0]["odoo_id"] == 11


def test_terminal_health_spots_a_silent_terminal(db, tenant):
    src = tenant_source(db, tenant)
    db.add(Device(tenant_id=tenant.id, source_id=src, serial_number="GATE-02", alias="Back door", is_enabled=True))
    db.add(Device(tenant_id=tenant.id, source_id=src, serial_number="GATE-03", is_enabled=True,
                  missing_since=datetime.now(timezone.utc)))
    db.commit()
    _punch(db, tenant, 1, utcnow_naive() - timedelta(hours=1), "synced", sn="GATE-01")
    _punch(db, tenant, 2, utcnow_naive() - timedelta(hours=1), "synced", code="101", sn="GATE-01")
    _punch(db, tenant, 3, utcnow_naive() - timedelta(hours=60), "synced", sn="GATE-02")
    db.commit()

    out = reports.terminals(db, tenant, _today(tenant) - timedelta(days=6), _today(tenant))
    by = {r["serial"]: r for r in out["terminals"]}
    assert by["GATE-01"]["state"] == "active" and by["GATE-01"]["punches"] == 2 and by["GATE-01"]["employees"] == 2
    assert by["GATE-02"]["state"] == "silent" and by["GATE-02"]["name"] == "Back door"
    assert by["GATE-03"]["state"] == "missing"
    assert out["totals"]["silent"] == 1 and out["totals"]["active"] == 1
    # A longer silence threshold makes GATE-02 count as active again.
    assert {r["serial"]: r["state"] for r in reports.terminals(
        db, tenant, _today(tenant) - timedelta(days=6), _today(tenant), silent_hours=72)["terminals"]}["GATE-02"] == "active"


def test_daily_summary_counts_by_local_day(db, tenant):
    today = _today(tenant)
    # 21:00 UTC yesterday is 01:00 today in Dubai: it belongs to today.
    early = datetime.combine(today - timedelta(days=1), datetime.min.time()) + timedelta(hours=21)
    _punch(db, tenant, 1, early, "synced")
    _punch(db, tenant, 2, early + timedelta(minutes=1), "synced", code="101")
    _punch(db, tenant, 3, early + timedelta(minutes=2), "error")
    _punch(db, tenant, 4, early + timedelta(minutes=3), "deleted")   # removed by a user: not counted
    db.add(AttendanceRecord(tenant_id=tenant.id, emp_code="100", check_in=early, shift_date=today.isoformat()))
    db.commit()

    out = reports.daily(db, tenant, today - timedelta(days=1), today)
    days = {d["date"]: d for d in out["days"]}
    assert days[today.isoformat()]["punches"] == 3 and days[today.isoformat()]["synced"] == 2
    assert days[today.isoformat()]["error"] == 1 and days[today.isoformat()]["employees"] == 2
    assert days[today.isoformat()]["shifts"] == 1 and days[today.isoformat()]["open_shifts"] == 1
    assert days[(today - timedelta(days=1)).isoformat()]["punches"] == 0
    assert out["days"][0]["date"] == today.isoformat()   # newest first


def test_sync_history_totals(db, tenant):
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    for status, ms, new in (("success", 1000, 5), ("success", 3000, 2), ("failed", None, 0)):
        db.add(SyncRun(tenant_id=tenant.id, status=status, started_at=now, duration_ms=ms, punches_new=new,
                       attendances_created=new))
    db.commit()
    out = reports.sync_history(db, tenant, _today(tenant), _today(tenant))
    t = out["totals"]
    assert t["runs"] == 3 and t["failed"] == 1 and t["success_rate"] == 67
    assert t["avg_duration_ms"] == 2000 and t["punches_new"] == 7
    assert out["days"][0]["runs"] == 3 and len(out["runs"]) == 3


# --- through the API: each account sees only its own --------------------------------
def test_reports_are_per_account(tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.session import get_db
    from app.main import app
    from app.models import Base, Tenant

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as client:
            def signup(company, email):
                r = client.post("/api/v1/auth/signup", json={
                    "company_name": company, "email": email, "password": "a-long-enough-password", "timezone": TZ})
                assert r.status_code == 201, r.text
                return {"Authorization": f"Bearer {r.json()['access_token']}"}

            a = signup("Alpha", "a@alpha.io")
            b = signup("Beta", "b@beta.io")
            s = Session()
            alpha = s.scalar(select(Tenant).where(Tenant.name == "Alpha"))
            s.add(SyncRun(tenant_id=alpha.id, status="success", started_at=datetime.now(timezone.utc)))
            s.commit()
            s.close()
            for kind in ("exceptions", "terminals", "daily", "sync"):
                assert client.get(f"/api/v1/reports/{kind}", headers=a).status_code == 200
            assert client.get("/api/v1/reports/sync", headers=a).json()["totals"]["runs"] == 1
            assert client.get("/api/v1/reports/sync", headers=b).json()["totals"]["runs"] == 0
            assert client.get("/api/v1/reports/sync").status_code == 401
            r = client.get("/api/v1/reports/daily?date_from=2026-10-10&date_to=2026-10-01", headers=a)
            assert r.status_code == 400
    finally:
        app.dependency_overrides.clear()

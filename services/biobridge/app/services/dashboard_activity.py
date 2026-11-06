"""What the Overview shows about activity: how many punches today and from how many
people, the last seven days, and the latest punches with where each one stands.

Days are the account's own (its timezone), not UTC's: an Indian site's "today" starts at
18:30 UTC the evening before. Computed on demand from state already kept.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Device, EmployeeMapping, MappingStatus, PunchRecord, Tenant

LATEST = 5
DAYS = 7


def _utc_midnights(tenant: Tenant, now: datetime, days: int) -> list[tuple[str, datetime]]:
    """(local date, its start as naive UTC) for today and the ``days - 1`` before it, oldest first."""
    try:
        tz = ZoneInfo(tenant.timezone or "UTC")
    except Exception:  # noqa: BLE001 — an unknown zone name falls back to UTC rather than breaking the page
        tz = ZoneInfo("UTC")
    today = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for back in range(days - 1, -1, -1):
        start = (today - timedelta(days=back))
        # Built from the date so a DST change never shifts a day's start by an hour.
        start = datetime(start.year, start.month, start.day, tzinfo=tz)
        out.append((start.date().isoformat(), start.astimezone(timezone.utc).replace(tzinfo=None)))
    return out


def activity_summary(db: Session, tenant: Tenant, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    days = _utc_midnights(tenant, now, DAYS)
    today_start = days[-1][1]
    base = PunchRecord.tenant_id == tenant.id

    week = []
    for i, (label, start) in enumerate(days):
        end = days[i + 1][1] if i + 1 < len(days) else None
        stmt = select(func.count(PunchRecord.id)).where(base, PunchRecord.punch_time_utc >= start)
        if end is not None:
            stmt = stmt.where(PunchRecord.punch_time_utc < end)
        week.append({"date": label, "count": db.scalar(stmt) or 0})

    # People, as the "of N" beside it counts them: badges matched to an Odoo employee.
    people_today = db.scalar(
        select(func.count(func.distinct(PunchRecord.emp_code)))
        .join(EmployeeMapping, (EmployeeMapping.tenant_id == PunchRecord.tenant_id)
              & (EmployeeMapping.emp_code == PunchRecord.emp_code))
        .where(base, PunchRecord.punch_time_utc >= today_start, EmployeeMapping.status == MappingStatus.mapped.value)
    ) or 0
    people_total = db.scalar(
        select(func.count(EmployeeMapping.id)).where(
            EmployeeMapping.tenant_id == tenant.id, EmployeeMapping.status == MappingStatus.mapped.value)
    ) or 0

    rows = db.execute(
        select(PunchRecord, EmployeeMapping.odoo_employee_name, EmployeeMapping.source_name, Device.alias, Device.serial_number)
        .outerjoin(EmployeeMapping, (EmployeeMapping.tenant_id == PunchRecord.tenant_id)
                   & (EmployeeMapping.emp_code == PunchRecord.emp_code))
        .outerjoin(Device, Device.id == PunchRecord.device_id)
        .where(base)
        .order_by(PunchRecord.punch_time_utc.desc())
        .limit(LATEST)
    ).all()
    latest = [{
        "id": p.id,
        "name": odoo_name or source_name or f"Badge {p.emp_code}",
        "emp_code": p.emp_code,
        "terminal": alias or serial or p.terminal_sn,
        "time": p.punch_time_utc.replace(tzinfo=timezone.utc),
        "time_local": p.punch_time_local,
        "direction": p.direction,
        "state": p.process_state,
    } for p, odoo_name, source_name, alias, serial in rows]

    return {
        "punches_today": week[-1]["count"],
        "people_today": people_today,
        "people_total": people_total,
        "punches_week": week,
        "latest_punches": latest,
    }

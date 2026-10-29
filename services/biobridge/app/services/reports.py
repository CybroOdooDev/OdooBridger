"""The account's reports: proof that attendance arrived, and what still needs a hand.

Every report reads this account's own ledger (punches), attendance mirror and
sync runs. None of it leaves the account. Dates are the account's own calendar
days (``Tenant.timezone``), turned into UTC bounds once here.

These are deliberately not attendance analytics (lateness, overtime, hours):
Odoo reports on the attendance itself. BioBridge reports on getting it there.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AttendanceRecord, Device, DeviceSource, EmployeeMapping, MappingStatus, PunchRecord, PunchState,
    SyncRun, SyncStatus, Tenant,
)
from app.models.adms import AdmsDevice
from app.services.timeutils import ensure_aware, local_to_utc, utc_to_local, utcnow_naive

#: The longest range one report covers. A year of daily rows is plenty to read.
MAX_DAYS = 366
#: Rows listed per section; the totals above each list are always complete.
ROW_LIMIT = 500


class ReportError(ValueError):
    """Safe to show."""


def bounds(tenant: Tenant, date_from: date, date_to: date) -> tuple[datetime, datetime]:
    """The account's calendar days ``date_from``..``date_to`` (inclusive) as naive UTC [start, end)."""
    if date_to < date_from:
        raise ReportError("The end date is before the start date.")
    if (date_to - date_from).days + 1 > MAX_DAYS:
        raise ReportError(f"Choose a range of {MAX_DAYS} days or less.")
    tz = tenant.timezone or "UTC"
    start = local_to_utc(datetime.combine(date_from, time.min), tz)
    end = local_to_utc(datetime.combine(date_to + timedelta(days=1), time.min), tz)
    return start, end


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _local(tenant: Tenant, value: datetime | None) -> str | None:
    """Naive UTC -> the account's wall clock, as text."""
    return utc_to_local(value, tenant.timezone or "UTC").isoformat(timespec="minutes") if value else None


def _names(db: Session, tenant: Tenant) -> dict[str, str]:
    """Badge -> the Odoo employee's name (or the device's), for readable rows."""
    rows = db.execute(select(EmployeeMapping.emp_code, EmployeeMapping.odoo_employee_name,
                             EmployeeMapping.source_name)
                      .where(EmployeeMapping.tenant_id == tenant.id)).all()
    return {code: (odoo or device or "") for code, odoo, device in rows}


def _terminal_names(db: Session, tenant: Tenant) -> dict[str, str]:
    return {d.serial_number: (d.alias or d.serial_number)
            for d in db.scalars(select(Device).where(Device.tenant_id == tenant.id))}


# =============================================================================
# Exceptions: everything that did not reach Odoo cleanly
# =============================================================================
def exceptions(db: Session, tenant: Tenant, date_from: date, date_to: date) -> dict[str, Any]:
    start, end = bounds(tenant, date_from, date_to)
    names = _names(db, tenant)
    terminals = _terminal_names(db, tenant)
    in_range = (PunchRecord.tenant_id == tenant.id, PunchRecord.punch_time_utc >= start,
                PunchRecord.punch_time_utc < end)

    # Unmatched badges: punches held because no Odoo employee carries the badge.
    unmatched_rows = db.execute(
        select(PunchRecord.emp_code, func.count(), func.min(PunchRecord.punch_time_utc),
               func.max(PunchRecord.punch_time_utc))
        .where(*in_range, PunchRecord.process_state == PunchState.unmapped.value)
        .group_by(PunchRecord.emp_code)
        .order_by(func.count().desc())).all()
    mapping = {m.emp_code: m for m in db.scalars(select(EmployeeMapping).where(
        EmployeeMapping.tenant_id == tenant.id,
        EmployeeMapping.emp_code.in_([r[0] for r in unmatched_rows] or [""])))}
    unmatched = [{
        "emp_code": code, "device_name": (mapping.get(code).source_name if mapping.get(code) else None),
        "status": (mapping.get(code).status if mapping.get(code) else MappingStatus.unmapped.value),
        "punches": count, "first_punch": _local(tenant, first), "last_punch": _local(tenant, last),
    } for code, count, first, last in unmatched_rows]

    # Punches Odoo refused (still retried while attempts remain).
    failed = [{
        "id": p.id, "emp_code": p.emp_code, "employee": names.get(p.emp_code) or None,
        "punch_time": _local(tenant, p.punch_time_utc), "terminal": terminals.get(p.terminal_sn or "", p.terminal_sn),
        "attempts": p.attempts, "error": p.error_message,
    } for p in db.scalars(select(PunchRecord).where(*in_range, PunchRecord.process_state == PunchState.error.value)
                          .order_by(PunchRecord.punch_time_utc.desc()).limit(ROW_LIMIT))]
    failed_total = db.scalar(select(func.count()).select_from(PunchRecord).where(
        *in_range, PunchRecord.process_state == PunchState.error.value)) or 0

    # Held: from a terminal beyond the plan's device allowance.
    held = [{
        "terminal": terminals.get(sn or "", sn) or "Unknown", "serial": sn, "punches": count,
        "oldest": _local(tenant, oldest),
    } for sn, count, oldest in db.execute(
        select(PunchRecord.terminal_sn, func.count(), func.min(PunchRecord.punch_time_utc))
        .where(*in_range, PunchRecord.process_state == PunchState.held.value)
        .group_by(PunchRecord.terminal_sn).order_by(func.count().desc())).all()]

    # Shifts with a problem: still open past the longest allowed shift, closed by
    # the shift-limit sweep, or a check-out with no check-in.
    att = (AttendanceRecord.tenant_id == tenant.id, AttendanceRecord.check_in >= start,
           AttendanceRecord.check_in < end)
    stale = utcnow_naive() - timedelta(hours=tenant.max_shift_hours or 16)

    def shift_row(r: AttendanceRecord) -> dict[str, Any]:
        return {"emp_code": r.emp_code, "employee": r.employee_name, "date": r.shift_date,
                "check_in": r.check_in_local.isoformat(timespec="minutes") if r.check_in_local else None,
                "check_out": r.check_out_local.isoformat(timespec="minutes") if r.check_out_local else None,
                "terminal": terminals.get(r.device_serial or "", r.device_serial), "odoo_id": r.odoo_attendance_id}

    missing_out = [shift_row(r) for r in db.scalars(
        select(AttendanceRecord).where(*att, AttendanceRecord.check_out.is_(None), AttendanceRecord.check_in < stale)
        .order_by(AttendanceRecord.check_in.desc()).limit(ROW_LIMIT))]
    auto_closed = [shift_row(r) for r in db.scalars(
        select(AttendanceRecord).where(*att, AttendanceRecord.is_auto_closed.is_(True))
        .order_by(AttendanceRecord.check_in.desc()).limit(ROW_LIMIT))]
    orphan_out = [shift_row(r) for r in db.scalars(
        select(AttendanceRecord).where(*att, AttendanceRecord.is_orphan_out.is_(True))
        .order_by(AttendanceRecord.check_in.desc()).limit(ROW_LIMIT))]

    return {
        "date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "timezone": tenant.timezone,
        "max_shift_hours": tenant.max_shift_hours,
        "totals": {"unmatched_badges": len(unmatched), "unmatched_punches": sum(r["punches"] for r in unmatched),
                   "failed": failed_total, "held": sum(r["punches"] for r in held),
                   "missing_check_out": len(missing_out), "auto_closed": len(auto_closed),
                   "orphan_check_out": len(orphan_out)},
        "unmatched": unmatched, "failed": failed, "held": held,
        "missing_check_out": missing_out, "auto_closed": auto_closed, "orphan_check_out": orphan_out,
    }


# =============================================================================
# Terminal health: is every terminal still sending?
# =============================================================================
def terminals(db: Session, tenant: Tenant, date_from: date, date_to: date, silent_hours: int = 24) -> dict[str, Any]:
    start, end = bounds(tenant, date_from, date_to)
    now = utcnow_naive()
    silent_before = now - timedelta(hours=silent_hours)
    sources = {s.id: s for s in db.scalars(select(DeviceSource).where(DeviceSource.tenant_id == tenant.id))}
    devices = list(db.scalars(select(Device).where(Device.tenant_id == tenant.id)
                              .order_by(Device.alias, Device.serial_number)))
    last_punch = dict(db.execute(
        select(PunchRecord.terminal_sn, func.max(PunchRecord.punch_time_utc))
        .where(PunchRecord.tenant_id == tenant.id).group_by(PunchRecord.terminal_sn)).all())
    in_range = dict(db.execute(
        select(PunchRecord.terminal_sn, func.count())
        .where(PunchRecord.tenant_id == tenant.id, PunchRecord.punch_time_utc >= start,
               PunchRecord.punch_time_utc < end, PunchRecord.process_state != PunchState.deleted.value)
        .group_by(PunchRecord.terminal_sn)).all())
    people = dict(db.execute(
        select(PunchRecord.terminal_sn, func.count(func.distinct(PunchRecord.emp_code)))
        .where(PunchRecord.tenant_id == tenant.id, PunchRecord.punch_time_utc >= start,
               PunchRecord.punch_time_utc < end, PunchRecord.process_state != PunchState.deleted.value)
        .group_by(PunchRecord.terminal_sn)).all())
    pushed = {d.serial_number: d for d in db.scalars(select(AdmsDevice).where(AdmsDevice.tenant_id == tenant.id))}

    rows = []
    for d in devices:
        last = last_punch.get(d.serial_number)
        contacts = [ensure_aware(x) for x in (d.last_seen_at, getattr(pushed.get(d.serial_number), "last_seen_at", None)) if x]
        contact = max(contacts) if contacts else None
        if not d.is_enabled:
            state = "disabled"
        elif d.missing_since:
            state = "missing"
        elif last is None:
            state = "no_punches"
        elif last < silent_before:
            state = "silent"
        else:
            state = "active"
        src = sources.get(d.source_id)
        rows.append({
            "id": d.id, "name": d.alias or d.serial_number, "serial": d.serial_number, "area": d.area,
            "connection": src.name if src else None, "company": d.company_name,
            "state": state, "last_punch": _local(tenant, last),
            "last_contact": _local(tenant, contact.replace(tzinfo=None)) if contact else None,
            "punches": in_range.get(d.serial_number, 0), "employees": people.get(d.serial_number, 0),
            "missing_since": _iso(d.missing_since),
        })
    states = Counter(r["state"] for r in rows)
    return {
        "date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "timezone": tenant.timezone,
        "silent_hours": silent_hours,
        "totals": {"terminals": len(rows), "active": states["active"], "silent": states["silent"],
                   "missing": states["missing"], "no_punches": states["no_punches"], "disabled": states["disabled"],
                   "punches": sum(r["punches"] for r in rows)},
        "terminals": rows,
    }


# =============================================================================
# Daily summary: what arrived each day, and where it went
# =============================================================================
DAILY_STATES = ("synced", "pending", "unmapped", "error", "held", "skipped")


def daily(db: Session, tenant: Tenant, date_from: date, date_to: date) -> dict[str, Any]:
    start, end = bounds(tenant, date_from, date_to)
    tz = tenant.timezone or "UTC"
    days: dict[str, dict[str, Any]] = {}
    d = date_from
    while d <= date_to:
        days[d.isoformat()] = {"date": d.isoformat(), "punches": 0, **{s: 0 for s in DAILY_STATES},
                               "_people": set(), "_terminals": set(), "shifts": 0, "open_shifts": 0}
        d += timedelta(days=1)
    # Only the columns needed: a busy site has thousands of punches a day.
    for when, state, code, sn in db.execute(
            select(PunchRecord.punch_time_utc, PunchRecord.process_state, PunchRecord.emp_code,
                   PunchRecord.terminal_sn)
            .where(PunchRecord.tenant_id == tenant.id, PunchRecord.punch_time_utc >= start,
                   PunchRecord.punch_time_utc < end, PunchRecord.process_state != PunchState.deleted.value)):
        row = days.get(utc_to_local(when, tz).date().isoformat())
        if row is None:
            continue
        row["punches"] += 1
        if state in DAILY_STATES:
            row[state] += 1
        row["_people"].add(code)
        if sn:
            row["_terminals"].add(sn)
    for shift_date, open_, count in db.execute(
            select(AttendanceRecord.shift_date, AttendanceRecord.check_out.is_(None), func.count())
            .where(AttendanceRecord.tenant_id == tenant.id, AttendanceRecord.shift_date >= date_from.isoformat(),
                   AttendanceRecord.shift_date <= date_to.isoformat())
            .group_by(AttendanceRecord.shift_date, AttendanceRecord.check_out.is_(None))):
        row = days.get(shift_date or "")
        if row is None:
            continue
        row["shifts"] += count
        if open_:
            row["open_shifts"] += count
    out = []
    for row in days.values():
        row["employees"] = len(row.pop("_people"))
        row["terminals"] = len(row.pop("_terminals"))
        out.append(row)
    out.reverse()   # newest first
    totals = {k: sum(r[k] for r in out) for k in ("punches", *DAILY_STATES, "shifts", "open_shifts")}
    return {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "timezone": tenant.timezone,
            "totals": totals, "days": out}


# =============================================================================
# Sync history: how the scheduler has been doing
# =============================================================================
def sync_history(db: Session, tenant: Tenant, date_from: date, date_to: date) -> dict[str, Any]:
    start, end = bounds(tenant, date_from, date_to)
    runs = list(db.scalars(select(SyncRun).where(
        SyncRun.tenant_id == tenant.id, SyncRun.started_at >= start, SyncRun.started_at < end)
        .order_by(SyncRun.started_at.desc())))
    finished = [r for r in runs if r.status != SyncStatus.running.value]
    durations = [r.duration_ms for r in finished if r.duration_ms is not None]
    per_day: dict[str, dict[str, int]] = defaultdict(lambda: {"runs": 0, "failed": 0, "punches_new": 0,
                                                              "attendances_created": 0, "attendances_closed": 0})
    tz = tenant.timezone or "UTC"
    for r in runs:
        started = ensure_aware(r.started_at).replace(tzinfo=None)
        day = per_day[utc_to_local(started, tz).date().isoformat()]
        day["runs"] += 1
        day["failed"] += r.status == SyncStatus.failed.value
        day["punches_new"] += r.punches_new or 0
        day["attendances_created"] += r.attendances_created or 0
        day["attendances_closed"] += r.attendances_closed or 0
    status = Counter(r.status for r in runs)
    return {
        "date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "timezone": tenant.timezone,
        "totals": {
            "runs": len(runs), "success": status[SyncStatus.success.value],
            "partial": status[SyncStatus.partial.value], "failed": status[SyncStatus.failed.value],
            "success_rate": round(100 * status[SyncStatus.success.value] / len(finished)) if finished else None,
            "avg_duration_ms": round(sum(durations) / len(durations)) if durations else None,
            "punches_new": sum(r.punches_new or 0 for r in runs),
            "attendances_created": sum(r.attendances_created or 0 for r in runs),
            "attendances_closed": sum(r.attendances_closed or 0 for r in runs),
            "errors": sum(r.error_count or 0 for r in runs),
        },
        "days": [{"date": k, **v} for k, v in sorted(per_day.items(), reverse=True)],
        "runs": [{
            "id": r.id, "started_at": _local(tenant, ensure_aware(r.started_at).replace(tzinfo=None)),
            "status": r.status, "trigger": r.triggered_by, "duration_ms": r.duration_ms,
            "punches_fetched": r.punches_fetched, "punches_new": r.punches_new,
            "attendances_created": r.attendances_created, "attendances_closed": r.attendances_closed,
            "errors": r.error_count, "error": r.error_message,
        } for r in runs[:ROW_LIMIT]],
    }

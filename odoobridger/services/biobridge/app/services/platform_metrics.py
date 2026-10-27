"""What BioBridge tells the OdooBridger core about itself (GET /platform/v1/metrics).

The core's staff console shows one row per service and one "Needs Attention"
line per failing customer integration. This builds that report from
aggregate queries over platform tenants only; standalone tenants on the same
BioBridge are not the core's business.

Definitions:

* integration: an active device source (``DeviceSource.is_active``);
  failed when its last check or sync left it ``failed``.
* failure "since": the first failed or partial sync run after the tenant's
  last successful one, else the source's last check.
* privacy: nothing customer-specific about connections leaves BioBridge —
  no device names, addresses, error text or Odoo URLs/messages. Failures are
  reported as counts with generic wording.

The ``dashboard`` block feeds the core's per-service staff page: tiles, a
daily series, one generic row per customer and a generic attention list.
* pending job: a punch not yet written to Odoo (``pending`` or ``error``,
  which are retried).
* scheduler ok: its heartbeat is no older than three ticks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Tenant
from app.models.attendance import EmployeeMapping, MappingStatus, PunchRecord, PunchState, SyncRun, SyncStatus
from app.models.connection import ConnectionStatus, Device, DeviceSource, OdooConnection
from app.services.scheduling import scheduler_health

PENDING_STATES = (PunchState.pending.value, PunchState.error.value)
DEGRADED_SHARE = 0.25
SERIES_DAYS = 30
DELAY_FLOOR = timedelta(minutes=30)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _failing_since(db: Session, tenant_ids: list[str]) -> dict[str, datetime]:
    """Per tenant: the first failed/partial run after its last successful one."""
    if not tenant_ids:
        return {}
    last_ok = dict(db.execute(
        select(SyncRun.tenant_id, func.max(SyncRun.started_at))
        .where(SyncRun.tenant_id.in_(tenant_ids), SyncRun.status == SyncStatus.success.value)
        .group_by(SyncRun.tenant_id)).all())
    bad = db.execute(
        select(SyncRun.tenant_id, SyncRun.started_at)
        .where(SyncRun.tenant_id.in_(tenant_ids),
               SyncRun.status.in_((SyncStatus.failed.value, SyncStatus.partial.value)))
        .order_by(SyncRun.started_at)).all()
    out: dict[str, datetime] = {}
    for tenant_id, started in bad:
        ok = last_ok.get(tenant_id)
        if tenant_id in out:
            continue
        if ok is None or _naive(started) > _naive(ok):
            out[tenant_id] = started
    return out


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _ago(delta: timedelta) -> str:
    """"1 day", "3 h", "40 min": how long ago, roughly."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds >= 86400:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''}"
    if seconds >= 3600:
        return f"{seconds // 3600} h"
    return f"{max(1, seconds // 60)} min"


def _counts(db: Session, column, ids: list[str], *where) -> dict[str, int]:
    if not ids:
        return {}
    return dict(db.execute(select(column, func.count()).where(column.in_(ids), *where).group_by(column)).all())


def collect(db: Session) -> dict[str, Any]:
    tenant_rows = db.scalars(select(Tenant).where(Tenant.platform_account_id.is_not(None))).all()
    account_of = {t.id: t.platform_account_id for t in tenant_rows}
    ids = list(account_of)

    sources = db.execute(
        select(DeviceSource.id, DeviceSource.tenant_id, DeviceSource.status, DeviceSource.last_checked_at)
        .where(DeviceSource.tenant_id.in_(ids), DeviceSource.is_active.is_(True))).all() if ids else []
    odoo_failing = db.execute(
        select(OdooConnection.tenant_id, OdooConnection.last_checked_at)
        .where(OdooConnection.tenant_id.in_(ids), OdooConnection.is_active.is_(True),
               OdooConnection.platform_connection_id.is_not(None),
               OdooConnection.status == ConnectionStatus.failed.value)).all() if ids else []
    since = _failing_since(db, [s.tenant_id for s in sources if s.status == ConnectionStatus.failed.value])

    per: dict[str, dict[str, Any]] = {tid: {"account_id": acc, "active": 0, "failed": 0, "failures": []}
                                      for tid, acc in account_of.items()}
    first_bad: dict[str, datetime | None] = {}
    for s in sources:
        row = per[s.tenant_id]
        row["active"] += 1
        if s.status == ConnectionStatus.failed.value:
            row["failed"] += 1
            first_bad.setdefault(s.tenant_id, since.get(s.tenant_id) or s.last_checked_at)
    for tenant_id, row in per.items():
        if row["failed"]:
            row["failures"].append({"key": "devices", "issue": "Sync failing",
                                    "detail": f"{row['failed']} of {row['active']} devices not syncing",
                                    "since": _iso(first_bad.get(tenant_id))})
    odoo_bad: dict[str, datetime | None] = {}
    for tenant_id, checked in odoo_failing:
        odoo_bad[tenant_id] = checked
        per[tenant_id]["failures"].append({"key": "odoo", "issue": "Odoo sync failing",
                                           "detail": "Attendance can't be written to Odoo.",
                                           "since": _iso(checked)})

    if ids:
        pending, oldest = db.execute(
            select(func.count(PunchRecord.id), func.min(PunchRecord.created_at))
            .where(PunchRecord.tenant_id.in_(ids), PunchRecord.process_state.in_(PENDING_STATES))).one()
        last_success = db.scalar(select(func.max(SyncRun.finished_at)).where(
            SyncRun.tenant_id.in_(ids), SyncRun.status == SyncStatus.success.value))
    else:
        pending, oldest, last_success = 0, None, None

    health = scheduler_health(db)
    active = sum(r["active"] for r in per.values())
    failed = sum(r["failed"] for r in per.values())
    status = "operational"
    if not health["running"] or (failed and failed / max(active, 1) >= DEGRADED_SHARE):
        status = "degraded"

    dashboard = _dashboard(db, tenant_rows, per, first_bad, odoo_bad, int(pending or 0))

    from app.main import app as fastapi_app   # the one place the version is declared

    return {
        "service": settings.platform_service_slug,
        "version": getattr(fastapi_app, "version", None),
        "status": status,
        "integrations": {"active": active, "failed": failed},
        "jobs": {"pending": int(pending or 0), "oldest_pending_at": _iso(oldest)},
        "last_success_at": _iso(last_success),
        "workers": {"scheduler": {"ok": bool(health["running"]), "last_run_at": _iso(health["last_tick_at"])}},
        "accounts": [r for r in per.values() if r["active"] or r["failures"]],
        "dashboard": dashboard,
    }


def _plan(tenant: Tenant) -> str | None:
    package = (tenant.platform_state or {}).get("package") or {}
    return package.get("name") or tenant.plan_name


def _dashboard(db: Session, tenants: list[Tenant], per: dict[str, dict[str, Any]],
               first_bad: dict[str, datetime | None], odoo_bad: dict[str, datetime | None],
               pending: int) -> dict[str, Any]:
    ids = [t.id for t in tenants]
    now = datetime.now(timezone.utc)
    devices = _counts(db, Device.tenant_id, ids)
    employees = _counts(db, EmployeeMapping.tenant_id, ids, EmployeeMapping.status == MappingStatus.mapped.value)
    last_ok = dict(db.execute(
        select(SyncRun.tenant_id, func.max(SyncRun.finished_at))
        .where(SyncRun.tenant_id.in_(ids), SyncRun.status == SyncStatus.success.value)
        .group_by(SyncRun.tenant_id)).all()) if ids else {}
    first_source = dict(db.execute(
        select(DeviceSource.tenant_id, func.min(DeviceSource.created_at))
        .where(DeviceSource.tenant_id.in_(ids), DeviceSource.is_active.is_(True))
        .group_by(DeviceSource.tenant_id)).all()) if ids else {}

    # Punches written to Odoo, by the day they were written (the ledger row's
    # last change is when it became synced).
    start = (now - timedelta(days=SERIES_DAYS - 1)).date()
    by_day: dict[str, int] = {}
    if ids:
        day = func.date(PunchRecord.updated_at)
        for d, n in db.execute(
                select(day, func.count()).where(
                    PunchRecord.tenant_id.in_(ids), PunchRecord.process_state == PunchState.synced.value,
                    PunchRecord.updated_at >= datetime.combine(start, datetime.min.time()))
                .group_by(day)).all():
            by_day[str(d)[:10]] = int(n)
    points = [{"date": (start + timedelta(days=i)).isoformat(),
               "value": by_day.get((start + timedelta(days=i)).isoformat(), 0)} for i in range(SERIES_DAYS)]

    accounts: dict[str, Any] = {}
    attention: list[dict[str, Any]] = []
    for t in tenants:
        acc = t.platform_account_id
        row = per[t.id]
        interval = t.sync_interval_minutes or None
        last = _aware(last_ok.get(t.id))
        grace = max(timedelta(minutes=3 * (interval or 15)), DELAY_FLOOR)
        if not row["active"]:
            health = "not_started"
        elif row["failed"] or t.id in odoo_bad:
            health = "failing"
        elif last is None:
            begun = _aware(first_source.get(t.id))
            health = "delayed" if begun and now - begun > grace else "healthy"
        elif now - last > grace:
            health = "delayed"
        else:
            health = "healthy"
        usage = {"devices": {"used": int(devices.get(t.id, 0)), "limit": t.plan_max_devices},
                 "employees": {"used": int(employees.get(t.id, 0)), "limit": t.plan_max_employees}}
        accounts[acc] = {**usage, "sync_interval": interval, "last_sync_at": _iso(last), "health": health}

        if row["failed"]:
            attention.append({"account_id": acc, "severity": "high", "title": "Sync failing",
                              "detail": f"{row['failed']} of {row['active']} devices not syncing.",
                              "since": _iso(first_bad.get(t.id))})
        elif t.id in odoo_bad:
            attention.append({"account_id": acc, "severity": "high", "title": "Sync failing",
                              "detail": "Attendance can't be written to Odoo.", "since": _iso(odoo_bad[t.id])})
        elif health == "delayed":
            detail = (f"Last sync {_ago(now - last)} ago; interval {interval or 15} min." if last
                      else f"No sync yet; interval {interval or 15} min.")
            attention.append({"account_id": acc, "severity": "medium", "title": "Sync delayed",
                              "detail": detail, "since": _iso(last)})
        plan = _plan(t)
        for key, noun in (("devices", "device"), ("employees", "employee")):
            used, limit = usage[key]["used"], usage[key]["limit"]
            if limit is not None and used >= limit:
                attention.append({"account_id": acc, "severity": "medium", "title": f"At {noun} limit",
                                  "detail": f"{used} of {limit} {key}" + (f" on {plan}." if plan else "."),
                                  "since": None})

    synced_30d = sum(p["value"] for p in points)
    return {
        "tiles": [
            {"key": "customers", "label": "Customers", "value": len(tenants), "icon": "users"},
            {"key": "devices", "label": "Devices Connected", "value": sum(a["devices"]["used"] for a in accounts.values()),
             "icon": "device"},
            {"key": "employees", "label": "Employees Connected",
             "value": sum(a["employees"]["used"] for a in accounts.values()), "icon": "badge"},
            {"key": "punches_30d", "label": "Punches (30 Days)", "value": synced_30d, "icon": "check", "tone": "ok"},
            {"key": "pending", "label": "Pending Sync", "value": pending, "icon": "clock",
             "tone": "warn" if pending else "ok"},
        ],
        "series": {"title": "Punches Synced Per Day", "subtitle": "all customers · last 30 days",
                   "unit": "punches", "points": points},
        "columns": [
            {"key": "devices", "label": "Devices", "kind": "usage"},
            {"key": "employees", "label": "Employees", "kind": "usage"},
            {"key": "sync_interval", "label": "Sync interval", "kind": "minutes"},
            {"key": "last_sync_at", "label": "Last sync", "kind": "ago"},
            {"key": "health", "label": "Sync health", "kind": "health"},
        ],
        "accounts": accounts,
        "attention": attention,
    }

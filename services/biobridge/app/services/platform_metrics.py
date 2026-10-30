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

The ``dashboard`` block feeds the core's per-service staff page: one row per
customer about how the service is running for it (automatic sync on or off,
last sync, activity, scheduler health) and a generic attention list. It
carries no counts of a customer's devices, employees or punches, and no
totals across customers: what a customer does with the service is theirs.
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
from app.models.attendance import PunchRecord, PunchState, SyncRun, SyncStatus
from app.models.connection import ConnectionStatus, DeviceSource, OdooConnection
from app.services.scheduling import scheduler_health

PENDING_STATES = (PunchState.pending.value, PunchState.error.value)
DEGRADED_SHARE = 0.25
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
                                    "detail": "Terminals aren't syncing",
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
    day_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    running = db.scalar(select(func.count(SyncRun.id)).where(SyncRun.status == SyncStatus.running.value)) or 0
    failed_runs = db.scalar(select(func.count(SyncRun.id)).where(
        SyncRun.status == SyncStatus.failed.value, SyncRun.started_at >= day_ago)) or 0
    active = sum(r["active"] for r in per.values())
    failed = sum(r["failed"] for r in per.values())
    status, reason = "operational", None
    if not health["running"]:
        status, reason = "degraded", "The scheduler has stopped, so automatic syncs are not running."
    elif failed and failed / max(active, 1) >= DEGRADED_SHARE:
        status = "degraded"
        reason = f"{failed} of {active} integrations are failing (the limit is {int(DEGRADED_SHARE * 100)}%)."

    dashboard = _dashboard(db, tenant_rows, per, first_bad, odoo_bad, int(pending or 0))

    from app.main import app as fastapi_app   # the one place the version is declared

    return {
        "service": settings.platform_service_slug,
        "version": getattr(fastapi_app, "version", None),
        "status": status,
        "status_reason": reason,
        "integrations": {"active": active, "failed": failed},
        "jobs": {"pending": int(pending or 0), "oldest_pending_at": _iso(oldest),
                 "running": int(running), "failed_24h": int(failed_runs)},
        "last_success_at": _iso(last_success),
        "workers": {"scheduler": {
            "ok": bool(health["running"]), "last_run_at": _iso(health["last_tick_at"]),
            "next_run_at": _iso(health["last_tick_at"] + timedelta(seconds=settings.scheduler_tick_seconds))
            if health["last_tick_at"] else None}},
        "requests": _requests(),
        "database": _database(db),
        "accounts": [r for r in per.values() if r["active"] or r["failures"]],
        "dashboard": dashboard,
    }


def _requests() -> dict[str, int]:
    from app.services import request_meter

    return request_meter.summary()


def _database(db: Session) -> dict[str, Any]:
    """Can BioBridge reach its database, and how quickly."""
    import time

    from sqlalchemy import text

    started = time.monotonic()
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return {"ok": False, "latency_ms": None}
    return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000, 1)}


def _plan(tenant: Tenant) -> str | None:
    package = (tenant.platform_state or {}).get("package") or {}
    return package.get("name") or tenant.plan_name


#: A customer counts as active when it synced within this long.
ACTIVE_WITHIN = timedelta(hours=24)


def _dashboard(db: Session, tenants: list[Tenant], per: dict[str, dict[str, Any]],
               first_bad: dict[str, datetime | None], odoo_bad: dict[str, datetime | None],
               pending: int) -> dict[str, Any]:
    ids = [t.id for t in tenants]
    now = datetime.now(timezone.utc)
    last_ok = dict(db.execute(
        select(SyncRun.tenant_id, func.max(SyncRun.finished_at))
        .where(SyncRun.tenant_id.in_(ids), SyncRun.status == SyncStatus.success.value)
        .group_by(SyncRun.tenant_id)).all()) if ids else {}
    first_source = dict(db.execute(
        select(DeviceSource.tenant_id, func.min(DeviceSource.created_at))
        .where(DeviceSource.tenant_id.in_(ids), DeviceSource.is_active.is_(True))
        .group_by(DeviceSource.tenant_id)).all()) if ids else {}

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
        if not row["active"] and last is None:
            activity = "not_started"
        elif last is not None and now - last <= ACTIVE_WITHIN:
            activity = "active"
        else:
            activity = "idle"
        accounts[acc] = {
            "sync_enabled": bool(t.sync_enabled) and t.syncable is not False,
            "sync_interval": interval, "last_sync_at": _iso(last),
            "activity": activity, "health": health,
        }

        if row["failed"]:
            attention.append({"account_id": acc, "severity": "high", "title": "Sync failing",
                              "detail": "Terminals aren't syncing.", "since": _iso(first_bad.get(t.id))})
        elif t.id in odoo_bad:
            attention.append({"account_id": acc, "severity": "high", "title": "Sync failing",
                              "detail": "Attendance can't be written to Odoo.", "since": _iso(odoo_bad[t.id])})
        elif health == "delayed":
            detail = (f"Last sync {_ago(now - last)} ago; interval {interval or 15} min." if last
                      else f"No sync yet; interval {interval or 15} min.")
            attention.append({"account_id": acc, "severity": "medium", "title": "Sync delayed",
                              "detail": detail, "since": _iso(last)})

    return {
        "tiles": [],
        "series": None,
        "columns": [
            {"key": "sync_enabled", "label": "Automatic sync", "kind": "onoff"},
            {"key": "last_sync_at", "label": "Last sync", "kind": "ago"},
            {"key": "activity", "label": "Activity", "kind": "activity"},
            {"key": "health", "label": "Scheduler health", "kind": "health"},
        ],
        "accounts": accounts,
        "attention": attention,
    }

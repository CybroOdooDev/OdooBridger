"""Win back a connection that stopped working, without waiting for a sync.

A connection is marked failed or degraded when a sync runs into trouble, and
until now it only went back to Connected when the next sync succeeded — or when
somebody pressed Test Connection. A customer who fixed the problem and signed in
saw the old status, and the slower "slow lane" interval kept the next sync far
away.

Two triggers, one routine (``recheck``):

* a customer signing in — in the background, so the sign-in itself is not slowed;
* the scheduler, every few minutes, for accounts that have a connection in trouble.

Cost is the reason it is built this way: only connections that are **not**
connected are probed, and none more often than ``MIN_AGE``. An account whose
connections are healthy costs nothing here — their status is already as fresh as
their last sync. A probe is the same check the Test Connection button runs
(a handful of calls to Odoo, one sign-in to the biometric server).

When everything checks out, the failure streak is cleared (which ends the slow
lane) and, if the last sync failed or punches are waiting, one catch-up sync runs
now rather than at the next slow-lane tick. Punches are kept in the ledger
meanwhile, so the catch-up loses nothing and a second one cannot start
(``run_cycle`` hands back the cycle already running).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import session_scope
from app.models import (
    ConnectionStatus,
    DeviceSource,
    OdooConnection,
    PunchRecord,
    PunchState,
    SyncRun,
    SyncStatus,
    Tenant,
)
from app.services.scheduling import SYNCABLE

log = logging.getLogger(__name__)

#: A connection checked more recently than this is left alone: repeated sign-ins
#: or ticks must not hammer a server that is already struggling.
MIN_AGE = timedelta(seconds=90)

#: Statuses worth re-checking. Connected is left to the sync that keeps it fresh.
UNHEALTHY = (
    ConnectionStatus.failed.value,
    ConnectionStatus.degraded.value,
    ConnectionStatus.unverified.value,
)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _due(row, now: datetime) -> bool:
    if row.status not in UNHEALTHY:
        return False
    checked = _aware(row.last_checked_at)
    return checked is None or now - checked >= MIN_AGE


def tenants_in_trouble(db: Session) -> list[str]:
    """Accounts with an active connection that is not connected — the only ones
    a recovery tick looks at."""
    in_trouble: set[str] = set()
    for model in (OdooConnection, DeviceSource):
        in_trouble.update(db.scalars(
            select(model.tenant_id).where(model.is_active.is_(True), model.status.in_(UNHEALTHY))
        ))
    if not in_trouble:
        return []
    return list(db.scalars(
        select(Tenant.id).where(Tenant.id.in_(in_trouble), Tenant.status.in_(SYNCABLE))
    ))


def recheck(db: Session, tenant: Tenant, *, now: datetime | None = None) -> dict:
    """Probe this account's unhealthy connections and, if they all pass, clear the
    failure streak. Returns what happened. Never raises."""
    # Imported here: the probes live with the API that the Test Connection buttons
    # call, and importing that module at load time would be circular.
    from app.api.v1 import connections as api

    now = now or datetime.now(timezone.utc)
    seat = SimpleNamespace(tenant=tenant)       # the probes only read .tenant
    result = {"probed": 0, "recovered": 0, "healthy": False, "streak_cleared": False}
    refused = False         # a probe that reached the server but found it unusable

    conn = db.scalars(
        select(OdooConnection)
        .where(OdooConnection.tenant_id == tenant.id, OdooConnection.is_active.is_(True))
        .limit(1)
    ).first()
    sources = list(db.scalars(
        select(DeviceSource).where(DeviceSource.tenant_id == tenant.id, DeviceSource.is_active.is_(True))
    ))

    for row, probe in [(conn, api._probe_odoo), *[(s, api._probe_source) for s in sources]]:
        if row is None or not _due(row, now):
            continue
        try:
            outcome = probe(seat, row)
        except Exception:  # noqa: BLE001 — one bad probe must not stop the others
            log.debug("probe failed for %s", getattr(row, "name", "odoo"), exc_info=True)
            continue
        result["probed"] += 1
        if outcome.ok:
            result["recovered"] += 1
        else:
            refused = True
    db.flush()

    # Only a streak that a probe just disproved is cleared. Failures with every
    # connection already connected have some other cause (a bug, a bad record),
    # and clearing them here would hide it.
    checked = ([conn] if conn is not None else []) + sources
    result["healthy"] = bool(checked) and not refused and all(
        r.status == ConnectionStatus.connected.value for r in checked
    )
    if result["healthy"] and result["probed"] and tenant.consecutive_failures:
        tenant.consecutive_failures = 0
        result["streak_cleared"] = True
        db.flush()
    return result


def _catch_up_needed(db: Session, tenant: Tenant) -> bool:
    """True when the last sync failed or punches are waiting to be sent."""
    last = db.scalars(
        select(SyncRun).where(SyncRun.tenant_id == tenant.id)
        .order_by(SyncRun.started_at.desc()).limit(1)
    ).first()
    if last is not None and last.status == SyncStatus.failed.value:
        return True
    waiting = db.scalar(
        select(func.count()).select_from(PunchRecord).where(
            PunchRecord.tenant_id == tenant.id,
            PunchRecord.process_state.in_([PunchState.pending.value, PunchState.error.value]),
        )
    )
    return bool(waiting)


def recheck_tenant(tenant_id: str) -> dict | None:
    """The whole routine on its own database session — what a background task or
    the scheduler calls. Cheap when nothing is wrong."""
    from app.services.sync_engine import SyncEngine, active_run

    try:
        with session_scope() as db:
            tenant = db.get(Tenant, tenant_id)
            if tenant is None or tenant.status not in SYNCABLE:
                return None
            result = recheck(db, tenant)
            if (result["streak_cleared"] or result["recovered"]) and result["healthy"] \
                    and active_run(db, tenant.id) is None and _catch_up_needed(db, tenant):
                db.commit()                          # statuses are visible before the sync starts
                run = SyncEngine(db, tenant, "maintenance").run_cycle()
                result["catch_up"] = run.status
                log.info("Connections recovered for %s; catch-up sync %s", tenant.slug, run.status)
            return result
    except Exception:  # noqa: BLE001 — a background check must never surface as an error
        log.warning("Connection recheck failed for %s", tenant_id, exc_info=True)
        return None

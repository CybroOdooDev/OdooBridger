"""When a tenant starts syncing into a different Odoo database.

Employee links (badge -> hr.employee id), the record of open shifts, the
local mirror's Odoo attendance ids, device companies and queued device
removals all point at records in one particular Odoo database. Ids are only
meaningful there: employee #2 in the new database is a different person, and
attendance #109 may already exist. So when the active Odoo becomes a different
database (another address or database name), everything tied to the old one
is let go:

* badges that only came from the roster (never punched) are removed; badges
  that have punched go back to *unmapped*, to be matched in the new Odoo on
  the next sync (badges a user chose to ignore or delete stay that way);
* open shifts are forgotten (they live in the old Odoo);
* the attendance history stays for reports but no longer claims Odoo ids, so
  it can't collide with the new database's ids or be "refreshed" from it;
* device companies and queued device removals (old Odoo records) are cleared.

Punches themselves are kept: those already written stay as history, those
still pending go to the new Odoo once their badge is matched there.

``Tenant.odoo_identity`` remembers which database the links belong to.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import (
    AttendanceRecord, Device, EmployeeMapping, MappingStatus, OdooConnection, OdooDeviceRemoval, Tenant,
)

log = logging.getLogger(__name__)

#: Statuses that are a user's decision about the badge itself, not about Odoo.
KEEP = (MappingStatus.ignored.value, MappingStatus.removed.value)


def identity(url: str | None, db_name: str | None) -> str:
    u = (url or "").strip().lower().split("://", 1)[-1].rstrip("/").removeprefix("www.")
    return f"{u}|{(db_name or '').strip().lower()}"


def active_connection(db: Session, tenant: Tenant) -> OdooConnection | None:
    return db.scalars(
        select(OdooConnection)
        .where(OdooConnection.tenant_id == tenant.id, OdooConnection.is_active.is_(True))
        .order_by(OdooConnection.platform_connection_id.is_(None))
        .limit(1)
    ).first()


def release_old_odoo(db: Session, tenant: Tenant) -> dict[str, int]:
    """Let go of everything tied to the previous Odoo database (see above)."""
    rows = db.scalars(select(EmployeeMapping).where(EmployeeMapping.tenant_id == tenant.id)).all()
    removed = reset = 0
    for m in rows:
        if m.status in KEEP:
            continue
        if m.last_punch_at is None:
            db.delete(m)
            removed += 1
            continue
        m.status = MappingStatus.unmapped.value
        m.odoo_employee_id = None
        m.odoo_employee_name = None
        m.odoo_company_id = m.odoo_company_name = None
        m.odoo_department_name = None
        m.odoo_manager_id = m.odoo_manager_name = None
        m.match_method = None
        m.match_note = "The Odoo connection changed; this badge will be matched in the new Odoo."
        m.open_attendance_id = None
        m.open_check_in = None
        reset += 1
    detached = db.execute(
        update(AttendanceRecord)
        .where(AttendanceRecord.tenant_id == tenant.id, AttendanceRecord.odoo_attendance_id.is_not(None))
        .values(odoo_attendance_id=None)
    ).rowcount or 0
    db.execute(update(Device).where(Device.tenant_id == tenant.id).values(company_id=None, company_name=None))
    db.execute(delete(OdooDeviceRemoval).where(OdooDeviceRemoval.tenant_id == tenant.id))
    db.flush()
    log.info("Tenant %s: Odoo changed; %s badge(s) removed, %s to re-match, %s attendance record(s) detached",
             tenant.slug, removed, reset, detached)
    return {"removed": removed, "reset": reset, "detached": detached}


def ensure_current(db: Session, tenant: Tenant) -> dict[str, int] | None:
    """Compare the active Odoo with the one the links belong to; release the
    old one's links when they differ. Cheap; call it whenever the connection
    may have changed and at the start of every sync. Returns what was released,
    or None when nothing changed."""
    conn = active_connection(db, tenant)
    if conn is None:
        return None   # disconnected: keep everything, the same Odoo may come back
    now = identity(conn.url, conn.db_name)
    before = tenant.odoo_identity
    if before == now:
        return None
    if before is None:
        # Recorded for the first time (tenants from before this existed). Only
        # treat it as a switch if a different Odoo connection was used before.
        others = db.scalars(select(OdooConnection).where(
            OdooConnection.tenant_id == tenant.id, OdooConnection.id != conn.id)).all()
        switched = any(identity(o.url, o.db_name) != now for o in others)
        tenant.odoo_identity = now
        if not switched:
            db.flush()
            return None
    tenant.odoo_identity = now
    return release_old_odoo(db, tenant)

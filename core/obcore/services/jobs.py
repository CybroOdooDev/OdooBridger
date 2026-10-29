"""Background work in the core: the static staff login, ended trials, and
re-sending provisioning a service has not acknowledged."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.config import settings
from obcore.db import session_scope
from obcore.mailer import try_send
from obcore.models import OdooConnection, StaffUser
from obcore.security import aware, hash_password, utcnow, verify_password
from obcore.services import billing, odoo_connections, provisioning, signups
from obcore.services.accounts import audit, owner_of

log = logging.getLogger(__name__)


def ensure_static_staff(db: Session) -> StaffUser | None:
    """Create or update the STAFF_EMAIL / STAFF_PASSWORD login."""
    email = settings.staff_email.strip().lower()
    password = settings.staff_password
    if not email or not password:
        if email or password:
            log.warning("STAFF_EMAIL and STAFF_PASSWORD must both be set; ignoring")
        return None
    if len(password) < 10:
        log.warning("STAFF_PASSWORD is shorter than 10 characters; static staff login not applied")
        return None
    staff = db.scalar(select(StaffUser).where(StaffUser.email == email))
    if staff is None:
        staff = StaffUser(email=email, full_name="Staff", hashed_password=hash_password(password), is_static=True)
        db.add(staff)
        log.info("Created the static staff login %s", email)
    else:
        if not verify_password(password, staff.hashed_password):
            staff.hashed_password = hash_password(password)
        staff.is_active = True
        staff.is_static = True
        staff.failed_login_count = 0
        staff.locked_until = None
    db.flush()
    return staff


def sweep_once(db: Session) -> dict[str, int]:
    expired = billing.expire_trials(db)
    for row in expired:
        audit(db, "system", "service.trial_ended", account_id=row.account_id, detail=row.service.slug)
        owner = owner_of(row.account)
        if owner:
            try_send(owner.email, f"Your {row.service.name} trial has ended",
                     f"Hi,\n\nThe free trial of {row.service.name} for {row.account.name} has ended, so it "
                     f"has stopped working. Your data is kept. Add payment details to carry on:\n"
                     f"{settings.base}/app/#/billing\n\n— OdooBridger\n")
    for row in billing.apply_pending_changes(db):
        audit(db, "system", "service.package", account_id=row.account_id,
              detail=f"{row.service.slug} -> {row.package.name}")
    pushed = provisioning.push_pending(db)
    checked = refresh_odoo_readiness(db)
    notified = odoo_connections.notify_failures(db)
    signups.clear_expired(db)
    return {"trials_ended": len(expired), "provisioned": pushed, "odoo_checked": checked, "odoo_notices": notified}


def refresh_odoo_readiness(db: Session, *, now=None) -> int:
    """Read each Odoo service's own readiness for connections not checked lately."""
    now = now or utcnow()
    due = now - timedelta(minutes=settings.odoo_readiness_minutes)
    checked = 0
    for conn in db.scalars(select(OdooConnection)):
        rows = [r for r in odoo_connections.bound_rows(conn) if r.usable]
        if rows and any(r.odoo_readiness_at is None or aware(r.odoo_readiness_at) < due for r in rows):
            odoo_connections.refresh_readiness(db, conn)
            checked += 1
    return checked


async def loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_tick)
        except Exception:  # noqa: BLE001 — the loop must survive anything
            log.exception("Background sweep failed")
        await asyncio.sleep(settings.sweep_interval_seconds)


async def alert_loop() -> None:
    """Check Needs Attention for alerts, apart from the slower sweep."""
    while True:
        try:
            await asyncio.to_thread(_alert_tick)
        except Exception:  # noqa: BLE001 — the loop must survive anything
            log.exception("Alert check failed")
        await asyncio.sleep(settings.alert_interval_seconds)


def _alert_tick() -> None:
    from obcore.services import alerts

    from obcore import locks

    with session_scope() as db:
        # One process checks; the others skip this round (no double alerts).
        if not locks.try_for_transaction(db, locks.ALERTS):
            return
        result = alerts.check(db)
        if any(result.values()):
            log.info("Alerts: %s", result)


def _tick() -> None:
    with session_scope() as db:
        from obcore.services import stripe_api

        stripe_api.load_config(db)
        from obcore import locks

        # One process sweeps; the others skip this round (no double trial
        # emails, no two pushes of the same account at once).
        if not locks.try_for_transaction(db, locks.SWEEP):
            return
        result = sweep_once(db)
        try:
            from obcore.services.ops import record_snapshot

            record_snapshot(db)
        except Exception:  # noqa: BLE001 — a figure for the overview, never worth failing the sweep
            log.exception("Daily snapshot failed")
        if any(result.values()):
            log.info("Sweep: %s", result)

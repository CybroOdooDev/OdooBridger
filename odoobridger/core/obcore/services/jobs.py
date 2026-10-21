"""Background work in the core: the static staff login, ended trials, and
re-sending provisioning a service has not acknowledged."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.config import settings
from obcore.db import session_scope
from obcore.mailer import try_send
from obcore.models import StaffUser
from obcore.security import hash_password, verify_password
from obcore.services import billing, provisioning
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
    pushed = provisioning.push_pending(db)
    return {"trials_ended": len(expired), "provisioned": pushed}


async def loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_tick)
        except Exception:  # noqa: BLE001 — the loop must survive anything
            log.exception("Background sweep failed")
        await asyncio.sleep(settings.sweep_interval_seconds)


def _tick() -> None:
    with session_scope() as db:
        from obcore.services import stripe_api

        stripe_api.load_config(db)
        result = sweep_once(db)
        if any(result.values()):
            log.info("Sweep: %s", result)

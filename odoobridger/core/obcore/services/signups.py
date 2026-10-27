"""Website signups: nothing is created until the email address is confirmed.

``start`` keeps what the signup form said in a ``PendingSignup`` and emails a
link. Only ``complete`` — the person opened that link and chose a password —
creates the account, its owner and its services (so the trial starts then),
tells the services, and, for "Buy Now", starts Stripe Checkout. An address
nobody confirms leaves no account, no trial and nothing in any service; the
row expires and is cleared by the sweep.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from obcore.config import settings
from obcore.mailer import try_send
from obcore.models import Account, PendingSignup, Service, ServicePackage, User
from obcore.security import aware, hash_password, hash_token, new_token, utcnow
from obcore.services import accounts as acc
from obcore.services import billing


class SignupError(ValueError):
    """Safe to show."""


def start(db: Session, *, email: str, company: str, full_name: str | None, country: str | None,
          timezone_name: str | None, services: list[dict[str, Any]], interval: str, mode: str,
          ip: str | None) -> PendingSignup:
    """Keep the signup and email the confirmation link. Replaces an earlier unconfirmed one."""
    email = acc.normalise_email(email)
    if db.scalar(select(User.id).where(User.email == email)):
        raise SignupError("An account with this email already exists. Sign in instead.")
    pending = db.scalar(select(PendingSignup).where(PendingSignup.email == email))
    if pending is None:
        pending = PendingSignup(email=email)
        db.add(pending)
    pending.company = company.strip()
    pending.full_name = (full_name or "").strip() or None
    pending.country = country
    pending.timezone = timezone_name
    pending.services = services
    pending.interval = "year" if interval == "year" else "month"
    pending.mode = "buy" if mode == "buy" else "trial"
    pending.ip_address = ip
    send(pending)
    db.flush()
    return pending


def send(pending: PendingSignup) -> None:
    """A fresh link (the previous one stops working) and the email carrying it."""
    raw = new_token()
    pending.token_hash = hash_token(raw)
    pending.expires_at = utcnow() + timedelta(hours=settings.email_verification_ttl_hours)
    pending.sent_at = utcnow()
    first = (pending.full_name or pending.email).split()[0]
    then = ("Once it's confirmed you'll go straight on to payment."
            if pending.mode == "buy" else "Your free trial starts when you confirm.")
    body = (f"Hi {first},\n\nThanks for signing up {pending.company} for OdooBridger.\n\n"
            f"Confirm your email and choose your password here:\n{acc.link_url(raw, acc.PURPOSE_VERIFY)}\n\n"
            f"{then} Your account is created only once you do; until then nothing is set up.\n\n"
            f"The link works once and expires in {settings.email_verification_ttl_hours} hours. "
            f"Your login is this email address and the password you choose.\n\n— OdooBridger\n")
    try_send(pending.email, "Confirm your email to create your OdooBridger account", body)


def for_link(db: Session, raw: str) -> PendingSignup | None:
    if not raw:
        return None
    pending = db.scalar(select(PendingSignup).where(PendingSignup.token_hash == hash_token(raw)))
    if pending is None:
        return None
    expires = aware(pending.expires_at)
    if expires is None or expires < utcnow():
        return None
    return pending


def complete(db: Session, pending: PendingSignup, password: str) -> tuple[Account, User]:
    """The address is confirmed: create the account, its owner and its services.

    Flushes, doesn't commit. The caller tells the services and starts Checkout.
    """
    if db.scalar(select(User.id).where(User.email == pending.email)):
        db.delete(pending)
        raise SignupError("An account with this email already exists. Sign in instead.")
    try:
        account, user = acc.create_account(db, company=pending.company, email=pending.email,
                                           full_name=pending.full_name, country=pending.country,
                                           timezone_name=pending.timezone)
    except acc.AccountError as exc:
        raise SignupError(str(exc)) from exc
    account.billing_interval = pending.interval
    for choice in pending.services or []:
        service = db.scalar(select(Service).where(Service.slug == choice.get("service"),
                                                  Service.is_active.is_(True)))
        if service is None:
            continue   # withdrawn since the signup; the rest still go ahead
        package = db.get(ServicePackage, choice["package_id"]) if choice.get("package_id") else None
        if package is not None and (package.service_id != service.id or not package.is_active):
            package = None   # that package is gone: the service's default instead
        try:
            billing.add_service(db, account, service, package)
        except billing.SubscriptionError:
            continue
    user.hashed_password = hash_password(password)
    user.email_verified_at = utcnow()
    user.last_login_at = utcnow()
    db.delete(pending)
    db.flush()
    return account, user


def clear_expired(db: Session) -> int:
    result = db.execute(delete(PendingSignup).where(PendingSignup.expires_at < utcnow()))
    return result.rowcount or 0

"""Single sign-on into services.

1. The user, signed in to the core, opens a service. The service sends the
   browser to ``/app/#/open/<slug>?redirect_uri=…&state=…`` on the core.
2. The portal calls ``POST /api/v1/sso/authorize``; the core checks the
   account has that service and the redirect is the service's own, and
   returns ``redirect_uri?code=…&state=…`` with a one-time code (60 s).
3. The service's backend posts the code with its client secret to
   ``POST /api/v1/sso/token`` and receives an RS256 identity token
   (``typ=identity``, ``aud=<slug>``) carrying the user, the account and the
   account's status and entitlements for that service. It creates or updates
   its tenant and user from that, and opens its own session.

The code travels through the browser; the identity token never does.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from obcore import keys
from obcore.config import settings
from obcore.models import AccountService, Service, SsoCode, User
from obcore.security import aware, hash_token, new_token, utcnow
from obcore.services.provisioning import effective_status, payload_for


class SsoError(ValueError):
    pass


def service_by_slug(db: Session, slug: str) -> Service | None:
    return db.scalar(select(Service).where(Service.slug == slug))


def default_redirect(service: Service) -> str:
    return f"{service.base_url.rstrip('/')}{service.sso_callback_path}"


def _same_origin(a: str, b: str) -> bool:
    x, y = urlsplit(a), urlsplit(b)
    return (x.scheme, x.netloc.lower()) == (y.scheme, y.netloc.lower())


def authorize(db: Session, user: User, slug: str, redirect_uri: str | None, state: str | None) -> str:
    service = service_by_slug(db, slug)
    if service is None or not service.is_active:
        raise SsoError("That service is not available.")
    row = db.scalar(select(AccountService).where(
        AccountService.account_id == user.account_id, AccountService.service_id == service.id))
    if row is None:
        raise SsoError(f"Your account does not have {service.name} yet. Add it from Services first.")
    target = redirect_uri or default_redirect(service)
    if not _same_origin(target, service.base_url):
        raise SsoError("That sign-in link does not belong to this service.")
    raw = new_token(24)
    db.add(SsoCode(code_hash=hash_token(raw), user_id=user.id, service_id=service.id,
                   redirect_uri=target,
                   expires_at=utcnow() + timedelta(seconds=settings.sso_code_ttl_seconds)))
    query = {"code": raw}
    if state:
        query["state"] = state
    # The service's dashboard uses hash routing, so the query may sit after "#/sso".
    sep = "&" if "?" in target.split("#")[-1] else "?"
    return f"{target}{sep}{urlencode(query)}"


def exchange(db: Session, client_id: str, client_secret: str, code: str) -> str:
    service = service_by_slug(db, client_id)
    if service is None or not service.client_secret_hash \
            or hash_token(client_secret or "") != service.client_secret_hash:
        raise SsoError("Unknown client or wrong client secret.")
    row = db.scalar(select(SsoCode).where(SsoCode.code_hash == hash_token(code or "")))
    if row is None or row.service_id != service.id:
        raise SsoError("Unknown sign-in code.")
    if row.used_at is not None:
        raise SsoError("This sign-in code was already used.")
    if aware(row.expires_at) < utcnow():
        raise SsoError("This sign-in code has expired. Open the service again from OdooBridger.")
    # Spent in one statement, so two exchanges of the same code at once can't
    # both see it unused: only the one whose update matched goes on.
    spent = db.execute(update(SsoCode).where(SsoCode.id == row.id, SsoCode.used_at.is_(None))
                       .values(used_at=utcnow()))
    if spent.rowcount != 1:
        raise SsoError("This sign-in code was already used.")
    db.expire(row, ["used_at"])
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise SsoError("This user can no longer sign in.")
    subscription = db.scalar(select(AccountService).where(
        AccountService.account_id == user.account_id, AccountService.service_id == service.id))
    if subscription is None:
        raise SsoError("The account no longer has this service.")
    state = payload_for(subscription)
    claims = {
        "sub": user.id,
        "email": user.email,
        "name": user.full_name,
        "role": user.role,
        "account": state["account"],
        "status": effective_status(subscription),
        "package": state["package"],
        "entitlements": state["entitlements"],
        "trial_ends_at": state["trial_ends_at"],
        "renews_at": state["renews_at"],
        "revision": subscription.revision,
        "portal_url": f"{settings.base}/app/",
    }
    return keys.sign_service_token(service.slug, "identity", claims, db=db)


def rotate_client_secret(service: Service) -> str:
    raw = new_token(32)
    service.client_secret_hash = hash_token(raw)
    return raw

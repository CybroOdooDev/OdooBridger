"""Telling services about the accounts they serve.

The core is the authority on who an account is, which package it has, and
whether it may use a service. Every change to an ``AccountService`` bumps its
``revision``; ``push`` sends the whole current state to the service
(idempotent PUT), and the row records the revision the service acknowledged.
Anything not yet acknowledged — the service was down, a deploy was in
progress — is sent again by the background sweep, so the two converge.

Wire format (``PUT {service}/platform/v1/accounts/{account_id}``)::

    {"account": {"id", "name", "slug", "timezone", "country"},
     "status": "trialing|active|past_due|suspended|cancelled",
     "package": {"id", "name"} | null,
     "entitlements": {...},
     "trial_ends_at": iso | null, "renews_at": iso | null,
     "users": [{"id", "email", "full_name", "role"}],
     "odoo": {"id", "name", "url", "db", "login", "api_key", "version", "api",
              "companies", "status"} | null,      # only for services that use Odoo
     "revision": int}

``odoo`` is present only when ``Service.uses_odoo``; a service that does not
work on Odoo never receives the key. Null means the account has no Odoo
connection (any connection the service held should stop being used).

Authorization: ``Bearer <RS256 token>`` with ``typ=provision``,
``aud=<service slug>``, ``account_id`` and ``body_sha256`` of the exact body.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore import keys
from obcore.config import settings
from obcore.models import Account, AccountService, AccountStatus, Service, ServiceStatus
from obcore.security import utcnow

log = logging.getLogger(__name__)

#: Builds the HTTP client used for service calls. Tests swap it for a client
#: that routes into a service app in-process.
client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=settings.http_timeout_seconds)


class ProvisionError(RuntimeError):
    pass


def effective_status(row: AccountService) -> str:
    """What the service should do for this account: the account's own state wins."""
    if row.account.status == AccountStatus.suspended.value:
        return ServiceStatus.suspended.value
    if row.account.status == AccountStatus.closed.value:
        return ServiceStatus.cancelled.value
    return row.status


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def payload_for(row: AccountService) -> dict[str, Any]:
    account = row.account
    return {
        "account": {"id": account.id, "name": account.name, "slug": account.slug,
                    "timezone": account.timezone, "country": account.country},
        "status": effective_status(row),
        "package": {"id": row.package.id, "name": row.package.name} if row.package else None,
        "entitlements": row.effective_entitlements(),
        "trial_ends_at": _iso(row.trial_ends_at),
        "renews_at": _iso(account.renews_at),
        "users": [{"id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role}
                  for u in account.users if u.is_active],
        "revision": row.revision,
    } | _odoo_part(row)


def _odoo_part(row: AccountService) -> dict[str, Any]:
    if not row.service.uses_odoo:
        return {}
    from obcore.services.odoo_connections import provision_block

    return {"odoo": provision_block(row)}


def _auth(service: Service, account_id: str, body: bytes, db: Session | None) -> dict[str, str]:
    token = keys.sign_service_token(
        service.slug, "provision",
        {"sub": "odoobridger-core", "account_id": account_id, "body_sha256": keys.body_digest(body)},
        ttl_seconds=120, db=db)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _url(service: Service, account_id: str, suffix: str = "") -> str:
    return f"{service.api_url}/platform/v1/accounts/{account_id}{suffix}"


def _call(method: str, url: str, headers: dict[str, str], body: bytes = b"") -> httpx.Response:
    try:
        with client_factory() as client:
            response = client.request(method, url, content=body or None, headers=headers)
    except httpx.HTTPError as exc:
        raise ProvisionError(f"Could not reach the service: {exc}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:200]
        raise ProvisionError(f"The service answered {response.status_code}: {detail}")
    return response


def mark_changed(row: AccountService) -> None:
    row.revision = (row.revision or 0) + 1


def mark_account_changed(account: Account) -> None:
    for row in account.services:
        mark_changed(row)


def push(db: Session, row: AccountService) -> bool:
    """Send one account-service's current state. Records the outcome; never raises."""
    service = row.service
    if not service.is_active:
        return False
    body = keys.dumps(payload_for(row))
    try:
        _call("PUT", _url(service, row.account_id), _auth(service, row.account_id, body, db), body)
    except ProvisionError as exc:
        row.provision_error = str(exc)[:400]
        log.warning("Provisioning %s on %s failed: %s", row.account_id, service.slug, exc)
        return False
    row.provisioned_at = utcnow()
    row.provisioned_revision = row.revision
    row.provision_error = None
    return True


def push_account(db: Session, account: Account) -> None:
    for row in account.services:
        if row.provisioned_revision < row.revision:
            push(db, row)


def push_pending(db: Session, limit: int = 100) -> int:
    rows = db.scalars(select(AccountService).where(
        AccountService.provisioned_revision < AccountService.revision).limit(limit)).all()
    done = sum(1 for row in rows if push(db, row))
    return done


def remove(db: Session, service: Service, account_id: str) -> None:
    """Ask a service to delete everything it holds for an account. Raises on failure."""
    _call("DELETE", _url(service, account_id), _auth(service, account_id, b"", db))


def usage(db: Session, row: AccountService, *, check_odoo: bool = False) -> dict[str, Any]:
    """The service's own numbers for an account (and its Odoo readiness).

    ``check_odoo`` asks the service to test its Odoo access live rather than
    report what it last saw.
    """
    response = _call("GET", _url(row.service, row.account_id, "/usage" + ("?check_odoo=1" if check_odoo else "")),
                     _auth(row.service, row.account_id, b"", db))
    return response.json()

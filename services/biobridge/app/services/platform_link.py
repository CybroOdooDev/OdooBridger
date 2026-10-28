"""BioBridge as the "biometric" service of an OdooBridger core.

The core owns who an account is, who its people are, which package it has and
whether it may be used. It tells BioBridge in two ways, both signed with the
core's RS256 key (verified here with its public key):

* **Provisioning** — the core PUTs an account's whole state to
  ``/platform/v1/accounts/{id}`` whenever it changes (and DELETEs it when the
  account is deleted). ``apply_state`` makes the tenant match.
* **SSO** — a user opening BioBridge from the portal arrives with a one-time
  code; ``exchange_code`` trades it (with this service's client secret) for an
  identity token, which carries the same account state, so a tenant missing a
  provisioning call is still created correctly on first sign-in.

The account's package entitlements land on the tenant's existing per-account
limits (``limit_max_devices`` …), so every limit already enforced keeps working
unchanged; in platform mode a tenant has no BioBridge plan of its own.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from typing import Any

import httpx
from jose import JWTError, jwt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.models import Tenant, TenantStatus, User, UserRole, UserSession

log = logging.getLogger(__name__)

#: Core role -> BioBridge role. A core "member" may look but not change.
ROLE_MAP = {"owner": UserRole.owner.value, "admin": UserRole.admin.value, "member": UserRole.viewer.value}
STATUSES = {s.value for s in TenantStatus}
#: Entitlement key -> Tenant limit column.
LIMITS = {
    "max_devices": "limit_max_devices",
    "max_employees": "limit_max_employees",
    "min_sync_interval_minutes": "limit_min_sync_interval_minutes",
}

_key_lock = threading.Lock()
_key_cache: tuple[str, float] | None = None
KEY_TTL_SECONDS = 3600


class PlatformError(ValueError):
    """The core's message could not be trusted or used. Safe to show."""


# =============================================================================
# The core's public key and token checks
# =============================================================================
def core_public_key(force: bool = False) -> str:
    global _key_cache
    if settings.platform_public_key_pem.strip():
        return settings.platform_public_key_pem.strip().replace("\\n", "\n")
    with _key_lock:
        if _key_cache and not force and time.monotonic() - _key_cache[1] < KEY_TTL_SECONDS:
            return _key_cache[0]
        try:
            response = httpx.get(f"{settings.platform_core_api}/api/v1/sso/public-key",
                                 timeout=settings.http_timeout_seconds)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            if _key_cache:
                return _key_cache[0]
            raise PlatformError(f"Could not fetch OdooBridger's public key: {exc}") from exc
        _key_cache = (response.text.strip(), time.monotonic())
        return _key_cache[0]


def reset_key_cache() -> None:
    global _key_cache
    _key_cache = None


def verify(token: str, typ: str) -> dict[str, Any]:
    """Decode a core token for this service. One retry with a fresh key, so a
    key rotation on the core is picked up without a restart."""
    for attempt in (0, 1):
        try:
            claims = jwt.decode(token, core_public_key(force=attempt == 1), algorithms=["RS256"],
                                audience=settings.platform_service_slug)
            break
        except JWTError as exc:
            if attempt == 1 or settings.platform_public_key_pem.strip():
                raise PlatformError(f"Untrusted token: {exc}") from exc
    if claims.get("typ") != typ:
        raise PlatformError("Wrong token type.")
    return claims


# =============================================================================
# Applying the core's view of an account
# =============================================================================
def _slug(db: Session, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "tenant"
    slug, n = base, 1
    while db.scalar(select(func.count(Tenant.id)).where(Tenant.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def tenant_for(db: Session, account_id: str) -> Tenant | None:
    return db.scalar(select(Tenant).where(Tenant.platform_account_id == account_id))


def apply_state(db: Session, state: dict[str, Any]) -> Tenant:
    """Create or update the tenant for one core account. Flushes, doesn't commit.

    An older revision than the one already applied is ignored: two updates
    arriving out of order must not undo the newer one.
    """
    account = state.get("account") or {}
    account_id = account.get("id")
    if not account_id:
        raise PlatformError("The account has no id.")
    status = state.get("status")
    if status not in STATUSES:
        raise PlatformError(f"Unknown status {status!r}.")
    tenant = tenant_for(db, account_id)
    revision = int(state.get("revision") or 0)
    if tenant is not None and revision and revision < int((tenant.platform_state or {}).get("revision") or 0):
        return tenant
    if tenant is None:
        tenant = Tenant(name=account.get("name") or "Account", slug=_slug(db, account.get("name") or "account"),
                        platform_account_id=account_id, status=status)
        db.add(tenant)
        db.flush()
        log.info("Platform account %s became tenant %s", account_id, tenant.slug)
    tenant.name = account.get("name") or tenant.name
    if account.get("timezone"):
        tenant.timezone = account["timezone"]
    tenant.status = status
    # Packages are the core's: no BioBridge plan, no BioBridge renewal sweep.
    tenant.plan_id = None
    tenant.pending_plan_id = None
    tenant.subscription_renews_at = None

    entitlements = state.get("entitlements") or {}
    for key, column in LIMITS.items():
        if key in entitlements:
            value = entitlements[key]
            # Null in the core means unlimited; on a tenant override that is 0.
            setattr(tenant, column, 0 if value in (None, 0) else int(value))
        else:
            setattr(tenant, column, None)
    floor = tenant.limit_min_sync_interval_minutes
    if floor and tenant.sync_interval_minutes < floor:
        tenant.sync_interval_minutes = floor

    tenant.platform_state = {
        "package": state.get("package"),
        "entitlements": entitlements,
        "trial_ends_at": state.get("trial_ends_at"),
        "renews_at": state.get("renews_at"),
        "revision": revision,
    }
    if "users" in state:
        _apply_users(db, tenant, state.get("users") or [])
    if "odoo" in state:
        apply_odoo(db, tenant, state.get("odoo"))
    db.flush()
    return tenant


# =============================================================================
# The account's Odoo, owned by the core
# =============================================================================
def _same_odoo(conn, url: str, db_name: str) -> bool:
    def norm(u: str) -> str:
        u = (u or "").strip().lower().split("://", 1)[-1].rstrip("/")
        return u.removeprefix("www.")
    return norm(conn.url) == norm(url) and (conn.db_name or "").strip().lower() == (db_name or "").strip().lower()


def apply_odoo(db: Session, tenant: Tenant, block: dict[str, Any] | None) -> None:
    """Make the tenant's Odoo connection match the one OdooBridger sent.

    The connection BioBridge already had for the same Odoo database is taken
    over (so its mappings, company switches and device records carry on);
    otherwise a new one is made. Any other active connection is switched
    off: a tenant syncs into one Odoo. ``None`` means the account has no Odoo
    connection any more — the managed one stops being used, nothing is deleted.
    """
    from app.core.crypto import encrypt
    from app.integrations.odoo import forget_detected_api
    from app.models import ConnectionStatus, OdooConnection

    rows = list(db.scalars(select(OdooConnection).where(OdooConnection.tenant_id == tenant.id)))
    if not block:
        for conn in rows:
            if conn.managed and conn.is_active:
                conn.is_active = False
                conn.status = ConnectionStatus.failed.value
                conn.status_message = "Odoo was disconnected in OdooBridger."
        return
    for key in ("id", "url", "db", "login", "api_key"):
        if not block.get(key):
            raise PlatformError(f"The Odoo connection has no {key}.")
    conn = next((c for c in rows if c.platform_connection_id == block["id"]), None) \
        or next((c for c in rows if _same_odoo(c, block["url"], block["db"])), None)
    if conn is None:
        name, n = "OdooBridger", 1
        while any(c.name == name for c in rows):
            n += 1
            name = f"OdooBridger {n}"
        conn = OdooConnection(tenant_id=tenant.id, name=name, url=block["url"], db_name=block["db"],
                              username=block["login"])
        db.add(conn)
        log.info("Tenant %s: Odoo connection from OdooBridger (%s)", tenant.slug, block["url"])
    from app.core.crypto import decrypt

    changed = (conn.url, conn.db_name, conn.username) != (block["url"], block["db"], block["login"]) \
        or decrypt(conn.api_key_enc, tenant.crypto_key) != block["api_key"]
    if changed:
        forget_detected_api(conn.url)
        conn.url, conn.db_name, conn.username = block["url"], block["db"], block["login"]
        conn.api_key_enc = encrypt(block["api_key"], tenant.crypto_key)
        conn.uid_cache = None
        conn.status = ConnectionStatus.unverified.value
        conn.status_message = None
    conn.platform_connection_id = block["id"]
    if block.get("version"):
        conn.odoo_version = str(block["version"])[:32]
    conn.is_active = True
    for other in rows:
        if other is not conn and other.is_active:
            other.is_active = False
            other.status_message = "Replaced by the Odoo connection set in OdooBridger."
    db.flush()
    from app.services.odoo_switch import ensure_current

    ensure_current(db, tenant)   # another Odoo database: let go of the old one's employees


def odoo_readiness(db: Session, tenant: Tenant, *, live: bool = False) -> dict[str, Any] | None:
    """BioBridge's own check on the Odoo OdooBridger gave it: can it sign in and
    write attendance. ``live`` runs the check now; otherwise the last result."""
    from datetime import datetime, timezone

    from app.integrations.odoo import OdooAuthError, OdooError
    from app.models import ConnectionStatus, OdooConnection
    from app.services.connections import UnsafeTargetError, build_odoo_client
    from app.services.friendly_errors import friendly_odoo_message

    conn = db.scalar(select(OdooConnection).where(OdooConnection.tenant_id == tenant.id,
                                                  OdooConnection.platform_connection_id.is_not(None),
                                                  OdooConnection.is_active.is_(True)))
    if conn is None:
        return None
    if live:
        now = datetime.now(timezone.utc)
        try:
            info = build_odoo_client(tenant, conn).ping()
        except (OdooError, UnsafeTargetError) as exc:
            message = friendly_odoo_message(exc)
            conn.status = ConnectionStatus.failed.value
            conn.status_message = message
            conn.last_checked_at = now
            db.flush()
            auth = isinstance(exc, OdooAuthError)
            return {"ready": False, "message": message, "auth_failed": auth,
                    "checks": [{"name": "Sign in" if auth else "Odoo check", "ok": False}],
                    "checked_at": now.isoformat()}
        conn.uid_cache = info["uid"]
        conn.odoo_version = str(info.get("server_version") or conn.odoo_version or "")
        conn.has_companion_addon = bool(info.get("has_companion_addon"))
        conn.has_device_tracking = bool(info.get("has_device_tracking"))
        conn.device_tracking_mode = info.get("device_tracking_mode")
        can_write = bool(info.get("can_create_attendance"))
        conn.status = ConnectionStatus.connected.value if can_write else ConnectionStatus.degraded.value
        conn.status_message = None if can_write else (
            "This Odoo user cannot create attendance records. Add them to Employees / Administrator.")
        conn.last_checked_at = now
        db.flush()
    checks = [
        {"name": "Sign in", "ok": conn.status in (ConnectionStatus.connected.value, ConnectionStatus.degraded.value)},
        {"name": "Create attendance", "ok": conn.status == ConnectionStatus.connected.value},
        {"name": "Device tracking", "ok": bool(conn.has_device_tracking), "optional": True},
    ]
    if conn.status == ConnectionStatus.unverified.value:
        return {"ready": None, "message": "Not checked yet.", "auth_failed": False, "checks": [],
                "checked_at": None}
    ready = conn.status == ConnectionStatus.connected.value
    message = "Ready." if ready else (conn.status_message or "Not ready.")
    auth_failed = conn.status == ConnectionStatus.failed.value and _looks_like_auth(conn.status_message)
    return {"ready": ready, "message": message, "auth_failed": auth_failed, "checks": checks,
            "checked_at": conn.last_checked_at.isoformat() if conn.last_checked_at else None}


def _looks_like_auth(message: str | None) -> bool:
    """The friendly messages BioBridge records when Odoo refuses to sign in
    (see friendly_errors.friendly_odoo_message)."""
    low = (message or "").lower()
    return low.startswith("login failed") or "check the database name" in low


def _apply_users(db: Session, tenant: Tenant, people: list[dict[str, Any]]) -> None:
    present = set()
    for person in people:
        if person.get("id"):
            upsert_user(db, tenant, person)
            present.add(person["id"])
    # Removed or disabled in the core: no longer signs in here.
    for user in db.scalars(select(User).where(User.tenant_id == tenant.id,
                                              User.platform_user_id.is_not(None))).all():
        if user.platform_user_id not in present and user.is_active:
            user.is_active = False
            for session in db.scalars(select(UserSession).where(
                    UserSession.user_id == user.id, UserSession.revoked_at.is_(None))):
                session.revoked_at = func.now()
                session.revoked_reason = "removed in OdooBridger"


def upsert_user(db: Session, tenant: Tenant, person: dict[str, Any]) -> User:
    email = (person.get("email") or "").strip().lower()
    user = db.scalar(select(User).where(User.platform_user_id == person["id"]))
    if user is None:
        user = db.scalar(select(User).where(func.lower(User.email) == email))
        if user is not None and user.tenant_id != tenant.id:
            raise PlatformError(
                f"{email} already signs in to another BioBridge account, so it can't join this one.")
        if user is None:
            user = User(tenant_id=tenant.id, email=email,
                        # Never usable: platform users sign in through the core.
                        hashed_password=hash_password(secrets.token_urlsafe(32)))
            db.add(user)
        user.platform_user_id = person["id"]
    if user.tenant_id != tenant.id:
        raise PlatformError("That user belongs to another account.")
    user.email = email or user.email
    user.full_name = person.get("full_name") or person.get("name") or user.full_name
    user.role = ROLE_MAP.get(person.get("role") or "", UserRole.viewer.value)
    user.is_active = True
    user.is_platform_admin = False
    user.credentials_pending = False
    user.must_change_password = False
    if user.email_verified_at is None:
        user.email_verified_at = func.now()
    db.flush()
    return user


# =============================================================================
# SSO: code -> identity -> tenant + user
# =============================================================================
def exchange_code(code: str) -> dict[str, Any]:
    if not settings.platform_client_secret:
        raise PlatformError("PLATFORM_CLIENT_SECRET is not set on this BioBridge.")
    try:
        response = httpx.post(f"{settings.platform_core_api}/api/v1/sso/token", json={
            "client_id": settings.platform_service_slug, "client_secret": settings.platform_client_secret,
            "code": code}, timeout=settings.http_timeout_seconds)
    except httpx.HTTPError as exc:
        raise PlatformError(f"Could not reach OdooBridger: {exc}") from exc
    if response.status_code != 200:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:200]
        raise PlatformError(detail or f"OdooBridger answered {response.status_code}.")
    return verify(response.json()["id_token"], "identity")


def sign_in(db: Session, claims: dict[str, Any]) -> User:
    """The tenant and user an identity token describes, created or updated."""
    state = {
        "account": claims.get("account") or {},
        "status": claims.get("status"),
        "package": claims.get("package"),
        "entitlements": claims.get("entitlements") or {},
        "trial_ends_at": claims.get("trial_ends_at"),
        "renews_at": claims.get("renews_at"),
        "revision": claims.get("revision") or 0,
    }
    tenant = apply_state(db, state)
    user = upsert_user(db, tenant, {"id": claims["sub"], "email": claims.get("email"),
                                    "full_name": claims.get("name"), "role": claims.get("role")})
    return user


def usage(db: Session, tenant: Tenant, *, check_odoo: bool = False) -> dict[str, Any]:
    from app.models import Device, EmployeeMapping, MappingStatus, SyncRun

    devices = db.scalar(select(func.count(Device.id)).where(Device.tenant_id == tenant.id)) or 0
    employees = db.scalar(select(func.count(EmployeeMapping.id)).where(
        EmployeeMapping.tenant_id == tenant.id, EmployeeMapping.status == MappingStatus.mapped.value)) or 0
    last = db.scalar(select(SyncRun).where(SyncRun.tenant_id == tenant.id)
                     .order_by(SyncRun.started_at.desc()).limit(1))
    return {
        "tenant": tenant.slug,
        "status": tenant.status,
        "devices": devices,
        "employees": employees,
        "sync_interval_minutes": tenant.sync_interval_minutes,
        "last_sync": {"status": last.status, "started_at": last.started_at.isoformat() if last.started_at else None}
        if last else None,
        "odoo": odoo_readiness(db, tenant, live=check_odoo),
    }

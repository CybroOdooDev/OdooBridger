"""The account's Odoo connection: saving, testing, binding services, readiness.

The core owns where the customer's Odoo is and the key to it. Services that
work on Odoo (``Service.uses_odoo``) are bound to one connection and are sent
it on provisioning; they call Odoo themselves with that copy, so a sync never
waits on the core. Each service reports back, through its usage endpoint,
whether it can do its own job on that Odoo (its add-on, its rights).

One Odoo database belongs to one account. Two accounts writing attendance
into the same database would trip over each other's records, so the same
URL + database (or the same ``database.uuid``) is refused for a second account.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from obcore.config import settings
from obcore.mailer import try_send
from obcore.models import (
    Account, AccountService, OdooConnection, OdooConnectionStatus, ServiceStatus, UserRole,
)
from obcore.security import aware, decrypt, encrypt, utcnow
from obcore.services import odoo_probe, provisioning

log = logging.getLogger(__name__)

KEY_PURPOSE = "odoo-api-key"
#: One Odoo database per account for now. The model allows more; the portal
#: and services are written for a list so this can be lifted later.
MAX_PER_ACCOUNT = 1
KEY_EXPIRY_WARNING = timedelta(days=14)


class OdooConnectionError(ValueError):
    """Safe to show the customer."""


def api_key(conn: OdooConnection) -> str:
    return decrypt(conn.api_key_enc, KEY_PURPOSE)


# --- uniqueness -----------------------------------------------------------------
def _taken_by_other(db: Session, account: Account, ukey: str, dkey: str, db_uuid: str | None,
                    exclude_id: str | None) -> bool:
    clauses = [(OdooConnection.url_key == ukey) & (OdooConnection.db_key == dkey)]
    if db_uuid:
        clauses.append(OdooConnection.db_uuid == db_uuid)
    query = select(OdooConnection).where(or_(*clauses))
    if exclude_id:
        query = query.where(OdooConnection.id != exclude_id)
    for other in db.scalars(query):
        if other.account_id != account.id:
            return True
        raise OdooConnectionError("This Odoo database is already connected to your account.")
    return False


def _check_free(db: Session, account: Account, ukey: str, dkey: str, db_uuid: str | None,
                exclude_id: str | None = None) -> None:
    if _taken_by_other(db, account, ukey, dkey, db_uuid, exclude_id):
        raise OdooConnectionError(
            "This Odoo database is already connected to another OdooBridger account. One Odoo database "
            "can belong to one account only. Contact us if you think this is wrong.")


# --- agents -----------------------------------------------------------------------
def _agent(account: Account, agent_id: str | None):
    """The account's live agent with this id (None for a direct connection)."""
    if not agent_id:
        return None
    agent = next((a for a in account.relay_agents if a.id == agent_id and a.revoked_at is None), None)
    if agent is None:
        raise OdooConnectionError("That OdooBridger Agent is not on your account.")
    return agent


def _keys(url: str, db_name: str, agent_id: str | None) -> tuple[str, str]:
    """Uniqueness keys. A private address is only meaningful inside the network
    of the agent that reaches it, so it is scoped to that agent; Odoo's own
    database.uuid still catches the same database reached two ways."""
    ukey = odoo_probe.url_key(url)
    return (f"agent:{agent_id}/{ukey}" if agent_id else ukey), odoo_probe.db_key(db_name)


def _probe(url: str, db_name: str, login: str, key: str, agent_id: str | None):
    try:
        return odoo_probe.probe(url, db_name, login, key, agent_id=agent_id)
    except odoo_probe.OdooProbeError as exc:
        raise OdooConnectionError(str(exc)) from exc


# --- binding ----------------------------------------------------------------------
def bind_services(account: Account) -> list[AccountService]:
    """Bind every Odoo service without a (still existing) connection to the account's first one."""
    first = account.odoo_connections[0] if account.odoo_connections else None
    changed = []
    for row in account.services:
        if not row.service.uses_odoo:
            continue
        current = row.odoo_connection if row.odoo_connection in account.odoo_connections else None
        target = current or first
        if row.odoo_connection is not target:
            row.odoo_connection = target
            row.odoo_readiness = None
            provisioning.mark_changed(row)
            changed.append(row)
    return changed


def bound_rows(conn: OdooConnection) -> list[AccountService]:
    return [r for r in conn.account.services if r.odoo_connection is conn]


def _changed(conn: OdooConnection) -> None:
    for row in bound_rows(conn):
        row.odoo_readiness = None
        provisioning.mark_changed(row)


# --- test ---------------------------------------------------------------------------
def _apply_probe(conn: OdooConnection, result: odoo_probe.ProbeResult) -> None:
    now = utcnow()
    conn.odoo_version = result.version
    conn.api = result.api
    conn.companies = result.companies
    if result.db_uuid:
        conn.db_uuid = result.db_uuid
    conn.status = OdooConnectionStatus.connected.value
    conn.status_message = None
    conn.last_checked_at = now
    conn.last_ok_at = now
    conn.failure_notified_at = None


def _fail(conn: OdooConnection, message: str) -> None:
    conn.status = OdooConnectionStatus.failed.value
    conn.status_message = message[:1000]
    conn.last_checked_at = utcnow()


def test(db: Session, conn: OdooConnection) -> bool:
    """Run the core's test on a saved connection. Records the outcome; never raises."""
    before = (conn.status, conn.odoo_version, conn.api, conn.companies)
    try:
        result = odoo_probe.probe(conn.url, conn.db_name, conn.login, api_key(conn), agent_id=conn.relay_agent_id)
        if result.db_uuid and result.db_uuid != conn.db_uuid:
            _check_free(db, conn.account, conn.url_key, conn.db_key, result.db_uuid, exclude_id=conn.id)
        _apply_probe(conn, result)
    except (odoo_probe.OdooProbeError, OdooConnectionError, ValueError) as exc:
        _fail(conn, str(exc))
    if (conn.status, conn.odoo_version, conn.api, conn.companies) != before:
        _changed(conn)
    db.flush()
    return conn.status == OdooConnectionStatus.connected.value


# --- save -----------------------------------------------------------------------------
def _expiry(value: str | datetime | None) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return aware(value)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise OdooConnectionError("The key expiry date is not a date.") from exc
    return aware(parsed)


def create(db: Session, account: Account, *, url: str, db_name: str, login: str, api_key_value: str,
           name: str | None = None, key_expires_at: str | None = None,
           agent_id: str | None = None) -> OdooConnection:
    """Test, then save. A connection that fails its test is not saved.

    ``agent_id``: reach this Odoo through the account's OdooBridger Agent
    (a private address, as seen from the agent's network)."""
    live = [c for c in account.odoo_connections]
    if len(live) >= MAX_PER_ACCOUNT:
        raise OdooConnectionError("Your account already has an Odoo connection. Change it instead.")
    agent = _agent(account, agent_id)
    try:
        url = odoo_probe.clean_url(url)
        db_name = (db_name or "").strip() or (odoo_probe.guess_db(url) or "")
    except odoo_probe.OdooProbeError as exc:
        raise OdooConnectionError(str(exc)) from exc
    if not db_name:
        raise OdooConnectionError("Enter the database name.")
    ukey, dkey = _keys(url, db_name, agent_id)
    _check_free(db, account, ukey, dkey, None)
    result = _probe(url, db_name, login, api_key_value, agent_id)
    _check_free(db, account, ukey, dkey, result.db_uuid)
    conn = OdooConnection(account_id=account.id, name=(name or "").strip() or "Odoo", url=url, db_name=db_name,
                          login=login.strip(), api_key_enc=encrypt(api_key_value.strip(), KEY_PURPOSE),
                          url_key=ukey, db_key=dkey, key_expires_at=_expiry(key_expires_at))
    conn.relay_agent = agent
    _apply_probe(conn, result)
    conn.account = account
    db.add(conn)
    db.flush()
    bind_services(account)
    db.flush()
    return conn


def update(db: Session, conn: OdooConnection, *, url: str | None = None, db_name: str | None = None,
           login: str | None = None, api_key_value: str | None = None, name: str | None = None,
           key_expires_at: Any = ..., agent_id: Any = ...) -> OdooConnection:
    """Change a connection. Anything that affects signing in is tested first;
    a change that fails its test is not saved (the old connection keeps working).
    ``agent_id``: ... keeps the route, None goes direct, an id goes through that agent."""
    try:
        new_url = odoo_probe.clean_url(url) if url else conn.url
    except odoo_probe.OdooProbeError as exc:
        raise OdooConnectionError(str(exc)) from exc
    new_db = (db_name or "").strip() or conn.db_name
    new_login = (login or "").strip() or conn.login
    new_key = (api_key_value or "").strip() or api_key(conn)
    new_agent_id = conn.relay_agent_id if agent_id is ... else (agent_id or None)
    agent = _agent(conn.account, new_agent_id)
    credentials_changed = (new_url, new_db, new_login, new_agent_id) != (
        conn.url, conn.db_name, conn.login, conn.relay_agent_id) or bool((api_key_value or "").strip())
    if credentials_changed:
        ukey, dkey = _keys(new_url, new_db, new_agent_id)
        _check_free(db, conn.account, ukey, dkey, None, exclude_id=conn.id)
        result = _probe(new_url, new_db, new_login, new_key, new_agent_id)
        if (ukey, dkey) != (conn.url_key, conn.db_key) and result.db_uuid != conn.db_uuid and conn.db_uuid:
            # Pointing the connection at another database: the services' data
            # (mappings, records written) belongs to the old one.
            conn.db_uuid = None
        _check_free(db, conn.account, ukey, dkey, result.db_uuid, exclude_id=conn.id)
        conn.url, conn.db_name, conn.login = new_url, new_db, new_login
        conn.url_key, conn.db_key = ukey, dkey
        conn.relay_agent = agent
        if (api_key_value or "").strip():
            conn.api_key_enc = encrypt(new_key, KEY_PURPOSE)
            conn.key_warned_at = None
        _apply_probe(conn, result)
    if name is not None and name.strip():
        conn.name = name.strip()[:120]
    if key_expires_at is not ...:
        conn.key_expires_at = _expiry(key_expires_at)
        conn.key_warned_at = None
    _changed(conn)
    db.flush()
    return conn


def delete(db: Session, conn: OdooConnection) -> None:
    account = conn.account
    for row in bound_rows(conn):
        row.odoo_connection = None
        row.odoo_readiness = None
        provisioning.mark_changed(row)
    account.odoo_connections.remove(conn)
    db.delete(conn)
    db.flush()


# --- what services are told ---------------------------------------------------------
def provision_block(row: AccountService) -> dict[str, Any] | None:
    """The ``odoo`` part of a service's provisioning payload (Odoo services only)."""
    conn = row.odoo_connection
    if conn is None:
        return None
    return {
        "id": conn.id, "name": conn.name, "url": conn.url, "db": conn.db_name, "login": conn.login,
        "api_key": api_key(conn), "version": conn.odoo_version, "api": conn.api,
        "companies": conn.companies or [], "status": conn.status,
        # Through the account's agent: call Odoo via the core's relay
        # (POST /api/v1/relay/forward), not directly.
        "relay": {"agent_id": conn.relay_agent_id} if conn.relay_agent_id else None,
    }


# --- readiness, reported by the services ---------------------------------------------
def record_readiness(db: Session, row: AccountService, report: dict[str, Any] | None) -> None:
    """Store a service's own check; an auth failure it saw marks the connection failed."""
    if not isinstance(report, dict):
        return
    row.odoo_readiness = {k: report.get(k) for k in ("ready", "message", "checks", "auth_failed", "checked_at")}
    row.odoo_readiness_at = utcnow()
    conn = row.odoo_connection
    if conn is not None and report.get("auth_failed") and conn.status != OdooConnectionStatus.failed.value:
        _fail(conn, report.get("message") or f"{row.service.name} could not sign in to Odoo.")
        _changed(conn)


def refresh_readiness(db: Session, conn: OdooConnection, *, live: bool = False) -> None:
    """Ask each bound, usable service for its readiness. Best effort."""
    for row in bound_rows(conn):
        if row.status not in (ServiceStatus.trialing.value, ServiceStatus.active.value):
            continue
        if row.provisioned_revision < row.revision:
            provisioning.push(db, row)
        try:
            usage = provisioning.usage(db, row, check_odoo=live)
        except provisioning.ProvisionError as exc:
            log.info("Readiness of %s for %s unavailable: %s", row.service.slug, row.account_id, exc)
            continue
        record_readiness(db, row, (usage or {}).get("odoo"))
    db.flush()


def notify_failures(db: Session, *, now: datetime | None = None) -> int:
    """Email the account's managers once when its Odoo stops working, and
    before a known key expiry date."""
    now = now or utcnow()
    sent = 0
    for conn in db.scalars(select(OdooConnection)):
        managers = [u for u in conn.account.users
                    if u.is_active and u.role in (UserRole.owner.value, UserRole.admin.value)]
        link = f"{settings.base}/app/#/odoo"
        if conn.status == OdooConnectionStatus.failed.value and conn.failure_notified_at is None:
            for user in managers:
                try_send(user.email, "Your Odoo connection stopped working",
                         f"Hi,\n\nOdooBridger can no longer sign in to your Odoo ({conn.url}).\n\n"
                         f"{conn.status_message or ''}\n\nFix it here: {link}\n\n— OdooBridger\n")
            conn.failure_notified_at = now
            sent += 1
        expires = aware(conn.key_expires_at)
        if expires and conn.key_warned_at is None and expires - now <= KEY_EXPIRY_WARNING:
            for user in managers:
                try_send(user.email, "Your Odoo API key expires soon",
                         f"Hi,\n\nThe Odoo API key OdooBridger uses expires on {expires:%d %b %Y}. "
                         f"Create a new key in Odoo and paste it here before then: {link}\n\n— OdooBridger\n")
            conn.key_warned_at = now
            sent += 1
    db.flush()
    return sent

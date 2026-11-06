"""What the dashboard's "Sync & connections" card shows about the connections.

Odoo: where attendance goes, whether it answered, and when that was last checked.
Terminals: one line each, and what BioBridge can honestly say about whether each is up:

* **A push terminal** (ZKTeco ADMS) calls in every minute or so, so silence is real
  evidence: *online* if it called in within ``TERMINAL_SILENT_MINUTES``, *offline* if
  not, *waiting* if it has never called in.
* **A terminal read through a platform** (BioTime, BioStar…) is never asked directly.
  When the platform's device list says when it last heard from the terminal (BioTime's
  ``last_activity``) that heartbeat is judged exactly like a push terminal's. When it
  says nothing, BioBridge only knows the platform lists it (*reporting*) or has stopped
  listing it (*missing*), and a quiet night is not a fault.
* **A standalone device** (one terminal, read directly) is asked every sync, so the
  answer to that ask is its health: *online* when the last check reached it, *unreachable*
  when it did not, *unchecked* before the first check. A standalone connection always
  appears, even before its terminal record exists (that record is made when a test first
  reaches it, or its first punch arrives).
* Either way the last *punch* is shown as activity, never as health.

Computed on demand from state already kept; nothing is stored. The same rules the
alert bell uses decide "offline", so the card and the bell never disagree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AdmsDevice, Device, DeviceSource, OdooConnection, PunchRecord, Tenant
from app.services.alerts import TERMINAL_SILENT_MINUTES
from app.services.dashboard_activity import _utc_midnights
from app.services.timeutils import ensure_aware

#: The card lists this many terminals; the rest are counted, with a link to the full page.
MAX_TERMINALS = 12

#: Worst first, so a problem is never below the fold.
_ORDER = {"unreachable": 0, "offline": 0, "missing": 1, "waiting": 2, "unchecked": 2,
          "online": 3, "reporting": 3}

_ADMS = "zkteco_adms"


def active_odoo_connection(db: Session, tenant: Tenant) -> OdooConnection | None:
    """The connection in use: the active one, and OdooBridger's before a local one.
    Falls back to any, so a switched-off connection still shows rather than "missing"."""
    return db.scalars(
        select(OdooConnection)
        .where(OdooConnection.tenant_id == tenant.id)
        .order_by(OdooConnection.is_active.desc(), OdooConnection.platform_connection_id.is_(None))
        .limit(1)
    ).first()


def _host(url: str | None) -> str:
    parsed = urlparse(url or "")
    return (parsed.netloc or parsed.path or "").rstrip("/")


def _standalone_state(source: DeviceSource) -> tuple[str, str, datetime | None]:
    """A standalone device is read directly every sync: that check is its heartbeat."""
    checked = ensure_aware(source.last_checked_at)
    if source.status == "connected":
        return "device", "online", checked
    if source.status == "unverified" or checked is None:
        return "device", "unchecked", None
    return "device", "unreachable", None


def _terminal_state(device: Device, push: AdmsDevice | None, now: datetime,
                    source: DeviceSource | None = None) -> tuple[str, str, datetime | None]:
    """(kind, state, last sign of life): the heartbeat, never the last punch, when there is one."""
    silent_after = timedelta(minutes=TERMINAL_SILENT_MINUTES)
    if push is not None:
        seen = ensure_aware(push.last_seen_at)
        if seen is None:
            return "push", "waiting", None
        return "push", "offline" if now - seen > silent_after else "online", seen
    if device.missing_since is not None:
        return "platform", "missing", ensure_aware(device.missing_since)
    if source is not None and source.connection_kind == "device":
        return _standalone_state(source)
    contact = ensure_aware(device.last_contact_at)
    if contact is not None:
        # The platform told us when it last heard from the terminal: a real answer.
        return "platform", "offline" if now - contact > silent_after else "online", contact
    # The platform doesn't say: it only lists the terminal. Not judged, never "offline".
    return "platform", "reporting", None


def connection_overview(db: Session, tenant: Tenant, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)

    conn = active_odoo_connection(db, tenant)
    odoo = None
    if conn is not None:
        odoo = {
            "host": _host(conn.url),
            "status": conn.status,
            "message": conn.status_message,
            "last_checked_at": ensure_aware(conn.last_checked_at),
            "managed": bool(conn.managed),
            "active": bool(conn.is_active),
        }

    pushers = {
        d.serial_number: d
        for d in db.scalars(select(AdmsDevice).where(AdmsDevice.tenant_id == tenant.id)).all()
    }
    devices = db.scalars(
        select(Device).where(Device.tenant_id == tenant.id, Device.is_enabled.is_(True))
    ).all()
    # Punches each terminal took today (the account's day): activity, shown beside its heartbeat.
    today_start = _utc_midnights(tenant, now, 1)[0][1]
    per_device = dict(db.execute(
        select(PunchRecord.device_id, func.count(PunchRecord.id))
        .where(PunchRecord.tenant_id == tenant.id, PunchRecord.punch_time_utc >= today_start,
               PunchRecord.device_id.is_not(None))
        .group_by(PunchRecord.device_id)
    ).all())
    sources = {
        src.id: src for src in db.scalars(
            select(DeviceSource).where(DeviceSource.tenant_id == tenant.id,
                                       DeviceSource.is_active.is_(True))).all()
    }
    rows = []
    for device in devices:
        source = sources.get(device.source_id)
        if source is None:  # its connection is switched off: not part of the live picture
            continue
        kind, state, seen = _terminal_state(device, pushers.get(device.serial_number), now, source)
        rows.append({
            "id": device.id,
            "registered": True,
            "name": device.alias or device.serial_number,
            "kind": kind,
            "state": state,
            "last_contact_at": seen,
            # Activity, kept apart from connectivity: a quiet door is not an offline one.
            "last_punch_at": ensure_aware(device.last_seen_at),
            "punches_today": per_device.get(device.id, 0),
        })
    # A standalone connection whose terminal record does not exist yet still shows, judged
    # by its own last check, so a working device is never missing from the list.
    registered_sources = {d.source_id for d in devices}
    for source in sources.values():
        if (source.connection_kind != "device" or source.provider == _ADMS
                or source.id in registered_sources):
            continue
        kind, state, seen = _standalone_state(source)
        rows.append({
            "id": source.id, "registered": False, "name": source.name, "kind": kind,
            "state": state, "last_contact_at": seen, "last_punch_at": None, "punches_today": 0,
        })
    rows.sort(key=lambda r: (_ORDER[r["state"]], r["name"].lower()))

    counts = {"total": len(rows)}
    for name in ("online", "reporting", "offline", "unreachable", "unchecked", "missing", "waiting"):
        counts[name] = sum(1 for r in rows if r["state"] == name)

    last_punch = db.scalar(
        select(func.max(PunchRecord.punch_time_utc)).where(PunchRecord.tenant_id == tenant.id))

    return {
        "odoo": odoo,
        "terminals": rows[:MAX_TERMINALS],
        "terminal_counts": counts,
        "last_punch_at": last_punch,
    }

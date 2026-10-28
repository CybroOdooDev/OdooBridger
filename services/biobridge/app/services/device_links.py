"""Which terminal a punch came from, and linking Odoo attendance to it.

A punch is linked to its terminal (``PunchRecord.device_id``) when it is
fetched — but only if that terminal was already imported into BioBridge at
the time. A terminal imported afterwards left its earlier punches unlinked
for good, so the attendance records they became went to Odoo with no device,
even though every punch carries the terminal's serial number
(``terminal_sn``). ``device_for_punch`` falls back to that serial.

``link_attendance_devices`` fixes records already in Odoo: for every
``hr.attendance`` BioBridge created, it finds the terminal from the
record's earliest traceable punch (the check-in, normally) and writes it
onto the Odoo record over Odoo's API — only where Odoo's record has no device
yet, so a device someone set by hand in Odoo is never overwritten.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.integrations.odoo import OdooClient, OdooError
from app.models import Device, DeviceSource, EmployeeMapping, MappingStatus, PunchRecord, Tenant
from app.services.company_scope import terminal_company_id


def device_for_punch(db: Session, punch: PunchRecord) -> Device | None:
    """The terminal a punch came from, or None if it can't be told.

    Uses the link made at fetch time when there is one; otherwise looks the
    terminal up by this punch's source and serial number (terminal identity
    is per source — see Device's unique constraint) and saves the link so
    the lookup happens once.
    """
    if punch.device_id:
        return db.get(Device, punch.device_id)
    if not punch.terminal_sn:
        return None
    device = db.scalar(
        select(Device).where(
            Device.source_id == punch.source_id,
            Device.serial_number == punch.terminal_sn,
        )
    )
    if device is not None:
        punch.device_id = device.id
    return device


def terminal_may_register_in_odoo(db: Session, device: Device) -> bool:
    """Whether this terminal may be written to Odoo as a device record.

    A terminal that has punches but whose employees are all unmapped is held
    back: nothing says whose terminal it is, and Odoo should not carry a device
    for people BioBridge cannot place. It registers as soon as one of its
    employees is mapped, automatically or by hand. A terminal with no punches
    yet (just discovered, or just connected) is not penalised.
    """
    base = select(PunchRecord.id).where(
        PunchRecord.tenant_id == device.tenant_id,
        PunchRecord.source_id == device.source_id,
        PunchRecord.terminal_sn == device.serial_number,
    )
    if db.scalar(base.limit(1)) is None:
        return True
    mapped = (
        base.join(
            EmployeeMapping,
            (EmployeeMapping.tenant_id == PunchRecord.tenant_id)
            & (EmployeeMapping.emp_code == PunchRecord.emp_code),
        )
        .where(EmployeeMapping.status == MappingStatus.mapped.value)
        .limit(1)
    )
    return db.scalar(mapped) is not None


@dataclass
class LinkReport:
    #: Odoo attendance records BioBridge created (from the punch ledger).
    attendances: int = 0
    #: ...of which none of the punches can be traced to a terminal.
    no_terminal: int = 0
    #: ...that already have a device in Odoo, or no longer exist there.
    already_linked: int = 0
    #: serial number -> attendance ids still needing a device.
    missing: dict[str, list[int]] = field(default_factory=dict)
    linked: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def to_link(self) -> int:
        return sum(len(ids) for ids in self.missing.values())


def link_attendance_devices(
    db: Session, tenant: Tenant, odoo: OdooClient, apply: bool = False
) -> LinkReport:
    """Report — and with ``apply``, fix — BioBridge-created hr.attendance
    records that have no device in Odoo. The caller commits (or rolls back,
    for a report-only run) the punch links ``device_for_punch`` fills in."""
    report = LinkReport()

    punches = db.scalars(
        select(PunchRecord)
        .where(
            PunchRecord.tenant_id == tenant.id,
            PunchRecord.odoo_attendance_id.is_not(None),
        )
        .order_by(PunchRecord.punch_time_utc)
    ).all()

    all_ids: set[int] = set()
    device_of: dict[int, Device] = {}
    for punch in punches:
        attendance_id = punch.odoo_attendance_id
        all_ids.add(attendance_id)
        if attendance_id in device_of:
            continue  # earliest traceable punch wins — the check-in, normally
        device = device_for_punch(db, punch)
        if device is not None:
            device_of[attendance_id] = device

    report.attendances = len(all_ids)
    report.no_terminal = len(all_ids) - len(device_of)
    if not device_of:
        return report

    without = set(odoo.attendance_ids_without_device(sorted(device_of)))
    report.already_linked = len(device_of) - len(without)

    by_device: dict[str, list[int]] = defaultdict(list)
    devices: dict[str, Device] = {}
    for attendance_id in sorted(without):
        device = device_of[attendance_id]
        by_device[device.id].append(attendance_id)
        devices[device.id] = device
    report.missing = {devices[d].serial_number: ids for d, ids in by_device.items()}

    if not apply:
        return report

    for device_id, ids in by_device.items():
        device = devices[device_id]
        if not terminal_may_register_in_odoo(db, device):
            continue
        try:
            odoo_device_id = odoo.upsert_device(
                device.serial_number,
                name=device.alias,
                location=device.area,
                terminal_model=device.model,
                ip_address=device.ip_address,
                company_id=terminal_company_id(db, device),
            )
            odoo.set_attendance_device(ids, odoo_device_id)
        except OdooError as exc:
            # One terminal Odoo won't take must not stop the others.
            report.failures.append(f"{device.serial_number}: {exc}")
            continue
        report.linked += len(ids)
    return report


# Punches that never became (and will not become) attendance: they do not
# make a deleted terminal worth bringing back.
_NOT_HISTORY = ("unmapped", "skipped", "deleted")


def sync_inventory(db: Session, tenant: Tenant, source: DeviceSource, terminals) -> dict:
    """Make the source's terminals match the inventory it publishes (BioTime's
    device list, for one). Used by "Import Terminals" and at every sync.

    * a terminal in the inventory is added, or its name/area/IP/model refreshed;
    * a terminal BioBridge has that the inventory no longer lists:
      - with real history (punches that reached or are on their way to Odoo)
        it is kept and flagged missing, so that history keeps its device;
      - otherwise it is removed. That is the usual case for terminals that
        only ever appeared because the server still returns old transactions
        of devices deleted from it.

    Returns {"added", "missing", "removed", "touched": [Device], "serials": set}.
    """
    from datetime import datetime

    existing = {d.serial_number: d for d in db.scalars(select(Device).where(Device.source_id == source.id))}
    added, touched, serials = 0, [], set()
    for terminal in terminals:
        serial = (terminal.serial_number or "").strip()
        if not serial:
            continue
        serials.add(serial)
        device = existing.get(serial)
        if device is None:
            device = Device(tenant_id=tenant.id, source_id=source.id, serial_number=serial)
            db.add(device)
            existing[serial] = device
            added += 1
        device.alias = terminal.alias or device.alias
        device.area = terminal.area or device.area
        device.ip_address = terminal.ip_address or device.ip_address
        device.model = terminal.model or device.model
        device.missing_since = None
        touched.append(device)
    # An empty list is not trusted to mean "every terminal was deleted": an
    # account without permission on the device list returns nothing too.
    gone = [d for s, d in existing.items() if s not in serials] if serials else []
    with_history = set()
    if gone:
        with_history = set(db.scalars(
            select(PunchRecord.device_id).where(
                PunchRecord.device_id.in_([d.id for d in gone]),
                PunchRecord.process_state.not_in(_NOT_HISTORY),
            ).distinct()
        ))
    now = datetime.now(timezone.utc)
    missing = removed = 0
    for device in gone:
        if device.id in with_history:
            if device.missing_since is None:
                device.missing_since = now
                missing += 1
            continue
        db.execute(update(PunchRecord).where(PunchRecord.device_id == device.id).values(device_id=None))
        # It may have been pushed to Odoo's device list: take it off there too.
        from app.services import device_removals
        device_removals.queue_device(db, tenant.id, device, source.name)
        db.delete(device)
        removed += 1
    db.flush()
    return {"added": added, "missing": missing, "removed": removed, "touched": touched, "serials": serials}


def restore_terminals_from_punches(db: Session, tenant: Tenant, source: DeviceSource,
                                   only: set[str] | None = None) -> list[Device]:
    """Bring back any terminal that has punches on record but no row.

    A terminal deleted from the Biometric connections page whose punches are
    still in the ledger must not stay gone: those punches are real history of
    that device. It is re-created from the ledger — serial, punch count and
    last-seen time as the punches say — and the punches are linked to it again.
    Runs at the start of every fetch for the source, so the terminal is back
    after the next sync whether or not it has sent anything new.

    Only punches that mean something count: synced, pending, error or held
    ones. A terminal whose punches were all left unmatched, skipped (a badge
    deleted from the unmatched list) or deleted by a user stays gone — nothing
    of it reached Odoo, and restoring it would undo the user's own cleanup.
    """
    known = set(db.scalars(select(Device.serial_number).where(Device.source_id == source.id)).all())
    rows = db.execute(
        select(PunchRecord.terminal_sn, func.count(PunchRecord.id), func.max(PunchRecord.punch_time_utc))
        .where(
            PunchRecord.tenant_id == tenant.id,
            PunchRecord.source_id == source.id,
            PunchRecord.terminal_sn.is_not(None),
            PunchRecord.terminal_sn != "",
            PunchRecord.process_state.not_in(_NOT_HISTORY),
        )
        .group_by(PunchRecord.terminal_sn)
    ).all()
    restored: list[Device] = []
    for serial, count, newest in rows:
        if serial in known or (only is not None and serial not in only):
            continue   # a source with an inventory only gets back terminals it still lists
        device = Device(
            tenant_id=tenant.id, source_id=source.id, serial_number=serial,
            punch_count=count,
            last_seen_at=newest.replace(tzinfo=newest.tzinfo or timezone.utc) if newest else None,
        )
        db.add(device)
        db.flush()
        db.execute(
            update(PunchRecord)
            .where(PunchRecord.tenant_id == tenant.id, PunchRecord.source_id == source.id,
                   PunchRecord.terminal_sn == serial, PunchRecord.device_id.is_(None))
            .values(device_id=device.id)
        )
        restored.append(device)
    if restored:
        db.commit()
    return restored

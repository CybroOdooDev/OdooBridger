"""The sync engine: device punches -> Odoo hr.attendance.

    1 fetch      pull punches from each source since its cursor, minus an overlap
    2 ingest     upsert into the ledger — replay-safe by construction
    3 normalise  local wall-clock -> naive UTC
    4 register   record every badge seen, before Odoo is involved at all
      provision  (opt-in, per source) push Odoo's active roster at the
                 provider, creating any employee it's missing — runs after
                 register, ahead of map; see _provision_employees
    5 map        emp_code -> hr.employee, cached on the mapping row
    6 pair       punch stream -> intervals, carrying the open shift forward
    7 push       create and close hr.attendance within Odoo's constraints
    8 record     ledger state, local mirror, run counters

Two invariants make a run safe to interrupt:

* The ledger is keyed on the vendor's own id scoped to its source, so ingesting
  the same window twice is a no-op.
* The cursor advances only *after* punches are stored, and only to the newest
  punch actually seen. A crash costs a retry, never a gap.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Collection
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import encrypt
from app.integrations.base import (
    Capability, EmployeeRecord, IdFilterUnsupported, ProviderError, UnsupportedCapability,
)
from app.integrations.odoo import OdooAuthError, OdooClient, OdooError, parse_dt
from app.models import (
    AttendanceRecord,
    ConnectionStatus,
    Device,
    DeviceSource,
    Direction,
    EmployeeMapping,
    MappingStatus,
    OdooConnection,
    PunchRecord,
    PunchState,
    SyncRun,
    SyncStatus,
    Tenant,
)
from app.services import device_removals
from app.services.device_links import terminal_may_register_in_odoo
from app.services.company_scope import (
    MULTI_COMPANY_PROVISION_MESSAGE,
    enabled_company_ids,
    infer_device_companies,
    is_multi_company,
)
from app.services.device_links import device_for_punch, restore_terminals_from_punches, sync_inventory
from app.services.connections import (
    UnsafeTargetError,
    build_odoo_client,
    build_source_provider,
)
from app.services.device_limits import over_limit_device_ids
from app.services.pairing import (
    Direction as PairDirection,
    OpenShift,
    PairingConfig,
    PairingMode,
    Punch,
    close_stale_at,
    day_of,
    pair_punches,
)
from app.services.name_match import (
    clear_suggestion,
    compatible as names_compatible,
    dismissed,
    hold_dismissed_badge,
    name_candidates,
    suggest_badge_only,
    suggest_name_only,
    usable_name,
)
from app.services.run_lock import TenantRunLock
from app.services.timeutils import local_to_utc, utc_to_local, utcnow_naive

log = logging.getLogger(__name__)

#: How many fetched punches to hold before asking the ledger about them. Large
#: enough that the round trips are few, small enough that the IN list stays an
#: index seek and the buffer stays negligible.
INGEST_BATCH = 500

#: DeviceSource.config keys of the record-id cursor (see _record_cursor).
RECORD_CURSOR_KEY = "record_cursor"
RECORD_CURSOR_OFF = "record_cursor_unsupported"

#: Reading a window whose read was cut short by a page cap (see _read_by_time):
#: at most this many reads per source per run, slices never under MIN_SLICE.
MAX_SLICE_READS = 12
MIN_SLICE = timedelta(minutes=10)

#: Bound parameters per lookup. SQLite's older default ceiling is 999.
ID_LOOKUP_CHUNK = 500


class SyncAborted(Exception):
    """A configuration problem that makes a run impossible — not an outage.

    Kept distinct because it must not count towards the failure streak: a tenant
    who has not finished onboarding should not be slow-laned or badged degraded.
    """


class AllSourcesUnreachable(SyncAborted):
    """Every configured platform failed. An outage, and it must be counted.

    A subclass because the *run* ends the same way — nothing to do, stop early —
    but the *cause* is the opposite of a configuration problem. Without this it
    inherited SyncAborted's exemption from the failure streak, so a customer
    whose BioTime server was switched off for good would never be badged
    degraded and BioBridge would poll a dead address on the fast lane forever.
    """


#: A run still marked running after this long died with its process.
RUN_STALE_AFTER = timedelta(hours=1)


def close_abandoned_runs(db: Session, tenant_id: str, keep_id: str | None = None) -> int:
    """Mark the tenant's "running" runs failed. Only call while holding its
    TenantRunLock: then no cycle is alive, so any such run was left behind by
    a process that stopped (deploy, crash) mid-cycle."""
    query = update(SyncRun).where(SyncRun.tenant_id == tenant_id, SyncRun.status == SyncStatus.running.value)
    if keep_id is not None:
        query = query.where(SyncRun.id != keep_id)
    done = db.execute(query.values(
        status=SyncStatus.failed.value, finished_at=datetime.now(timezone.utc),
        error_message="Interrupted: the process running this sync stopped before it finished.",
    )).rowcount or 0
    return done


def latest_run(db: Session, tenant_id: str) -> SyncRun | None:
    return db.scalars(select(SyncRun).where(SyncRun.tenant_id == tenant_id)
                      .order_by(SyncRun.started_at.desc()).limit(1)).first()


def active_run(db: Session, tenant_id: str, exclude_id: str | None = None) -> SyncRun | None:
    """The tenant's cycle in progress, if any (one started within the last hour
    and not finished)."""
    query = select(SyncRun).where(
        SyncRun.tenant_id == tenant_id,
        SyncRun.status == SyncStatus.running.value,
        SyncRun.finished_at.is_(None),
        SyncRun.started_at >= datetime.now(timezone.utc) - RUN_STALE_AFTER,
    )
    if exclude_id is not None:
        query = query.where(SyncRun.id != exclude_id)
    return db.scalars(query.order_by(SyncRun.started_at.desc()).limit(1)).first()


class SyncEngine:
    """Runs one full cycle for one tenant."""

    def __init__(
        self,
        db: Session,
        tenant: Tenant,
        triggered_by: str = "schedule",
        only_source_id: str | None = None,
        run: SyncRun | None = None,
        lock: TenantRunLock | None = None,
    ) -> None:
        self.db = db
        self.tenant = tenant
        #: A "Sync now" pressed on one connection — a standalone device, say —
        #: fetches from that source alone. Everything after the fetch (badge
        #: matching, the push to Odoo) is the same as a full cycle. A scoped run
        #: that fails does not count toward the account's failure streak: one
        #: switched-off terminal must not slow-lane every other site.
        self.only_source_id = only_source_id
        self._failures_before = tenant.consecutive_failures
        # Held locally as well as on the row: the run commits mid-cycle, which
        # expires the ORM object, and a reloaded value can come back naive.
        self._started_at = datetime.now(timezone.utc)
        #: ``run``: a row already created for this cycle (see /sync/start).
        self.run = run or SyncRun(
            tenant_id=tenant.id,
            status=SyncStatus.running.value,
            triggered_by=triggered_by,
            started_at=self._started_at,
            log=[],
        )
        self._adopted = run is not None
        #: Held by whoever created ``run`` (see /sync/start); released here.
        self._lock = lock
        if run is not None and run.started_at is not None:
            started = run.started_at
            self._started_at = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
        self._log_lines: list[str] = []
        #: device.id -> the id it was upserted to in Odoo's optional
        #: biobridge.device model this cycle, or None once that lookup has
        #: already failed once — see _odoo_device_id.
        self._odoo_device_cache: dict[str, int | None] = {}
        #: source.id -> DeviceSource, for the terminal's default company.
        self._source_cache: dict[str, DeviceSource | None] = {}

    # -- logging -----------------------------------------------------------
    def _log(self, message: str, level: str = "info", *, ui: bool = True) -> None:
        """Write a log line. ``ui=False`` keeps it in the server log only:
        device records, company isolation and provisioning are housekeeping,
        and the run log customers read is about attendance."""
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if ui:
            self._log_lines.append(f"[{stamp}] {level.upper()}: {message}")
        getattr(log, level, log.info)("[tenant=%s] %s", self.tenant.slug, message)

    def pairing_config(self, mode: str | None = None) -> PairingConfig:
        return PairingConfig(
            mode=PairingMode(mode or self.tenant.pairing_mode),
            day_boundary_hour=self.tenant.day_boundary_hour,
            timezone=self.tenant.timezone or "UTC",
            min_punch_interval_seconds=self.tenant.min_punch_interval_seconds,
            max_shift_hours=self.tenant.max_shift_hours,
            orphan_out_policy=self.tenant.orphan_out_policy,
        )

    # -- entry point -------------------------------------------------------
    def run_cycle(self) -> SyncRun:
        """Run one cycle, unless one is already running for this account (the
        schedule and a "Sync Now", say, or two BioBridge processes): then the
        running one is returned and nothing else happens. Two at once would
        read the same punches and wait on each other's rows."""
        lock = self._lock or TenantRunLock(self.tenant.id)
        if not lock.held and not lock.acquire():
            log.info("[tenant=%s] A sync is already running; not starting another", self.tenant.slug)
            return self._the_running_one()
        try:
            if not TenantRunLock.supported() and not self._adopted:
                other = active_run(self.db, self.tenant.id)
                if other is not None:
                    return other
            if not self._adopted:
                # Started now that it may run (it may have waited for a database connection).
                self._started_at = datetime.now(timezone.utc)
                self.run.started_at = self._started_at
            if TenantRunLock.supported():
                closed = close_abandoned_runs(self.db, self.tenant.id, keep_id=self.run.id)
                if closed:
                    log.warning("[tenant=%s] Closed %d sync run(s) left running by a stopped process",
                                self.tenant.slug, closed)
            self.db.add(self.run)
            # Committed now, so the run shows as running everywhere.
            self.db.commit()
            return self._cycle()
        finally:
            lock.release()

    def _the_running_one(self) -> SyncRun:
        """The cycle holding the lock. Its run row may not be committed yet
        (it was claimed a moment ago), so wait a little for it."""
        for _ in range(20):
            other = active_run(self.db, self.tenant.id)
            if other is not None:
                return other
            # Ends the read transaction (and hands the connection back) while waiting.
            self.db.commit()
            time.sleep(0.25)
        return latest_run(self.db, self.tenant.id) or self.run

    def _recover(self) -> None:
        """A failed flush (another process stored the same row at the same
        moment, say) leaves the transaction unusable; start a clean one so the
        run can still be recorded."""
        if not self.db.is_active:
            self.db.rollback()

    def _cycle(self) -> SyncRun:
        odoo_conn: OdooConnection | None = None

        try:
            # The subscription gate, as a backstop.
            #
            # The scheduler filters on this and the console's own button checks
            # it, but the customer's "Sync now" did not — so a suspended
            # account could press it and the run went through. Putting the
            # check here as well means the gate holds for every caller,
            # including any route added later that forgets: there is exactly one
            # place a cycle can start.
            #
            # SyncAborted rather than a bare raise: it records a run saying why,
            # visible to the customer and to staff, and is exempt from the
            # failure streak — a stopped account is not a failing one and must
            # not be badged degraded or slow-laned for it.
            if not self.tenant.syncable:
                raise SyncAborted(
                    f"This account is {self.tenant.status} and does not sync. "
                    f"Nothing has been lost: the cursor stays where it is and "
                    f"the next run picks up from there."
                )

            odoo_conn = self._active_odoo_connection()
            from app.services.odoo_switch import ensure_current

            released = ensure_current(self.db, self.tenant)
            if released:
                self._log(f"The Odoo connection now points at another database: {released['reset']} badge(s) "
                          f"will be matched again there, {released['removed']} removed.", "warning")
            # A cycle runs for minutes (a first sync reads weeks of punches):
            # don't hold the tenant row locked for all of it.
            self.db.commit()
            sources = self._active_sources()
            if not sources:
                raise SyncAborted(
                    "That connection is switched off or no longer exists."
                    if self.only_source_id else "No attendance platform is connected."
                )
            if self.only_source_id:
                self._log(f"Syncing '{sources[0].name}' only")

            # Ingest first, and independently of Odoo. Device platforms prune old
            # transactions, so a punch not captured now may be gone by the next
            # run. Everything lands in the ledger as pending and is pushed once
            # Odoo is reachable: an Odoo outage costs a delay, never data.
            #
            # Each source is isolated. With several sites, letting one failure
            # propagate would skip every other site's punches — data loss
            # disguised as a failed run.
            reachable = 0
            for source in sources:
                try:
                    self._fetch_and_ingest(source)
                    reachable += 1
                except (ProviderError, UnsafeTargetError) as exc:
                    source.status = ConnectionStatus.failed.value
                    source.status_message = str(exc)[:500]
                    source.last_checked_at = datetime.now(timezone.utc)
                    self.run.error_count += 1
                    self._log(f"'{source.name}' unreachable: {str(exc)}", "error")

            if reachable == 0:
                # The run list shows this message and nothing else, so it has to
                # carry the reason — "none could be reached" tells a customer
                # only what they already suspected. The per-source handler above
                # has just written a specific, actionable message; reuse it
                # rather than inventing a vaguer one.
                reasons = [s.status_message for s in sources if s.status_message]
                if len(sources) == 1 and reasons:
                    detail = reasons[0]          # one site: its message *is* the answer
                elif reasons:
                    detail = (
                        f"None of the {len(sources)} connected platforms could be "
                        f"reached. First: {reasons[0]}"
                    )
                else:
                    detail = f"None of the {len(sources)} connected platforms could be reached."
                raise AllSourcesUnreachable(
                    f"{detail} Punches already in the ledger are unaffected."
                )

            # Knowing *which badges punched* needs no Odoo — it is in the punch
            # stream. Registering them now means the Employees page lists
            # everyone awaiting a match even while Odoo is down.
            self._register_badges()
            self.db.commit()

            if odoo_conn is None:
                raise SyncAborted(
                    "Punches captured, but no active Odoo connection is configured. "
                    "Connect Odoo and they will be pushed on the next run."
                )

            odoo = build_odoo_client(self.tenant, odoo_conn)
            try:
                odoo.authenticate()
            except OdooAuthError:
                if not odoo_conn.managed:
                    raise
                # OdooBridger may have sent a new key since this run loaded
                # the connection. Read it again and try once more.
                self.db.refresh(odoo_conn)
                odoo = build_odoo_client(self.tenant, odoo_conn)
                odoo.authenticate()
            if odoo_conn.uid_cache != odoo.uid:
                odoo_conn.uid_cache = odoo.uid

            # Independent of the punch stream on purpose — an Odoo employee
            # who has never punched anywhere yet is still eligible to be
            # provisioned onto a source, since the point is to get them onto
            # the device *before* their first day, not to react to a punch
            # that can never arrive from someone the provider has never
            # heard of. Runs before mapping so a person provisioned this
            # cycle can, at the earliest, match on the very next one (once
            # someone has actually enrolled their biometric at the terminal
            # and they've punched for real).
            self._provision_employees(odoo, sources)

            # Both ends are connected: bring in everyone the device already
            # knows who matches an Odoo employee, without waiting for a punch.
            self._import_matched_employees(odoo, sources)

            self._reconcile_company_scope(odoo)
            self._resolve_mappings(odoo)
            try:
                from app.services.roster_import import refresh_companies

                refresh_companies(self.db, self.tenant, odoo, force=True)
            except Exception:  # noqa: BLE001 — a display cache must never fail a sync
                log.warning("company refresh skipped", exc_info=True)
            # Before the push, so a terminal that can now be assigned releases
            # its held punches in this same cycle.
            if is_multi_company(odoo):
                for note in infer_device_companies(self.db, self.tenant):
                    self._log(note, "info", ui=False)
            self._sync_odoo_devices(odoo, odoo_conn)
            self._refresh_open_mirror(odoo)
            self._push(odoo, odoo_conn)
            # Shifts nobody badged out of: closed at the limit on every run,
            # not only by the hourly sweep, so they never outlive a sync.
            self.run.attendances_closed += self.close_stale_shifts(odoo)
            # Then bring in what Odoo has that this copy doesn't: records entered
            # there by hand, from a kiosk, or split or edited there.
            self._follow_odoo_attendance(odoo)

            self.tenant.consecutive_failures = 0
            odoo_conn.status = ConnectionStatus.connected.value
            odoo_conn.last_checked_at = datetime.now(timezone.utc)
            self.run.status = (
                SyncStatus.partial.value if self.run.error_count else SyncStatus.success.value
            )

        except AllSourcesUnreachable as exc:
            self._recover()
            # Ordered before SyncAborted: it is a subclass, so the broader
            # handler below would swallow it and skip the streak.
            self.tenant.consecutive_failures += 1
            self.run.status = SyncStatus.failed.value
            self.run.error_message = str(exc)
            self._log(str(exc), "error")
            self._mark_degraded_if_needed()
        except SyncAborted as exc:
            self._recover()
            self.run.status = SyncStatus.failed.value
            self.run.error_message = str(exc)
            self._log(str(exc), "warning")
        except (ProviderError, OdooError) as exc:
            self._recover()
            self.tenant.consecutive_failures += 1
            self.run.status = SyncStatus.failed.value
            self.run.error_message = str(exc)
            self._log(str(exc), "error")
            if isinstance(exc, OdooAuthError) and odoo_conn is not None:
                # Recorded on the connection so the dashboard (and, in platform
                # mode, OdooBridger through the usage report) see Odoo refusing us.
                from app.services.friendly_errors import friendly_odoo_message

                odoo_conn.status = ConnectionStatus.failed.value
                odoo_conn.status_message = friendly_odoo_message(exc)
                odoo_conn.last_checked_at = datetime.now(timezone.utc)
            self._mark_degraded_if_needed()
        except Exception as exc:  # noqa: BLE001 — a worker must never die silently
            self._recover()
            self.tenant.consecutive_failures += 1
            self.run.status = SyncStatus.failed.value
            self.run.error_message = f"Unexpected error: {exc}"
            self._log(f"Unexpected error: {exc}", "exception")
            self._mark_degraded_if_needed()
        finally:
            self._recover()
            if self.only_source_id and self.run.status == SyncStatus.failed.value:
                self.tenant.consecutive_failures = self._failures_before
            finished = datetime.now(timezone.utc)
            self.run.finished_at = finished
            self.run.duration_ms = int((finished - self._started_at).total_seconds() * 1000)
            self.run.log = self._log_lines[-500:]
            self.db.commit()

        return self.run

    # -- stages 1-3 --------------------------------------------------------
    def _fetch_and_ingest(self, source: DeviceSource) -> None:
        overlap = timedelta(minutes=settings.fetch_overlap_minutes)
        floor = utcnow_naive() - timedelta(days=settings.backfill_limit_days)

        cursor = source.cursor_punch_time or floor
        start_utc = max(cursor - overlap, floor)
        end_utc = utcnow_naive() + timedelta(minutes=5)  # tolerate device clock skew

        self.run.cursor_from = start_utc
        self.run.cursor_to = end_utc

        tz = source.server_timezone or self.tenant.timezone
        start_local = utc_to_local(start_utc, tz)
        end_local = utc_to_local(end_utc, tz)
        self._log(f"Fetching '{source.name}' {start_local} -> {end_local} ({tz})")

        provider = build_source_provider(self.tenant, source)
        pushes = getattr(provider, "pushes", False)

        # A source that publishes its device list (BioTime, BioStar...) is the
        # authority on which terminals exist: BioBridge follows it, so old
        # transactions of a terminal deleted there don't bring it back here.
        inventory = None
        if not pushes and getattr(provider, "server_inventory", False) \
                and provider.supports(Capability.LIST_TERMINALS):
            try:
                terminals = list(provider.fetch_terminals())
            except Exception as exc:  # noqa: BLE001 — the device list is a nicety; punches still sync
                self._log(f"Couldn't read the device list of '{source.name}': {exc}", ui=False)
            else:
                inventory = sync_inventory(self.db, self.tenant, source, terminals)
                if not inventory["serials"]:
                    inventory = None   # nothing listed: not trusted, see sync_inventory
                elif inventory["added"] or inventory["removed"] or inventory["missing"]:
                    self._log(f"Terminals of '{source.name}': {inventory['added']} new, "
                              f"{inventory['removed']} removed, {inventory['missing']} no longer listed", ui=False)
        listed = inventory["serials"] if inventory else None

        # A terminal deleted by hand but still holding punches comes back
        # (with an inventory, only one the source still lists).
        for restored in restore_terminals_from_punches(self.db, self.tenant, source, only=listed):
            self._log(f"Restored terminal {restored.serial_number} — it has {restored.punch_count} punch(es) on record", ui=False)
        # Committed before the long read, so "Import Terminals" or a second sync
        # running meanwhile isn't left waiting on these rows until it ends.
        self.db.commit()
        devices = {
            d.serial_number: d
            for d in self.db.scalars(
                select(Device).where(Device.source_id == source.id)
            ).all()
        }
        enabled = {sn for sn, d in devices.items() if d.is_enabled}
        self._listed = listed

        if pushes:
            try:
                self._claim_pushed(source, provider)
            finally:
                provider.close()
            return
        newest = source.cursor_punch_time
        fetched = created = 0
        by_id = self._record_cursor(source, provider)
        top_id: int | None = None          # highest record id stored this run
        time_pass_top: int | None = None   # ... by the time-window read alone
        time_pass_created = 0
        seen_external: set[str] = set()

        def consume(events) -> int:
            """Ingest one read's events; returns how many were new."""
            nonlocal fetched, newest, top_id
            made = 0
            # Buffered, so the "have we seen this already?" check can be asked
            # about the handful of punches in hand rather than the whole ledger.
            batch: list[tuple[object, datetime]] = []
            for event in events:
                if event.external_id in seen_external:
                    continue  # already read by the other pass of this run
                seen_external.add(event.external_id)
                fetched += 1
                if event.record_id is not None and (top_id is None or event.record_id > top_id):
                    top_id = event.record_id

                # A terminal the customer explicitly disabled is skipped, but an
                # unknown one is kept: a device that appeared since the last
                # import should not silently lose its punches.
                if devices and event.terminal_sn and event.terminal_sn not in enabled:
                    if event.terminal_sn in devices:
                        continue

                punch_utc = local_to_utc(event.punch_time_local, tz)
                if punch_utc < floor:
                    continue  # device clock is wrong, or an ancient backfill

                if newest is None or punch_utc > newest:
                    newest = punch_utc

                batch.append((event, punch_utc))
                if len(batch) >= INGEST_BATCH:
                    made += self._ingest_batch(batch, source, devices)
                    batch = []

            if batch:
                made += self._ingest_batch(batch, source, devices)
            return made

        try:
            # 1. By time: always, so nothing ever depends on the id cursor alone.
            time_pass_created, read_until = self._read_by_time(provider, consume, start_utc, end_utc, tz)
            time_pass_top = top_id
            created += time_pass_created
            if read_until < end_utc:
                # Only part of the window fit in this run's page budget. The
                # cursor stops where the complete reading stops, so the rest
                # is read next run instead of being stepped over.
                if newest is not None and newest > read_until:
                    newest = read_until
                self._log(f"'{source.name}': large backlog — read up to "
                          f"{utc_to_local(read_until, tz)}, continuing next run", "warning")

            # 2. By record id: whatever the platform stored after the last
            # record we have, whatever its timestamp — the punch an offline
            # terminal uploads hours later with an old time.
            if by_id is not None and read_until >= end_utc:   # a backlog run reads by time only
                try:
                    provider.truncated = False
                    late = consume(provider.fetch_punches_after(
                        by_id, since=utc_to_local(floor, tz), until=end_local))
                    if provider.truncated:
                        # Not read to the end, and not necessarily in id order:
                        # keep the id cursor where it was rather than skip ids.
                        top_id = max(x for x in (time_pass_top, by_id) if x is not None)
                    # A provider that checks ids one by one may stop part-way;
                    # the cursor goes no further than it checked.
                    reached = getattr(provider, "record_cursor_until", None)
                    if isinstance(reached, int) and top_id is not None and top_id > reached:
                        top_id = max(reached, by_id)
                except IdFilterUnsupported as exc:
                    late = 0
                    source.config = {**(source.config or {}), RECORD_CURSOR_OFF: True}
                    self._log(f"'{source.name}': reading by record id is not supported here "
                              f"({exc}); reading by time only", "warning", ui=False)
                created += late
                if late:
                    self._log(f"'{source.name}': {late} punch(es) found by record id "
                              "outside the time window (uploaded late)")

            cached = getattr(provider, "cached_token", None)
            if cached:
                source.token_enc = encrypt(cached, self.tenant.crypto_key)
            # A provider that reads by position rather than by time (COSEC's
            # sequence cursor) hands back where it got to. Saved only here,
            # after every punch it yielded is in the ledger.
            updates = getattr(provider, "config_updates", None)
            if updates:
                source.config = {**(source.config or {}), **updates}

            source.status = ConnectionStatus.connected.value
            source.status_message = None
            source.last_checked_at = datetime.now(timezone.utc)
            # Advanced last, and only over punches actually stored. If the
            # provider raised mid-iteration the cursor stays put, the rows
            # already written remain, and the next run re-reads the same window
            # — which the unique index makes free.
            if newest:
                source.cursor_punch_time = newest
            if getattr(provider, "record_id_cursor", False):
                self._advance_record_cursor(source, by_id, top_id, time_pass_top, time_pass_created)
        finally:
            provider.close()

        self.db.flush()
        self.run.punches_fetched += fetched
        self.run.punches_new += created
        self._log(f"'{source.name}': {fetched} punch(es) seen, {created} new")

    # -- reading a window by time ------------------------------------------
    def _read_by_time(self, provider, consume, start_utc: datetime, end_utc: datetime, tz: str):
        """Read [start, end] by time, slice by slice, oldest first.

        A provider that hit its page cap on a slice (``truncated``) may have
        left punches anywhere in it — BioTime, for one, pages in id order, not
        time order — so that slice is split in two and both halves are read
        again. Returns (new punches, read_until): everything before
        ``read_until`` was read completely. Bounded per run, so a huge backlog
        is worked off over several runs, each picking up where the last one's
        complete reading stopped.
        """
        created = 0
        slices = [(start_utc, end_utc)]
        read_until = start_utc
        reads = 0
        while slices:
            if reads >= MAX_SLICE_READS:
                return created, read_until
            lo, hi = slices.pop(0)
            reads += 1
            provider.truncated = False
            created += consume(provider.fetch_punches(
                since=utc_to_local(lo, tz), until=utc_to_local(hi, tz)))
            if provider.truncated and hi - lo > MIN_SLICE:
                mid = lo + (hi - lo) / 2
                slices[:0] = [(lo, mid), (mid, hi)]
                continue
            if provider.truncated:
                self._log(f"More punches than one read can hold between "
                          f"{utc_to_local(lo, tz)} and {utc_to_local(hi, tz)}; "
                          "some may be missing — raise MAX_PAGES_PER_RUN", "warning")
            read_until = hi
        return created, read_until

    # -- record-id cursor --------------------------------------------------
    @staticmethod
    def _record_cursor(source: DeviceSource, provider) -> int | None:
        """The last record id stored for this source, when its platform can
        be read by id and hasn't been found to ignore that."""
        if not getattr(provider, "record_id_cursor", False):
            return None
        config = source.config or {}
        if config.get(RECORD_CURSOR_OFF):
            return None
        value = config.get(RECORD_CURSOR_KEY)
        return int(value) if isinstance(value, int) or str(value or "").isdigit() else None

    def _advance_record_cursor(
        self, source: DeviceSource, old: int | None, top: int | None,
        time_top: int | None, time_created: int,
    ) -> None:
        """Move the id cursor to the highest record id stored this run.

        Ids only grow, so a *new* punch whose id is below the cursor means the
        platform started counting again (log cleared, device replaced or
        reset): the cursor follows it down rather than waiting, possibly for
        ever, for ids to climb past the old value.
        """
        if top is None:
            return
        config = dict(source.config or {})
        current = config.get(RECORD_CURSOR_KEY)
        current = int(current) if str(current or "").isdigit() else old
        if current is not None and time_created and time_top is not None and time_top < current:
            self._log(f"'{source.name}': record ids restarted (now {time_top}, was {current}); "
                      "following the new sequence", "warning", ui=False)
            config[RECORD_CURSOR_KEY] = time_top
        elif current is None or top > current:
            config[RECORD_CURSOR_KEY] = top
        else:
            return
        source.config = config

    def _claim_pushed(self, source: DeviceSource, provider) -> None:
        """A push device's punches are already in the ledger — they were
        stored as they arrived (app/api/adms.py). Stamp the ones no run has
        claimed yet with this run, so "N new" and Activity read the same as
        for a pulled source, and report the device's health from when it
        last called in. Never raises: an offline terminal keeps its punches
        and sends them later, and what already arrived must still reach Odoo."""
        from sqlalchemy import update

        result = self.db.execute(
            update(PunchRecord)
            .where(PunchRecord.tenant_id == self.tenant.id,
                   PunchRecord.source_id == source.id,
                   PunchRecord.first_seen_run_id.is_(None))
            .values(first_seen_run_id=self.run.id)
        )
        created = result.rowcount or 0
        health = provider.test_connection()
        source.status = (ConnectionStatus.connected if health.ok else ConnectionStatus.failed).value
        source.status_message = None if health.ok else health.message[:500]
        source.last_checked_at = datetime.now(timezone.utc)
        newest = self.db.scalar(select(func.max(PunchRecord.punch_time_utc)).where(
            PunchRecord.tenant_id == self.tenant.id, PunchRecord.source_id == source.id))
        if newest:
            source.cursor_punch_time = newest
        self.db.flush()
        self.run.punches_fetched += created
        self.run.punches_new += created
        self._log(f"'{source.name}': {created} pushed punch(es) since the last run"
                  + ("" if health.ok else f" — {health.message}"),
                  "info" if health.ok else "warning")

    def _known_external_ids(
        self, source: DeviceSource, external_ids: Collection[str]
    ) -> set[str]:
        """Which of *these* ids the ledger already holds.

        Scoped to the batch on purpose. It used to load every external id the
        tenant had ever stored into a Python set, once per run — so its cost
        grew with the age of the account rather than with the work in front of
        it. Measured on a 5,000-employee tenant: 0.06 s and 2 MB at 10k rows,
        5.3 s and 232 MB at 1M, for a run that had 67 new punches to ingest. At
        a 1-minute interval across 500 tenants that one query was 44 CPU cores,
        against roughly 1.4 for the actual work, and it grew every day the
        system ran.

        Scoped this way it is an index seek on ``uq_punch_external`` over a few
        hundred values, so the cost tracks the batch and stays flat forever.
        """
        ids = list(dict.fromkeys(external_ids))  # de-dupe, keep order for tests
        if not ids:
            return set()

        found: set[str] = set()
        # Chunked to stay under SQLite's bound-parameter ceiling (999 on older
        # builds). Postgres would take the lot, but the limit has to hold for
        # the smallest deployment, not the largest.
        for start in range(0, len(ids), ID_LOOKUP_CHUNK):
            found.update(
                self.db.scalars(
                    select(PunchRecord.external_id).where(
                        PunchRecord.tenant_id == self.tenant.id,
                        PunchRecord.source_id == source.id,
                        PunchRecord.external_id.in_(ids[start:start + ID_LOOKUP_CHUNK]),
                    )
                ).all()
            )
        return found

    def _ingest_batch(
        self, batch: list[tuple[object, datetime]], source: DeviceSource, devices: dict
    ) -> int:
        """Store the new punches in one buffer, and report how many were new."""
        seen = self._known_external_ids(source, [event.external_id for event, _ in batch])
        fresh = []
        for event, punch_utc in batch:
            # The in-batch check matters as much as the database one: the fetch
            # window overlaps the previous run deliberately, and a provider may
            # repeat a row across pages, so the same id can appear twice inside
            # a single batch and would otherwise violate the unique index.
            if event.external_id in seen:
                continue
            seen.add(event.external_id)
            fresh.append((event, punch_utc))
        created = insert_punches(self.db, fresh, lambda e, t: self._ingest(e, source, devices, t),
                                 refresh_devices=lambda: _reload_devices(self.db, source, devices))
        # Each batch is committed: the punches are safe in the ledger as soon as
        # they are read (a re-read is deduplicated), and the terminals' counters
        # aren't locked for the rest of a long first sync.
        self.db.commit()
        return created

    def _ingest(self, event, source: DeviceSource, devices: dict, punch_utc: datetime) -> None:
        store_punch(self.db, self.tenant, source, devices, event, punch_utc, run_id=self.run.id,
                    register_unknown=getattr(self, "_listed", None) is None)

    # -- stage 4 -----------------------------------------------------------
    def _register_badges(self) -> None:
        """Create an unmapped row for every badge seen, before Odoo is involved."""
        seen = set(
            self.db.scalars(
                select(PunchRecord.emp_code).where(
                    PunchRecord.tenant_id == self.tenant.id,
                    PunchRecord.process_state.in_(
                        [PunchState.pending.value, PunchState.unmapped.value]
                    ),
                )
            ).all()
        )
        if not seen:
            return

        known = set(
            self.db.scalars(
                select(EmployeeMapping.emp_code).where(
                    EmployeeMapping.tenant_id == self.tenant.id
                )
            ).all()
        )

        added = 0
        for code in sorted(seen - known):
            self.db.add(
                EmployeeMapping(
                    tenant_id=self.tenant.id,
                    emp_code=code,
                    source_name=self._name_for(code),
                    status=MappingStatus.unmapped.value,
                    match_note="Waiting to be matched to an Odoo employee.",
                )
            )
            added += 1

        if added:
            self.db.flush()
            self._log(f"Registered {added} new badge ID(s) awaiting a match")

    def _name_for(self, emp_code: str) -> str | None:
        row = self.db.scalars(
            select(PunchRecord)
            .where(
                PunchRecord.tenant_id == self.tenant.id,
                PunchRecord.emp_code == emp_code,
            )
            .limit(1)
        ).first()
        if row and row.raw:
            name = f"{row.raw.get('first_name') or ''} {row.raw.get('last_name') or ''}"
            return name.strip() or None
        return None

    # -- roster provisioning (runs once per cycle, ahead of stage 5) -------
    def _provision_employees(self, odoo: OdooClient, sources: list[DeviceSource]) -> None:
        """Push Odoo's active roster at every source opted into it.

        Odoo is the source of truth here, by design (see DeviceSource.
        auto_provision_employees): for each source with the flag on, any
        active Odoo employee this source's provider doesn't already know
        about gets created there. This never touches a biometric template —
        no vendor lets one be pushed remotely (see the provider modules'
        docstrings) — only identity: the id/name a person can then clock
        against once someone enrolls their fingerprint or face locally at
        the terminal, or issues them a card/PIN.

        Matching reuses the same emp_code-priority scheme as stage 5 in the
        opposite direction (OdooClient.employee_code_for mirrors
        find_employee's MATCH_FIELDS order), so a code minted here for a
        new device user is exactly the one a future punch from that person
        will carry back — no separate vendor-specific mapping logic needed.
        """
        targets = [s for s in sources if s.auto_provision_employees]
        if not targets:
            return
        if is_multi_company(odoo):
            # Which company's people belong on which terminal is not known, so
            # none are created on any of them.
            self._log(MULTI_COMPANY_PROVISION_MESSAGE, "warning", ui=False)
            return

        try:
            roster = odoo.list_employees()
        except OdooError as exc:
            self._log(f"Could not read the Odoo roster for provisioning: {exc}", "warning", ui=False)
            return

        wanted: dict[str, str] = {}  # emp_code -> name, active employees only
        for row in roster:
            if not row.get("active", True):
                continue
            code = odoo.employee_code_for(row)
            if not code or code in wanted:
                continue
            wanted[code] = row.get("name") or code
        if not wanted:
            return

        provisioned = 0
        for source in targets:
            try:
                provider = build_source_provider(self.tenant, source)
            except (ProviderError, UnsupportedCapability, UnsafeTargetError) as exc:
                self._log(f"'{source.name}': could not connect to provision employees: {exc}", "warning", ui=False)
                continue

            try:
                if not (
                    provider.supports(Capability.READ_EMPLOYEES)
                    and provider.supports(Capability.WRITE_EMPLOYEES)
                ):
                    continue  # this vendor has no create-employee capability at all

                try:
                    existing_codes = {
                        e.emp_code for e in provider.fetch_employees() if e.emp_code
                    }
                except (ProviderError, UnsupportedCapability) as exc:
                    self._log(f"'{source.name}': could not read its employee list: {exc}", "warning", ui=False)
                    continue

                created_here = 0
                for code, name in wanted.items():
                    if code in existing_codes:
                        continue
                    first_name, _, last_name = name.partition(" ")
                    try:
                        provider.create_employee(
                            EmployeeRecord(
                                external_id=None,
                                emp_code=code,
                                first_name=first_name,
                                last_name=last_name,
                            )
                        )
                        created_here += 1
                    except (ProviderError, UnsupportedCapability) as exc:
                        # One bad code (too long for this protocol, a vendor
                        # validation rule, ...) must not abort the rest of
                        # the roster — same principle as _fetch_and_ingest
                        # isolating one dead source from the others.
                        self._log(f"'{source.name}': could not provision '{code}': {exc}", "warning", ui=False)

                if created_here:
                    self._log(f"'{source.name}': provisioned {created_here} employee(s) from Odoo's roster", ui=False)
                provisioned += created_here
            finally:
                provider.close()

        self.run.employees_provisioned = provisioned

    # -- roster import (device users -> matched employees) -----------------
    def _import_matched_employees(self, odoo: OdooClient, sources: list[DeviceSource]) -> None:
        """Both ends are connected: link device users to Odoo employees now,
        without waiting for a punch. See app.services.roster_import."""
        from app.services.roster_import import import_matched_employees

        try:
            imported = import_matched_employees(
                self.db, self.tenant, odoo, sources, self._log, build_source_provider
            )
        except Exception as exc:  # noqa: BLE001 — an import is a convenience, never a reason to fail a sync
            self._log(f"Employee import skipped: {exc}", "warning", ui=False)
            return
        if imported:
            self.run.employees_matched = (self.run.employees_matched or 0) + imported

    # -- stage 4b: which companies are on --------------------------------
    def _reconcile_company_scope(self, odoo: OdooClient) -> None:
        """Keep mappings in step with the companies switched on for this Odoo.

        An employee whose company was switched off leaves the active roster
        (status ``out_of_scope``: hidden, uncounted, not synced) and comes back
        — to ``mapped``, with the same Odoo employee — the run after it is
        switched on again. The roster is read from Odoo itself, so this holds
        however the company set changed. No-op when nothing is restricted and
        nothing is held out.
        """
        held = list(self.db.scalars(
            select(EmployeeMapping).where(
                EmployeeMapping.tenant_id == self.tenant.id,
                EmployeeMapping.status == MappingStatus.out_of_scope.value,
            )
        ).all())
        scope = odoo.company_scope()
        if scope is None and not held:
            return
        try:
            enabled = {row["id"] for row in odoo.list_employees()}
        except OdooError as exc:
            self._log(f"Could not check which companies are on: {exc}", "warning", ui=False)
            return

        released = parked = 0
        for mapping in held:
            if mapping.odoo_employee_id in enabled or scope is None:
                mapping.status = MappingStatus.mapped.value
                mapping.match_note = None
                released += 1
        if scope is not None:
            mapped = self.db.scalars(
                select(EmployeeMapping).where(
                    EmployeeMapping.tenant_id == self.tenant.id,
                    EmployeeMapping.status == MappingStatus.mapped.value,
                    EmployeeMapping.odoo_employee_id.is_not(None),
                )
            ).all()
            for mapping in mapped:
                if mapping.odoo_employee_id not in enabled:
                    mapping.status = MappingStatus.out_of_scope.value
                    mapping.match_note = (
                        "This employee is not in a company that is switched on for "
                        "your Odoo connection."
                    )
                    parked += 1
        if released or parked:
            self.db.flush()
            self._log(
                f"Company switches: {parked} employee(s) set aside, {released} restored",
                ui=False,
            )

    # -- stage 5 -----------------------------------------------------------
    def _resolve_mappings(self, odoo: OdooClient) -> None:
        self._roster_cache: list[dict] | None = None   # read once per run, only if a name lookup needs it
        self._taken_ids: set[int] | None = None
        pending = set(
            self.db.scalars(
                select(PunchRecord.emp_code).where(
                    PunchRecord.tenant_id == self.tenant.id,
                    PunchRecord.process_state.in_(
                        [PunchState.pending.value, PunchState.unmapped.value]
                    ),
                )
            ).all()
        )
        if not pending:
            return

        existing = {
            m.emp_code: m
            for m in self.db.scalars(
                select(EmployeeMapping).where(
                    EmployeeMapping.tenant_id == self.tenant.id,
                    EmployeeMapping.emp_code.in_(pending),
                )
            ).all()
        }

        # The plan's employee cap, if any. Counted once up front rather than
        # re-queried per code: every new match below increments it in memory,
        # so the loop sees a consistent, monotonically-updated count instead
        # of racing its own writes within the same run. Skipped entirely when
        # there is no cap, which is the common case and the state of every
        # tenant with no plan assigned.
        cap = self.tenant.plan_max_employees
        mapped_count = 0
        if cap is not None:
            mapped_count = self.db.scalar(
                select(func.count(EmployeeMapping.id)).where(
                    EmployeeMapping.tenant_id == self.tenant.id,
                    EmployeeMapping.status == MappingStatus.mapped.value,
                )
            ) or 0

        matched = 0
        for code in sorted(pending):
            mapping = existing.get(code)
            if mapping and mapping.status in (
                MappingStatus.mapped.value,
                MappingStatus.ignored.value,
                MappingStatus.removed.value,
                MappingStatus.out_of_scope.value,
            ):
                continue
            if mapping is None:
                mapping = EmployeeMapping(tenant_id=self.tenant.id, emp_code=code)
                self.db.add(mapping)
                existing[code] = mapping

            # The cap only holds back *new* matches. A mapping already
            # `mapped` was excluded above and never reaches here, so a plan
            # downgraded under a tenant already over its new cap does not
            # start unmapping people relying on it — see
            # SubscriptionPlan.max_employees. Checked before the Odoo lookup
            # below on purpose: there is no reason to spend an API call, or
            # auto-create an employee in Odoo, for a badge this run is not
            # going to use anyway.
            if cap is not None and mapped_count >= cap:
                mapping.status = MappingStatus.unmapped.value
                mapping.source_name = self._name_for(code)
                mapping.match_note = (
                    f"Not matched: this account's plan allows up to {cap} mapped "
                    f"employee(s), and that many are already in use. Upgrade the "
                    f"plan, or free one up, to match this badge."
                )
                continue

            try:
                emp_id, emp_name, method = odoo.find_employee(code)
            except OdooError as exc:
                mapping.match_note = f"Lookup failed: {exc}"
                continue

            device_name = usable_name(self._name_for(code), code)
            if emp_id and device_name and not names_compatible(device_name, emp_name):
                # The badge is on someone else by name: a person decides.
                if dismissed(mapping):
                    hold_dismissed_badge(mapping, code, device_name, emp_name)
                else:
                    suggest_badge_only(mapping, code, device_name, emp_id, emp_name)
                continue
            if emp_id:
                mapping.odoo_employee_id = emp_id
                mapping.odoo_employee_name = emp_name
                mapping.status = MappingStatus.mapped.value
                mapping.match_method = method
                mapping.match_note = None
                clear_suggestion(mapping)
                if self._taken_ids is not None:
                    self._taken_ids.add(emp_id)
                matched += 1
                mapped_count += 1
            elif method and method.startswith("ambiguous:"):
                mapping.status = MappingStatus.ambiguous.value
                mapping.match_note = (
                    f"Several Odoo employees share {method.split(':')[1]}={code}. "
                    "Pick one in the dashboard."
                )
            elif self._in_switched_off_company(odoo, code, mapping):
                continue
            elif device_name and not dismissed(mapping) and self._suggest_by_name(odoo, code, device_name, mapping):
                continue
            elif self.tenant.auto_create_employees:
                name = self._name_for(code) or f"Employee {code}"
                try:
                    mapping.odoo_employee_id = odoo.create_employee(name, code)
                except OdooError as exc:
                    mapping.match_note = f"Auto-create failed: {exc}"
                    continue
                mapping.odoo_employee_name = name
                mapping.status = MappingStatus.mapped.value
                mapping.match_method = "auto_created"
                matched += 1
                mapped_count += 1
            else:
                clear_suggestion(mapping)
                mapping.status = MappingStatus.unmapped.value
                mapping.source_name = self._name_for(code)
                mapping.match_note = (
                    f"No Odoo employee has barcode / pin / registration number "
                    f"'{code}'. Set it in Odoo, or map by hand."
                )

        self.run.employees_matched = matched
        self.db.flush()
        if matched:
            self._log(f"Matched {matched} new employee(s) to Odoo")

    def _suggest_by_name(self, odoo: OdooClient, code: str, device_name: str,
                         mapping: EmployeeMapping) -> bool:
        """A badge found nowhere in Odoo whose name is: suggest that employee
        instead of creating a duplicate. True when the badge was dealt with
        (suggested, or held because Odoo's roster could not be read), False when
        nobody by that name exists and the usual path (create / leave) goes on."""
        try:
            if self._roster_cache is None:
                self._roster_cache = [r for r in odoo.list_employees() if r.get("active", True)]
        except OdooError as exc:
            # Creating blind could duplicate someone who is there: wait instead.
            mapping.match_note = f"Lookup failed: {exc}"
            return True
        if self._taken_ids is None:
            self._taken_ids = set(self.db.scalars(
                select(EmployeeMapping.odoo_employee_id).where(
                    EmployeeMapping.tenant_id == self.tenant.id,
                    EmployeeMapping.status == MappingStatus.mapped.value,
                    EmployeeMapping.odoo_employee_id.is_not(None),
                )
            ).all())
        found = name_candidates(device_name, self._roster_cache, self._taken_ids)
        if not found:
            return False
        suggest_name_only(mapping, code, device_name, found[0], len(found) - 1)
        return True

    def _in_switched_off_company(
        self, odoo: OdooClient, code: str, mapping: EmployeeMapping
    ) -> bool:
        """Whether this badge belongs to an employee of a company that is
        switched off — checked before auto-creating, so a person from a
        disabled company is not duplicated into an enabled one."""
        if odoo.company_scope() is None:
            return False
        try:
            emp_id, emp_name, _method = odoo.find_employee(code, scoped=False)
        except OdooError:
            return False
        if not emp_id:
            return False
        mapping.odoo_employee_id = emp_id
        mapping.odoo_employee_name = emp_name
        mapping.status = MappingStatus.out_of_scope.value
        mapping.match_note = (
            "This employee is in a company that is switched off for your Odoo "
            "connection. Switch it on to sync their attendance."
        )
        return True

    # -- stages 6-8 --------------------------------------------------------
    def _push(self, odoo: OdooClient, odoo_conn: OdooConnection) -> None:
        mappings = {
            m.emp_code: m
            for m in self.db.scalars(
                select(EmployeeMapping).where(EmployeeMapping.tenant_id == self.tenant.id)
            ).all()
        }

        punches = self.db.scalars(
            select(PunchRecord)
            .where(
                PunchRecord.tenant_id == self.tenant.id,
                PunchRecord.process_state.in_(
                    [
                        PunchState.pending.value,
                        PunchState.unmapped.value,
                        PunchState.error.value,
                        PunchState.held.value,
                    ]
                ),
                PunchRecord.attempts < 5,
            )
            .order_by(PunchRecord.emp_code, PunchRecord.punch_time_utc)
        ).all()

        if not punches:
            self._log("Nothing to push — Odoo is up to date")
            return

        over_limit = over_limit_device_ids(self.db, self.tenant)
        cap = self.tenant.plan_max_devices
        held = 0
        company_held = 0
        enabled_companies = self._enabled_company_ids(odoo)
        by_employee: dict[str, list[PunchRecord]] = {}
        for punch in punches:
            if over_limit:
                device = device_for_punch(self.db, punch)
                if device is not None and device.id in over_limit:
                    punch.process_state = PunchState.held.value
                    punch.error_message = (
                        f"Held: terminal {device.alias or device.serial_number} is beyond your "
                        f"plan's {cap}-device limit. Upgrade, or remove a terminal, to send it."
                    )
                    held += 1
                    continue
            mapping = mappings.get(punch.emp_code)
            if mapping is None or mapping.status in (
                MappingStatus.unmapped.value, MappingStatus.out_of_scope.value,
            ):
                punch.process_state = PunchState.unmapped.value
                continue
            if mapping.status in (MappingStatus.ignored.value, MappingStatus.ambiguous.value,
                                  MappingStatus.removed.value):
                punch.process_state = PunchState.skipped.value
                punch.error_message = f"Mapping is {mapping.status}"
                continue
            reason = self._company_hold_reason(punch, mapping, enabled_companies)
            if reason:
                punch.process_state = PunchState.held.value
                punch.error_message = reason
                company_held += 1
                continue
            if punch.process_state == PunchState.held.value:
                # Held earlier for its terminal's company, and that is now
                # sorted out (terminal assigned, or employee moved).
                punch.process_state = PunchState.pending.value
                punch.error_message = None
            by_employee.setdefault(punch.emp_code, []).append(punch)

        if company_held:
            self._log(
                f"{company_held} punch(es) held for review: the terminal's Odoo company "
                "is not set, or differs from the employee's company", "warning",
            )
        if held:
            self._log(
                f"{held} punch(es) held: from terminal(s) beyond the plan's "
                f"{cap}-device limit", "warning",
            )

        for emp_code, emp_punches in by_employee.items():
            mapping = mappings[emp_code]
            try:
                self._push_employee(odoo, odoo_conn, mapping, emp_punches)
            except OdooError as exc:
                self.run.error_count += 1
                for punch in emp_punches:
                    if punch.process_state == PunchState.synced.value and punch.odoo_attendance_id:
                        continue  # already in Odoo earlier in this push — a later failure doesn't undo it
                    punch.process_state = PunchState.error.value
                    punch.error_message = str(exc)[:500]
                    punch.attempts += 1
                self._log(f"{emp_code}: {exc}", "error")

        self.db.flush()

    # -- terminal <-> company isolation ------------------------------------
    def _enabled_company_ids(self, odoo: OdooClient) -> list[int] | None:
        """Every company this connection may touch, or None when that can't
        be read (then only explicit mismatches are caught, nothing is held
        for being unassigned)."""
        return enabled_company_ids(odoo)

    def _company_hold_reason(
        self, punch: PunchRecord, mapping: EmployeeMapping, enabled: list[int] | None
    ) -> str | None:
        """Why this punch must wait for review, or None to send it.

        A terminal belongs to one Odoo company (its own, else its
        connection's default). A punch is held when the employee is in a
        different company, and — on an Odoo with several companies on — when
        the terminal has no company yet, because there is then no telling
        whose attendance it is. With a single company on, an unassigned
        terminal is fine: there is nowhere wrong to send it.
        """
        device = device_for_punch(self.db, punch)
        company_id = device.company_id if device is not None else None
        company_name = device.company_name if device is not None else None
        if company_id is None:
            source = self._source_cache.get(punch.source_id)
            if source is None and punch.source_id:
                source = self.db.get(DeviceSource, punch.source_id)
                self._source_cache[punch.source_id] = source
            if source is not None:
                company_id = source.default_company_id
                company_name = source.default_company_name
        label = (device.alias or device.serial_number) if device is not None else (
            punch.terminal_sn or "this terminal")
        if company_id is None:
            if enabled is not None and len(enabled) > 1:
                return (
                    f"Held: company not set for terminal {label}. Your Odoo has several "
                    "companies on, so choose this terminal's company on the Terminals page."
                )
            return None
        if enabled is not None and company_id not in enabled:
            return (
                f"Held: company mismatch: terminal {label} belongs to "
                f"{company_name or f'company {company_id}'}, which is switched off for this "
                "connection."
            )
        emp_company = mapping.odoo_company_id
        if emp_company is not None and emp_company != company_id:
            return (
                f"Held: company mismatch: terminal {label} belongs to "
                f"{company_name or f'company {company_id}'}, but {mapping.odoo_employee_name or punch.emp_code} "
                f"is in {mapping.odoo_company_name or f'company {emp_company}'}. "
                "Review it, or change the terminal's company."
            )
        return None

    def _push_employee(
        self,
        odoo: OdooClient,
        odoo_conn: OdooConnection,
        mapping: EmployeeMapping,
        punches: list[PunchRecord],
    ) -> None:
        employee_id = mapping.odoo_employee_id
        assert employee_id is not None

        # The shift already open for this person, taken from Odoo so a record
        # closed by hand there is respected, and reconciled with what we stored.
        open_shift = self._current_open_shift(odoo, mapping)

        by_id = {p.id: p for p in punches}
        pending_ids = set(by_id)
        mode = self._mode_for(punches)
        config = self.pairing_config(mode)
        used_mode = config.mode.value

        # An open record that newer records already sit after cannot be closed
        # by a later punch — Odoo refuses — so it is closed where the next one
        # begins, and the punches then start fresh.
        open_shift = self._close_blocked_open_shifts(odoo, mapping, open_shift, config)

        # First/last is "the first and last punch of the *day*", so a punch that
        # arrives in a later cycle has to be weighed against the ones already
        # pushed earlier that day — otherwise every cycle opens its own record.
        # The day's already-synced punches are brought back in (they are only
        # inputs: they are never re-marked or re-skipped), and the day's one
        # record is then updated in place.
        day_record: dict[object, int] = {}
        pairing_input = list(punches)
        if config.mode is PairingMode.first_last:
            earlier = self._synced_punches_for_days(punches, config)
            for p in earlier:
                by_id[p.id] = p
            pairing_input += earlier
            for p in sorted(earlier, key=lambda x: x.punch_time_utc):
                if p.odoo_attendance_id:
                    day_record.setdefault(day_of(p.punch_time_utc, config), p.odoo_attendance_id)

        result = pair_punches(
            [
                Punch(
                    punch_id=p.id,
                    emp_code=p.emp_code,
                    time_utc=p.punch_time_utc,
                    direction=PairDirection(p.direction),
                    terminal_sn=p.terminal_sn,
                )
                for p in pairing_input
            ],
            config,
            open_shift=open_shift,
        )

        for punch_id in result.skipped_punch_ids:
            punch = by_id.get(punch_id)
            if punch is not None and punch_id in pending_ids:
                punch.process_state = PunchState.skipped.value
                punch.error_message = "Duplicate punch within the minimum interval"

        for warning in result.warnings:
            self._log(warning, "warning")

        for interval in result.intervals:
            try:
                if interval.closes_attendance_id is not None:
                    # Closes a shift opened in an earlier cycle. No new record is
                    # created — this is the case a naive engine turns into a phantom.
                    self._clip_to_next_record(odoo, mapping, interval, interval.check_in)
                    odoo.close_attendance(
                        interval.closes_attendance_id, interval.check_out, pairing_mode=used_mode,
                        auto_closed=interval.auto_closed,
                    )
                    self.run.attendances_closed += 1
                    attendance_id = interval.closes_attendance_id
                elif (
                    config.mode is PairingMode.first_last
                    and (day_id := day_record.get(day_of(interval.check_in, config)))
                ):
                    # The day already has its record from an earlier cycle: move its
                    # check-in / check-out to the day's current first and last punch.
                    odoo.update_attendance(
                        day_id, interval.check_in, interval.check_out, pairing_mode=used_mode,
                        auto_closed=interval.auto_closed,
                    )
                    attendance_id = day_id
                else:
                    existing = odoo.attendance_exists(employee_id, interval.check_in)
                    if existing:
                        if interval.check_out:
                            self._clip_to_next_record(odoo, mapping, interval, interval.check_in)
                            odoo.close_attendance(
                                existing, interval.check_out, pairing_mode=used_mode,
                                auto_closed=interval.auto_closed,
                            )
                            self.run.attendances_closed += 1
                        attendance_id = existing
                    elif recovered_id := self._recover_from_lost_close(
                        odoo, employee_id, interval, by_id
                    ):
                        # interval.check_in/check_out were just corrected in place
                        # to the real shift's times — see the method's docstring.
                        attendance_id = recovered_id
                        self._log(
                            f"{mapping.emp_code}: recovered a check-out whose local "
                            f"state was lost after an earlier attempt already closed "
                            f"attendance {attendance_id} in Odoo — nothing to write",
                        )
                    elif self._settle_overlap(odoo, mapping, interval, by_id, pending_ids, config):
                        continue  # covered by a record already in Odoo — nothing to write
                    else:
                        self._clip_to_next_record(odoo, mapping, interval, interval.check_in)
                        attendance_id = odoo.create_attendance(
                            employee_id,
                            interval.check_in,
                            interval.check_out,
                            biotime_ref=self._ref(interval, by_id)
                            if odoo_conn.has_companion_addon
                            else None,
                            device_id=self._odoo_device_id(
                                odoo, odoo_conn, self._terminal_for(interval, by_id)
                            ),
                            pairing_mode=used_mode,
                            auto_closed=interval.auto_closed,
                        )
                        self.run.attendances_created += 1
                        if interval.check_out:
                            self.run.attendances_closed += 1

                self._mark(by_id, interval, attendance_id)
                self._record_interval(mapping, interval, attendance_id, by_id, used_mode)

                # Carry the open shift forward for the next cycle. This is the state
                # that makes a later lone check-out resolvable.
                if interval.check_out is None:
                    mapping.open_attendance_id = attendance_id
                    mapping.open_check_in = interval.check_in
                else:
                    mapping.open_attendance_id = None
                    mapping.open_check_in = None
            except OdooError as exc:
                # One interval Odoo refuses must not take the employee's other
                # intervals down with it: only its own punches are marked, the
                # rest of the batch is still sent.
                self._interval_failed(mapping, interval, by_id, pending_ids, exc)

        if config.mode is PairingMode.first_last:
            self._settle_covered_punches(punches, result.intervals, by_id)

        mapping.last_synced_at = datetime.now(timezone.utc)
        if punches:
            mapping.last_punch_at = max(p.punch_time_utc for p in punches)

    def _interval_failed(
        self, mapping: EmployeeMapping, interval, by_id: dict[str, PunchRecord],
        pending_ids: set, exc: OdooError,
    ) -> None:
        """Mark just the punches of one interval as failed, saying which one."""
        tz = self.tenant.timezone
        span = f"{utc_to_local(interval.check_in, tz):%Y-%m-%d %H:%M}"
        if interval.check_out is not None:
            span += f" to {utc_to_local(interval.check_out, tz):%Y-%m-%d %H:%M}"
        message = f"Interval {span}: {exc}"[:500]
        self.run.error_count += 1
        for punch_id in {interval.check_in_punch_id, interval.check_out_punch_id}:
            punch = by_id.get(punch_id or "")
            if punch is None or punch_id not in pending_ids:
                continue
            if punch.process_state == PunchState.synced.value and punch.odoo_attendance_id:
                continue
            punch.process_state = PunchState.error.value
            punch.error_message = message
            punch.attempts += 1
        self._log(f"{mapping.emp_code}: {message}", "error")

    def _clip_to_next_record(
        self, odoo: OdooClient, mapping: EmployeeMapping, interval, record_check_in: datetime
    ) -> None:
        """Keep an interval's check-out from reaching past the next record.

        Odoo refuses any record whose check-out runs beyond another record's
        check-in (``the employee was already checked in on <that record>``),
        on create and on write alike. Punches paired across a gap — a stale
        one with a recent one, or old ones released late — can span a record
        that is already in Odoo. The check-out is cut back to where that record
        begins and the interval flagged as closed automatically, so the write
        succeeds and the punches are settled instead of erroring on every cycle.
        """
        if interval.check_out is None:
            return
        later = odoo.attendance_after(mapping.odoo_employee_id, record_check_in)
        starts = parse_dt(later.get("check_in")) if later else None
        if starts is None or starts >= interval.check_out:
            return
        was = interval.check_out
        interval.check_out = starts
        interval.auto_closed = True
        interval.notes.append("Check-out limited to where the next record in Odoo begins")
        self._log(
            f"{mapping.emp_code}: the check-out at "
            f"{utc_to_local(was, self.tenant.timezone):%Y-%m-%d %H:%M} would run past another "
            f"attendance that begins at {utc_to_local(starts, self.tenant.timezone):%Y-%m-%d %H:%M}; "
            "ended this one there", "warning",
        )

    def close_stale_shifts(self, odoo: OdooClient) -> int:
        """Close every Odoo record of this account's matched employees still
        open longer than the shift limit. Returns how many were closed.

        One search for all of them (``open_attendances``), oldest first, and
        every open record — not just each person's latest, since an old one can
        sit open behind a newer one. Each ends at the shift limit, or earlier
        where the employee's next record begins (Odoo refuses an overlap).
        """
        mappings: dict[int, EmployeeMapping] = {}
        for m in self.db.scalars(select(EmployeeMapping).where(
                EmployeeMapping.tenant_id == self.tenant.id,
                EmployeeMapping.status == MappingStatus.mapped.value,
                EmployeeMapping.odoo_employee_id.is_not(None))):
            mappings[m.odoo_employee_id] = m
        if not mappings:
            return 0
        config = self.pairing_config()
        now = stale_clock()
        closed = 0
        for rec in odoo.open_attendances(list(mappings), now - timedelta(hours=config.max_shift_hours)):
            emp = rec.get("employee_id")
            emp_id = int(emp[0] if isinstance(emp, (list, tuple)) else emp)
            mapping = mappings.get(emp_id)
            check_in = parse_dt(rec.get("check_in"))
            end = close_stale_at(check_in, now, config) if check_in else None
            if mapping is None or end is None:
                continue
            try:
                blocked = self._blocked_end(odoo, emp_id, check_in, config)
                if blocked is not None:
                    end = min(end, blocked)
                odoo.close_attendance(rec["id"], end, auto_closed=True)
            except OdooError as exc:
                self._log(f"{mapping.emp_code}: could not close attendance {rec['id']} left open since "
                          f"{utc_to_local(check_in, self.tenant.timezone):%Y-%m-%d %H:%M}: {exc}", "warning")
                continue
            self._mirror_close(rec["id"], end, auto_closed=True)
            if mapping.open_attendance_id == rec["id"]:
                mapping.open_attendance_id = None
                mapping.open_check_in = None
            self._log(
                f"{mapping.emp_code}: attendance open since "
                f"{utc_to_local(check_in, self.tenant.timezone):%Y-%m-%d %H:%M} passed the "
                f"{config.max_shift_hours}h shift limit; closed it automatically at "
                f"{utc_to_local(end, self.tenant.timezone):%Y-%m-%d %H:%M}", "warning")
            closed += 1
        self.db.flush()
        return closed

    def _blocked_end(self, odoo: OdooClient, employee_id: int, check_in: datetime,
                     config: PairingConfig) -> datetime | None:
        """Where an open record that starts at ``check_in`` has to end because
        another record already begins after it, or None when nothing does.
        Never later than the shift limit."""
        later = odoo.attendance_after(employee_id, check_in)
        starts = parse_dt(later.get("check_in")) if later else None
        if starts is None:
            return None
        return min(starts, check_in + timedelta(hours=config.max_shift_hours))

    def _close_blocked_open_shifts(
        self, odoo: OdooClient, mapping: EmployeeMapping, open_shift: OpenShift | None,
        config: PairingConfig,
    ) -> OpenShift | None:
        """Close open records that newer records already follow.

        Punches can be processed out of order — a badge that was unmatched or
        held for days is released after later ones were sent — which leaves an
        older record open behind newer closed ones. Odoo allows creating that
        (an open record skips the overlap check) but never closing it later with
        a real check-out: the write fails with "the employee was already
        checked in on <the newer record>", and every punch of the employee
        after it fails with it. No check-out exists for it, so it ends where the
        next record starts, flagged as closed automatically.
        """
        for _ in range(5):                       # several can have piled up
            if open_shift is None:
                return None
            end = self._blocked_end(odoo, mapping.odoo_employee_id, open_shift.check_in, config)
            if end is None:
                return open_shift
            odoo.close_attendance(open_shift.attendance_id, end, auto_closed=True)
            self._mirror_close(open_shift.attendance_id, end, auto_closed=True)
            self._log(
                f"{mapping.emp_code}: attendance {open_shift.attendance_id} was still open from "
                f"{utc_to_local(open_shift.check_in, self.tenant.timezone):%Y-%m-%d %H:%M}, behind "
                f"newer records; closed it automatically at "
                f"{utc_to_local(end, self.tenant.timezone):%Y-%m-%d %H:%M}, where the next one begins",
                "warning",
            )
            self.run.attendances_closed += 1
            mapping.open_attendance_id = None
            mapping.open_check_in = None
            open_shift = self._current_open_shift(odoo, mapping)
        return open_shift

    def _settle_overlap(
        self,
        odoo: OdooClient,
        mapping: EmployeeMapping,
        interval,
        by_id: dict[str, PunchRecord],
        pending_ids: set,
        config: PairingConfig,
    ) -> bool:
        """Deal with a new check-in that falls inside a record Odoo already has.

        Odoo rejects overlapping attendances, closed ones included, so writing
        it would fail and — being retried — keep failing. Two cases:

        * The record was closed by BioBridge itself, at the shift limit
          (a forgotten check-out). That closure was a guess, and this punch
          proves the person was back at the door earlier, so the record is
          shortened to end where this one starts — the same rule as for a
          check-in that follows a forgotten check-out. Returns False: the
          caller goes on to create the new record.
        * A real record already spans it (a second check-in without a
          check-out, say). The punch is settled as covered, with the record
          named, rather than left as an error that Retry cannot fix. Returns
          True: nothing to write.
        """
        covering = odoo.attendance_covering(mapping.odoo_employee_id, interval.check_in)
        if covering is None:
            return False
        start = parse_dt(covering.get("check_in"))
        end = parse_dt(covering.get("check_out"))
        limit = timedelta(hours=config.max_shift_hours)
        local = self.db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.tenant_id == self.tenant.id,
                AttendanceRecord.odoo_attendance_id == covering["id"],
            )
        ).first()
        guessed = (local is not None and local.is_auto_closed) or (
            start is not None and end is not None and abs((end - start) - limit) < timedelta(seconds=2)
        )
        if guessed:
            odoo.close_attendance(covering["id"], interval.check_in, auto_closed=True)
            self._mirror_close(covering["id"], interval.check_in, auto_closed=True)
            self._log(
                f"{mapping.emp_code}: attendance {covering['id']} had been closed automatically "
                f"at the {config.max_shift_hours}h limit; shortened it to end at "
                f"{utc_to_local(interval.check_in, self.tenant.timezone):%Y-%m-%d %H:%M} "
                "where this check-in begins",
                "warning",
            )
            return False

        shown = (
            f"{utc_to_local(start, self.tenant.timezone):%Y-%m-%d %H:%M} to "
            f"{utc_to_local(end, self.tenant.timezone):%Y-%m-%d %H:%M}"
            if start and end else "an earlier time"
        )
        message = (
            f"Not sent: it falls inside attendance {covering['id']} ({shown}), which Odoo "
            "already has for this employee. Odoo does not allow overlapping attendances."
        )
        for punch_id in (interval.check_in_punch_id, interval.check_out_punch_id):
            punch = by_id.get(punch_id or "")
            if punch is not None and punch_id in pending_ids:
                punch.process_state = PunchState.skipped.value
                punch.error_message = message
        self._log(f"{mapping.emp_code}: {message}", "warning")
        return True

    #: How far back the copy follows Odoo on an account's first read.
    FOLLOW_ODOO_DAYS = 35
    #: Re-read this much before the last read: Odoo's clock and ours may differ.
    FOLLOW_ODOO_OVERLAP = timedelta(minutes=10)

    def _follow_odoo_attendance(self, odoo: OdooClient) -> None:
        """Bring BioBridge's copy of the attendance in line with Odoo, for the
        matched employees: records changed in Odoo since the last read are
        updated here, and ones that exist only in Odoo are added (as "Added in
        Odoo"). Odoo is the record of truth; this copy is what the Attendance
        page and Reports read. A failure here never fails the sync.
        """
        try:
            mappings = {m.odoo_employee_id: m for m in self.db.scalars(select(EmployeeMapping).where(
                EmployeeMapping.tenant_id == self.tenant.id,
                EmployeeMapping.status == MappingStatus.mapped.value,
                EmployeeMapping.odoo_employee_id.is_not(None)))}
            if not mappings:
                return
            started = utcnow_naive()
            floor = started - timedelta(days=self.FOLLOW_ODOO_DAYS)
            last = self.tenant.odoo_attendance_synced_at
            since = (last - self.FOLLOW_ODOO_OVERLAP) if last else floor
            rows = odoo.attendances_changed_since(list(mappings), since, floor)
            with self.db.begin_nested():   # a database error here undoes only this step
                known = {r.odoo_attendance_id: r for r in self.db.scalars(select(AttendanceRecord).where(
                    AttendanceRecord.tenant_id == self.tenant.id,
                    AttendanceRecord.odoo_attendance_id.in_([int(r["id"]) for r in rows] or [0])))}
                tz = self.tenant.timezone or "UTC"
                added = updated = 0
                for rec in rows:
                    emp = rec.get("employee_id")
                    mapping = mappings.get(int(emp[0] if isinstance(emp, (list, tuple)) else emp))
                    check_in = parse_dt(rec.get("check_in"))
                    if mapping is None or check_in is None:
                        continue
                    check_out = parse_dt(rec.get("check_out"))
                    row = known.get(int(rec["id"]))
                    if row is not None and row.check_in == check_in and row.check_out == check_out:
                        continue
                    if row is None:
                        row = AttendanceRecord(tenant_id=self.tenant.id, odoo_attendance_id=int(rec["id"]),
                                               emp_code=mapping.emp_code, origin="odoo",
                                               pairing_mode=None)
                        self.db.add(row)
                        added += 1
                    else:
                        updated += 1
                    row.employee_name = mapping.odoo_employee_name or mapping.source_name
                    row.department = mapping.department
                    row.odoo_employee_id = mapping.odoo_employee_id
                    row.check_in = check_in
                    row.check_out = check_out
                    row.check_in_local = utc_to_local(check_in, tz)
                    row.check_out_local = utc_to_local(check_out, tz) if check_out else None
                    row.shift_date = row.check_in_local.strftime("%Y-%m-%d")
                    row.worked_hours = (check_out - check_in).total_seconds() / 3600 if check_out else None
                self.tenant.odoo_attendance_synced_at = started
                self.db.flush()
                if added or updated:
                    self._log(f"Attendance from Odoo: {added} record(s) added here, {updated} updated to match Odoo",
                              "info")
        except Exception as exc:  # noqa: BLE001 — a copy for display must never fail a sync
            log.warning("following Odoo's attendance skipped for %s: %s", self.tenant.slug, exc)

    def _refresh_open_mirror(self, odoo: OdooClient) -> None:
        """Bring records BioBridge shows as open in step with Odoo.

        A shift can be closed in Odoo by something other than a sync — the
        hourly stale-shift sweep, or a person editing it by hand — and the
        local copy would then stay "Open" for ever. Odoo is authoritative:
        every locally-open record is looked up (one query), and any that
        Odoo shows with a check-out takes it. Records Odoo does not return
        are left alone: a company switch can hide them as well as a delete.
        """
        rows = self.db.scalars(
            select(AttendanceRecord)
            .where(
                AttendanceRecord.tenant_id == self.tenant.id,
                AttendanceRecord.check_out.is_(None),
                AttendanceRecord.odoo_attendance_id.is_not(None),
            )
            .order_by(AttendanceRecord.check_in.desc())
            .limit(500)
        ).all()
        if not rows:
            return
        try:
            live = odoo.attendances_by_id([r.odoo_attendance_id for r in rows])
        except OdooError as exc:
            log.warning("open-record refresh skipped for %s: %s", self.tenant.slug, exc)
            return
        limit = timedelta(hours=self.tenant.max_shift_hours)
        fixed = 0
        for row in rows:
            found = live.get(row.odoo_attendance_id)
            end = parse_dt(found.get("check_out")) if found else None
            if end is None:
                continue
            row.check_out = end
            row.check_out_local = utc_to_local(end, self.tenant.timezone)
            row.worked_hours = (end - row.check_in).total_seconds() / 3600
            if abs((end - row.check_in) - limit) < timedelta(seconds=2):
                row.is_auto_closed = True        # the shift-limit sweep's signature
            fixed += 1
        if fixed:
            self.db.flush()
            self._log(f"{fixed} attendance record(s) already closed in Odoo updated here", "info")

    def _mirror_close(self, attendance_id: int, check_out: datetime, *, auto_closed: bool) -> None:
        """Keep the local copy of a record in step when its check-out changes
        outside the normal pairing path (the stale-shift sweep, a shortened
        auto-close)."""
        row = self.db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.tenant_id == self.tenant.id,
                AttendanceRecord.odoo_attendance_id == attendance_id,
            )
        ).first()
        if row is None:
            return
        row.check_out = check_out
        row.check_out_local = utc_to_local(check_out, self.tenant.timezone)
        row.worked_hours = (check_out - row.check_in).total_seconds() / 3600
        row.is_auto_closed = auto_closed
        self.db.flush()

    def _synced_punches_for_days(
        self, pending: list[PunchRecord], config: PairingConfig
    ) -> list[PunchRecord]:
        """Punches already pushed for the same employee on the same shift days
        as ``pending`` — the other half of a first/last day."""
        if not pending:
            return []
        days = {day_of(p.punch_time_utc, config) for p in pending}
        lo = min(p.punch_time_utc for p in pending) - timedelta(hours=48)
        hi = max(p.punch_time_utc for p in pending) + timedelta(hours=48)
        rows = self.db.scalars(
            select(PunchRecord).where(
                PunchRecord.tenant_id == self.tenant.id,
                PunchRecord.emp_code == pending[0].emp_code,
                PunchRecord.process_state == PunchState.synced.value,
                PunchRecord.punch_time_utc >= lo,
                PunchRecord.punch_time_utc <= hi,
            )
        ).all()
        have = {p.id for p in pending}
        return [
            p for p in rows
            if p.id not in have and day_of(p.punch_time_utc, config) in days
        ]

    def _settle_covered_punches(
        self, punches: list[PunchRecord], intervals, by_id: dict[str, PunchRecord]
    ) -> None:
        """First/last ignores the punches between the day's first and last. They
        are covered by that day's record, so they are done — left pending they
        would be fed back into pairing every cycle."""
        for punch in punches:
            if punch.process_state in (PunchState.synced.value, PunchState.skipped.value):
                continue
            for interval in intervals:
                end = interval.check_out
                if interval.check_in <= punch.punch_time_utc and (
                    end is None or punch.punch_time_utc <= end
                ):
                    first = by_id.get(interval.check_in_punch_id or "")
                    punch.process_state = PunchState.synced.value
                    punch.odoo_attendance_id = first.odoo_attendance_id if first else None
                    punch.error_message = None
                    break

    def _recover_from_lost_close(
        self,
        odoo: OdooClient,
        employee_id: int,
        interval,
        by_id: dict[str, PunchRecord],
    ) -> int | None:
        """Tell a genuinely new shift apart from a check-out replaying after a
        crash between its Odoo write and the local commit that would have
        marked it synced.

        By the time this runs, two things are already true: there was no
        open shift in Odoo to close (``_current_open_shift`` just checked),
        and ``attendance_exists`` found nothing at this ``check_in`` either.
        For an ordinary new shift that is simply the answer — create it —
        and this function is not even called (see the ``elif`` at the call
        site, which only reaches here when both of those came up empty).

        The one case that still isn't safe to create: pairing has no memory
        across a crash, so a check-out punch that already closed a shift in
        Odoo, but whose "synced" state locally was lost before it committed,
        comes back on retry looking exactly like a lone, unrelated check-in
        — every pairing mode reads it that way once there is no open shift
        left to attach it to. Left alone, that opens a phantom record next
        to the real, already-correctly-closed one.

        The tell, checked first and entirely locally (no Odoo call, so an
        ordinary create pays nothing extra for this): the punch pairing
        used as this interval's check-in was itself recorded with direction
        "out". A real shift's opening punch is never that — either "in", or
        "unknown" on a device that does not report state at all, which is
        deliberately left alone here rather than risk a false positive and
        silently drop what might be a genuine new shift.

        Only then is Odoo asked whether some record already carries this
        exact check-out. A match means the punch already did its job; this
        rewrites ``interval`` in place to the real shift's actual times
        (Odoo's, not the misread ones) so everything downstream — the local
        mirror, lateness scoring, the open-shift carried into next cycle —
        reflects the real shift rather than the phantom reading, and
        returns that record's id so the caller writes nothing further.
        """
        check_in_punch = by_id.get(interval.check_in_punch_id or "")
        if check_in_punch is None or check_in_punch.direction != Direction.outward.value:
            return None

        real = odoo.attendance_closed_at(employee_id, interval.check_in)
        if real is None:
            return None

        real_check_out = interval.check_in
        interval.check_in = parse_dt(real.get("check_in")) or real_check_out
        interval.check_out = real_check_out
        return real["id"]

    def _current_open_shift(self, odoo: OdooClient, mapping: EmployeeMapping) -> OpenShift | None:
        """Reconcile our stored open shift against Odoo's actual state.

        Odoo is authoritative: somebody may have closed the record by hand.
        """
        live = odoo.get_open_attendance(mapping.odoo_employee_id)
        if not live:
            mapping.open_attendance_id = None
            mapping.open_check_in = None
            return None

        check_in = parse_dt(live["check_in"])
        if check_in is None:
            return None
        mapping.open_attendance_id = live["id"]
        mapping.open_check_in = check_in
        return OpenShift(attendance_id=live["id"], check_in=check_in)

    def _mode_for(self, punches: list[PunchRecord]) -> str | None:
        """Per-device pairing override, when every punch came from one terminal.

        Mixing modes inside one employee's stream would produce incoherent
        intervals, so the override applies only when the batch is unambiguous.
        """
        device_ids = {p.device_id for p in punches if p.device_id}
        if len(device_ids) != 1:
            return None
        device = self.db.get(Device, device_ids.pop())
        return device.pairing_override if device else None

    @staticmethod
    def _ref(interval, by_id: dict[str, PunchRecord]) -> str:
        punch = by_id.get(interval.check_in_punch_id or "")
        return f"biotime:{punch.external_id}" if punch else "biotime:derived"

    def _terminal_for(self, interval, by_id: dict[str, PunchRecord]) -> Device | None:
        """Which terminal recorded this interval's check-in, if known.

        Falls back to the check-out punch's terminal for a record created
        while closing a shift, where the check-in punch may not be in this
        batch's ``by_id`` at all.
        """
        punch = by_id.get(interval.check_in_punch_id or "") or by_id.get(
            interval.check_out_punch_id or ""
        )
        if punch is None:
            return None
        # Falls back to the punch's serial number when it wasn't linked at
        # fetch time (its terminal was imported later) — see device_links.
        return device_for_punch(self.db, punch)

    def _sync_odoo_devices(self, odoo: OdooClient, odoo_conn: OdooConnection) -> None:
        """Keep every terminal's Odoo device (and its company) current each
        cycle, not only when a punch happens to be pushed — so a company set
        in BioBridge, or a device registered before it was, reaches Odoo."""
        if not odoo_conn.has_device_tracking:
            self._auto_enable_device_tracking(odoo, odoo_conn)
        if not odoo_conn.has_device_tracking:
            self._log(
                "Odoo device tracking is off for this connection, so terminals and their "
                "companies are not written to Odoo. Run Test Connection to re-detect it.",
                "warning",
                ui=False,
            )
            return
        for device in self.db.scalars(
            select(Device).where(Device.tenant_id == self.tenant.id)
        ):
            self._odoo_device_id(odoo, odoo_conn, device)
        removed, errors = device_removals.work_off(self.db, self.tenant.id, odoo)
        if removed:
            self._log(f"Removed {removed} terminal(s) of a deleted connection from Odoo")
        if errors:
            self._log(
                f"{len(errors)} terminal(s) of a deleted connection could not be removed from "
                f"Odoo yet; retrying next sync. {errors[0]}", "warning", ui=False,
            )
        try:
            if odoo.ensure_device_company_rule():
                self._log("Odoo device company rule is in place", ui=False)
            for line in odoo.check_device_isolation():
                # Server log only: a diagnostic, not something for the run log
                # customers read.
                log.info("[tenant=%s] Isolation check: %s", self.tenant.slug, line)
        except Exception as exc:  # noqa: BLE001 — a diagnostic must never fail a sync
            self._log(
                f"Could not set up the Odoo device company rule: {exc}. The connected "
                "user needs Settings access, or run Update Setup on the connection.",
                "warning",
                ui=False,
            )

    def _auto_enable_device_tracking(self, odoo: OdooClient, odoo_conn: OdooConnection) -> None:
        """Create the device model in Odoo when this connection has none yet.

        A fresh Odoo has neither the add-on nor the bootstrap model, and until
        now the model was only created by clicking Test Connection, so a sync
        on a new instance silently wrote no devices and no companies. Best
        effort: needs Settings access on the Odoo user, and never fails a sync.
        """
        try:
            odoo.ensure_device_tracking_bootstrap()
            mode = odoo._device_tracking_mode()
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"Could not create the device model in Odoo: {exc}. The connected Odoo "
                "user needs Settings (Administrator) access.",
                "warning",
                ui=False,
            )
            return
        if mode is None:
            return
        odoo_conn.has_device_tracking = True
        odoo_conn.device_tracking_mode = mode
        self.db.flush()
        self._log(f"Created Odoo device tracking ({mode}) for this connection", ui=False)

    def _terminal_company_id(self, device: Device) -> int | None:
        """The Odoo company this terminal belongs to: its own, else its
        connection's default."""
        if device.company_id is not None:
            return device.company_id
        source = self._source_cache.get(device.source_id)
        if source is None and device.source_id:
            source = self.db.get(DeviceSource, device.source_id)
            self._source_cache[device.source_id] = source
        return source.default_company_id if source is not None else None

    def _odoo_device_id(
        self, odoo: OdooClient, odoo_conn: OdooConnection, device: Device | None
    ) -> int | None:
        """Upsert this terminal into Odoo's optional biobridge.device model.

        Returns None — never raises — when the companion add-on isn't
        installed, when the punch can't be attributed to a terminal, or when
        the upsert itself fails: a device link is a nice-to-have on top of the
        attendance record, not a reason to fail the whole push. Cached per
        device for the run, so a terminal with fifty punches this cycle is
        upserted once, not fifty times.
        """
        if device is None or not odoo_conn.has_device_tracking:
            return None
        if device.id in self._odoo_device_cache:
            return self._odoo_device_cache[device.id]
        if not terminal_may_register_in_odoo(self.db, device):
            self._log(
                f"Terminal {device.serial_number} not written to Odoo: none of its "
                "employees is mapped yet", ui=False,
            )
            return None
        try:
            terminal_company = self._terminal_company_id(device)
            odoo_id = odoo.upsert_device(
                device.serial_number,
                name=device.alias,
                location=device.area,
                terminal_model=device.model,
                ip_address=device.ip_address,
                company_id=terminal_company,
            )
            self._log(
                f"Odoo device {device.serial_number}: id {odoo_id}, company "
                f"{terminal_company if terminal_company is not None else 'not set in BioBridge'}",
                ui=False,
            )
        except OdooError as exc:
            # has_device_tracking was true as of the last connection test, but
            # the add-on may have been removed, or this Odoo user may lack
            # access to it, since then. Degrade rather than error the push.
            self._log(
                f"Could not register device {device.serial_number} in Odoo: {exc}",
                "warning",
                ui=False,
            )
            odoo_id = None
        self._odoo_device_cache[device.id] = odoo_id
        return odoo_id

    def _mark(self, by_id: dict[str, PunchRecord], interval, attendance_id: int) -> None:
        for punch_id in (interval.check_in_punch_id, interval.check_out_punch_id):
            punch = by_id.get(punch_id or "")
            if punch is not None:
                punch.process_state = PunchState.synced.value
                punch.odoo_attendance_id = attendance_id
                punch.error_message = None

    def _record_interval(
        self,
        mapping: EmployeeMapping,
        interval,
        attendance_id: int,
        by_id: dict,
        pairing_mode: str | None = None,
    ) -> None:
        """Mirror the interval locally, so reports never call Odoo.

        Keyed on the Odoo attendance id, so re-processing the same shift updates
        the row rather than duplicating it.
        """
        tz = self.tenant.timezone
        check_in_local = utc_to_local(interval.check_in, tz)
        check_out_local = utc_to_local(interval.check_out, tz) if interval.check_out else None
        punch = by_id.get(interval.check_in_punch_id or "")
        shift_date = check_in_local.strftime("%Y-%m-%d")

        existing = self.db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.tenant_id == self.tenant.id,
                AttendanceRecord.odoo_attendance_id == attendance_id,
            )
        ).first()

        values = {
            "emp_code": mapping.emp_code,
            "employee_name": mapping.odoo_employee_name or mapping.source_name,
            "department": mapping.department,
            "odoo_employee_id": mapping.odoo_employee_id,
            "check_in": interval.check_in,
            "check_out": interval.check_out,
            "worked_hours": interval.duration_hours,
            "shift_date": shift_date,
            "check_in_local": check_in_local,
            "check_out_local": check_out_local,
            "device_serial": punch.terminal_sn if punch else None,
            "pairing_mode": pairing_mode or self.tenant.pairing_mode,
            "is_auto_closed": interval.auto_closed,
            "is_orphan_out": interval.orphan_out,
            "notes": "; ".join(interval.notes) or None,
        }

        if existing is not None:
            for key, value in values.items():
                setattr(existing, key, value)
        else:
            self.db.add(
                AttendanceRecord(
                    tenant_id=self.tenant.id, odoo_attendance_id=attendance_id, **values
                )
            )

        # The session runs with autoflush off, so flush now: the next interval
        # handled in this run must be able to see this record.
        self.db.flush()

    # -- helpers -----------------------------------------------------------
    def _active_odoo_connection(self) -> OdooConnection | None:
        return self.db.scalars(
            select(OdooConnection)
            .where(
                OdooConnection.tenant_id == self.tenant.id,
                OdooConnection.is_active.is_(True),
            )
            # The one set in OdooBridger wins in platform mode.
            .order_by(OdooConnection.platform_connection_id.is_(None))
            .limit(1)
        ).first()

    def _active_sources(self) -> list[DeviceSource]:
        stmt = select(DeviceSource).where(
            DeviceSource.tenant_id == self.tenant.id,
            DeviceSource.is_active.is_(True),
        )
        if self.only_source_id:
            stmt = stmt.where(DeviceSource.id == self.only_source_id)
        return list(self.db.scalars(stmt).all())

    def _mark_degraded_if_needed(self) -> None:
        if self.only_source_id:
            return  # see __init__: a one-connection sync never degrades the rest
        if self.tenant.consecutive_failures < settings.max_consecutive_failures:
            return
        for source in self._active_sources():
            source.status = ConnectionStatus.degraded.value
        conn = self._active_odoo_connection()
        if conn:
            conn.status = ConnectionStatus.degraded.value
        self._log(
            f"{self.tenant.consecutive_failures} consecutive failures — connections "
            "marked degraded and polling moved to the slow lane",
            "error",
        )


def _reload_devices(db: Session, source: DeviceSource, devices: dict) -> None:
    devices.clear()
    devices.update({d.serial_number: d for d in db.scalars(select(Device).where(Device.source_id == source.id))})


def insert_punches(db: Session, items: list, store, *, refresh_devices=None) -> int:
    """Store ``items`` with ``store(event, punch_utc)``; return how many were new.

    In one savepoint first. If another process stored some of these punches in
    the meantime (an ADMS terminal re-sending a batch, two cycles' overlapping
    reads), the savepoint is rolled back and the punches go in one by one,
    each in its own savepoint, skipping the ones already there — so a
    duplicate costs a retry, never the whole batch or the sync."""
    if not items:
        return 0
    try:
        with db.begin_nested():
            for event, punch_utc in items:
                store(event, punch_utc)
        return len(items)
    except IntegrityError:
        if refresh_devices:
            refresh_devices()
    created = 0
    for event, punch_utc in items:
        try:
            with db.begin_nested():
                store(event, punch_utc)
            created += 1
        except IntegrityError:
            if refresh_devices:
                refresh_devices()
    return created


def store_punch(
    db: Session, tenant: Tenant, source: DeviceSource, devices: dict,
    event, punch_utc: datetime, *, run_id: str | None = None, register_unknown: bool = True,
) -> PunchRecord:
    """Write one new punch to the ledger, pending.

    Shared by the sync engine (pulled punches) and the ADMS receiver (pushed
    ones), so a punch is stored the same way whichever direction it came from.
    ``devices`` is serial -> Device for this source; a terminal not in it is
    recorded on the spot, unless ``register_unknown`` is off (the source
    publishes its device list and doesn't list it: the punch is kept, without
    a terminal). The caller has already checked ``external_id`` is new.
    """
    direction = Direction.unknown.value
    if event.direction is True:
        direction = Direction.inward.value
    elif event.direction is False:
        direction = Direction.outward.value

    device = devices.get(event.terminal_sn or "")
    if device is None and event.terminal_sn and register_unknown:
        # First punch from a terminal nobody imported: record it, so it
        # shows on the Terminals page, reaches Odoo's device list, and
        # counts toward the plan's device allowance like any other.
        from app.services.device_links import device_for_serial

        device, _ = device_for_serial(db, tenant.id, source.id, event.terminal_sn,
                                      alias=getattr(event, "terminal_alias", None) or None)
        devices[event.terminal_sn] = device
    record = PunchRecord(
        tenant_id=tenant.id,
        source_id=source.id,
        device_id=device.id if device else None,
        # Stamp the run, so "what did this sync bring in" is answerable
        # later from the ledger rather than only from a counter. Pushed
        # punches arrive between runs and are stamped by the next one.
        first_seen_run_id=run_id,
        external_id=event.external_id,
        emp_code=event.emp_code,
        punch_time_utc=punch_utc,
        punch_time_local=event.punch_time_local,
        direction=direction,
        raw_state=str(event.raw.get("punch_state", "") or "")[:8] or None,
        verify_type=(event.verify_type or None) and str(event.verify_type)[:8],
        terminal_sn=event.terminal_sn,
        process_state=PunchState.pending.value,
        raw=event.raw,
    )
    db.add(record)
    if device is not None:
        device.punch_count = (device.punch_count or 0) + 1
        device.last_seen_at = datetime.now(timezone.utc)
        # A punch is live proof the terminal is there, regardless of
        # whether "Import terminals" has been re-run since it was last
        # flagged missing.
        device.missing_since = None
    return record


def stale_clock() -> datetime:
    """"Now", for deciding a shift has been open too long. Its own function so
    tests replaying yesterday's punches as if live can stand still."""
    return utcnow_naive()


def close_stale_attendances(db: Session, tenant: Tenant) -> int:
    """Close shifts left open past max_shift_hours, between syncs.

    Every sync does this too (``SyncEngine.close_stale_shifts``); this sweep
    covers an account whose syncs are off or failing. Without it, one employee
    who forgets to badge out holds an ever-growing open record that blocks
    every later check-in for that person.
    """
    engine = SyncEngine(db, tenant, "maintenance")
    conn = engine._active_odoo_connection()  # noqa: SLF001
    if conn is None:
        return 0
    lock = TenantRunLock(tenant.id)
    if not lock.acquire():
        return 0      # a sync is running for this account, and it closes them itself
    try:
        closed = engine.close_stale_shifts(build_odoo_client(tenant, conn))
        db.commit()
        return closed
    except OdooError as exc:
        db.rollback()
        log.warning("Stale-close failed for %s: %s", tenant.slug, exc)
        return 0
    finally:
        lock.release()

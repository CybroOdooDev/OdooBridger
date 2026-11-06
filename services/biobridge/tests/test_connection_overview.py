"""The dashboard's Sync & connections card: what it can honestly say about each connection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import AdmsDevice, Device, DeviceSource, OdooConnection, PunchRecord, Tenant
from app.services.alerts import TERMINAL_SILENT_MINUTES, tenant_alerts
from app.services.connection_overview import MAX_TERMINALS, connection_overview
from tests.test_punch_ledger import api  # noqa: F401 — a seeded, signed-in tenant

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _source(db, tenant) -> DeviceSource:
    return db.scalars(select(DeviceSource).where(DeviceSource.tenant_id == tenant.id)).first()


def _push(db, tenant, serial: str, *, minutes_ago: int | None, alias: str | None = None) -> Device:
    """A push (ADMS) terminal: a Device row and the heartbeat row the platform keeps for it."""
    source = _source(db, tenant)
    device = Device(tenant_id=tenant.id, source_id=source.id, serial_number=serial, alias=alias, is_enabled=True)
    db.add(device)
    db.add(AdmsDevice(serial_number=serial, tenant_id=tenant.id, source_id=source.id,
                      last_seen_at=NOW - timedelta(minutes=minutes_ago) if minutes_ago is not None else None))
    db.commit()
    return device


def test_a_push_terminal_is_online_offline_or_waiting_by_its_heartbeat(db, tenant):
    _push(db, tenant, "P-OK", minutes_ago=2, alias="Reception")
    _push(db, tenant, "P-OLD", minutes_ago=TERMINAL_SILENT_MINUTES + 5, alias="Gate 2")
    _push(db, tenant, "P-NEW", minutes_ago=None, alias="Warehouse")
    by_name = {t["name"]: t for t in connection_overview(db, tenant, now=NOW)["terminals"]}
    assert by_name["Reception"]["state"] == "online" and by_name["Reception"]["kind"] == "push"
    assert by_name["Gate 2"]["state"] == "offline"
    assert by_name["Warehouse"]["state"] == "waiting" and by_name["Warehouse"]["last_contact_at"] is None


def test_a_platform_terminal_is_never_called_offline_for_being_quiet(db, tenant):
    """GATE-01 comes from the fixture's BioTime platform: no heartbeat, last punch days ago."""
    device = db.scalars(select(Device).where(Device.serial_number == "GATE-01")).one()
    device.last_seen_at = NOW - timedelta(days=3)
    db.commit()
    out = connection_overview(db, tenant, now=NOW)
    (row,) = out["terminals"]
    assert row["kind"] == "platform" and row["state"] == "reporting"
    assert row["last_punch_at"] == NOW - timedelta(days=3)


def test_a_terminal_its_platform_stopped_listing_is_missing(db, tenant):
    device = db.scalars(select(Device).where(Device.serial_number == "GATE-01")).one()
    device.missing_since = NOW - timedelta(hours=2)
    db.commit()
    (row,) = connection_overview(db, tenant, now=NOW)["terminals"]
    assert row["state"] == "missing"


def test_problems_sort_first_and_counts_cover_every_terminal(db, tenant):
    _push(db, tenant, "A", minutes_ago=1, alias="A online")
    _push(db, tenant, "B", minutes_ago=999, alias="B offline")
    out = connection_overview(db, tenant, now=NOW)
    assert [t["name"] for t in out["terminals"]][0] == "B offline"
    assert out["terminal_counts"] == {"total": 3, "online": 1, "reporting": 1, "offline": 1,
                                      "missing": 0, "waiting": 0, "unreachable": 0, "unchecked": 0}


def test_a_switched_off_terminal_is_left_out(db, tenant):
    device = db.scalars(select(Device).where(Device.serial_number == "GATE-01")).one()
    device.is_enabled = False
    db.commit()
    out = connection_overview(db, tenant, now=NOW)
    assert out["terminals"] == [] and out["terminal_counts"]["total"] == 0


def test_the_list_is_capped_but_the_counts_are_not(db, tenant):
    for i in range(MAX_TERMINALS + 5):
        _push(db, tenant, f"T{i:02d}", minutes_ago=1)
    out = connection_overview(db, tenant, now=NOW)
    assert len(out["terminals"]) == MAX_TERMINALS
    assert out["terminal_counts"]["total"] == MAX_TERMINALS + 6


def test_it_agrees_with_the_alert_bell_about_who_is_offline(db, tenant):
    _push(db, tenant, "P-OLD", minutes_ago=TERMINAL_SILENT_MINUTES + 5, alias="Gate 2")
    offline = [t for t in connection_overview(db, tenant, now=NOW)["terminals"] if t["state"] == "offline"]
    bell = [a for a in tenant_alerts(db, tenant, now=NOW) if a["key"].startswith("terminal-offline-")]
    assert len(offline) == len(bell) == 1


def test_odoo_shows_where_it_points_and_when_it_was_checked(db, tenant):
    conn = db.scalars(select(OdooConnection).where(OdooConnection.tenant_id == tenant.id)).one()
    conn.last_checked_at = NOW
    conn.status, conn.status_message = "failed", "Odoo rejected the API key."
    db.commit()
    odoo = connection_overview(db, tenant, now=NOW)["odoo"]
    assert odoo["host"] == "acme.odoo.com"          # no scheme, no path
    assert odoo["status"] == "failed" and odoo["message"] == "Odoo rejected the API key."
    assert odoo["last_checked_at"] == NOW and odoo["managed"] is False


def test_an_active_odoo_connection_wins_over_a_switched_off_one(db, tenant):
    old = db.scalars(select(OdooConnection).where(OdooConnection.tenant_id == tenant.id)).one()
    old.is_active = False
    db.add(OdooConnection(tenant_id=tenant.id, name="New", url="https://new.odoo.com", db_name="n",
                          username="u", is_active=True))
    db.commit()
    assert connection_overview(db, tenant, now=NOW)["odoo"]["host"] == "new.odoo.com"


def test_no_odoo_and_no_punches_are_simply_empty(db):
    bare = Tenant(name="Bare", slug="bare", status="active")
    db.add(bare)
    db.commit()
    out = connection_overview(db, bare, now=NOW)
    assert out["odoo"] is None and out["terminals"] == [] and out["last_punch_at"] is None


def test_the_last_punch_is_the_newest_one(db, tenant):
    for minutes, sid in ((90, "a"), (7, "b")):
        db.add(PunchRecord(tenant_id=tenant.id, source_id=_source(db, tenant).id, external_id=sid, emp_code="1",
                           punch_time_utc=(NOW - timedelta(minutes=minutes)).replace(tzinfo=None),
                           punch_time_local=(NOW - timedelta(minutes=minutes)).replace(tzinfo=None),
                           direction="in", process_state="synced"))
    db.commit()
    assert connection_overview(db, tenant, now=NOW)["last_punch_at"] == (NOW - timedelta(minutes=7)).replace(tzinfo=None)


def test_the_dashboard_endpoint_carries_the_connection_detail(api):  # noqa: F811
    body = api.get("/api/v1/dashboard").json()
    conns = body["connections"]
    assert set(conns) == {"odoo", "terminals", "terminal_counts", "last_punch_at"}
    assert conns["terminal_counts"]["total"] == len(conns["terminals"])
    # The old fields the page already reads are untouched.
    assert set(body["connection_health"]) == {"odoo", "source"} and "schedule" in body


def test_a_platform_terminal_with_a_heartbeat_is_judged_by_it_not_by_its_punches(db, tenant):
    """A quiet door is not a dead one: online on a fresh heartbeat whatever its last punch,
    offline on a stale one even if it punched a minute before — and the bell agrees."""
    device = db.scalars(select(Device).where(Device.serial_number == "GATE-01")).one()
    device.last_seen_at = NOW - timedelta(days=3)           # nobody has used it for days
    device.last_contact_at = NOW - timedelta(minutes=1)     # but the terminal is talking to its platform
    db.commit()
    (row,) = connection_overview(db, tenant, now=NOW)["terminals"]
    assert row["state"] == "online" and row["last_contact_at"] == NOW - timedelta(minutes=1)
    assert row["last_punch_at"] == NOW - timedelta(days=3)

    device.last_seen_at = NOW - timedelta(minutes=1)        # punched a minute ago, yet silent to the platform
    device.last_contact_at = NOW - timedelta(minutes=TERMINAL_SILENT_MINUTES + 5)
    db.commit()
    offline = [t for t in connection_overview(db, tenant, now=NOW)["terminals"] if t["state"] == "offline"]
    bell = [a for a in tenant_alerts(db, tenant, now=NOW) if a["key"].startswith("terminal-offline-")]
    assert len(offline) == len(bell) == 1


def test_a_push_terminal_still_shows_its_last_punch_beside_its_heartbeat(db, tenant):
    device = _push(db, tenant, "P-1", minutes_ago=1, alias="Front")
    device.last_seen_at = NOW - timedelta(hours=5)
    db.commit()
    row = next(t for t in connection_overview(db, tenant, now=NOW)["terminals"] if t["name"] == "Front")
    assert row["state"] == "online" and row["last_punch_at"] == NOW - timedelta(hours=5)


def test_biotime_reads_a_terminals_last_activity_in_the_servers_timezone():
    from app.integrations.providers.biotime import BioTimeProvider
    provider = BioTimeProvider.__new__(BioTimeProvider)
    provider.config = type("C", (), {"timezone": "Asia/Kolkata"})()
    # No offset: the server's own clock (IST = UTC+5:30).
    assert provider._contact_time("2026-11-06 17:30:00") == datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    # An explicit offset wins; junk and blanks mean "not told", never a guess.
    assert provider._contact_time("2026-11-06T12:00:00+00:00") == datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    assert provider._contact_time("") is None and provider._contact_time("never") is None and provider._contact_time(None) is None


def _standalone(db, tenant, *, status: str, checked_minutes_ago: int | None = 3, with_device: bool) -> DeviceSource:
    source = DeviceSource(tenant_id=tenant.id, name="Front Door", provider="zkteco", connection_kind="device",
                          base_url="zk://10.0.0.9:4370", username="", is_active=True, status=status,
                          last_checked_at=NOW - timedelta(minutes=checked_minutes_ago)
                          if checked_minutes_ago is not None else None)
    db.add(source)
    db.flush()
    if with_device:
        db.add(Device(tenant_id=tenant.id, source_id=source.id, serial_number="ZK-1", alias="Front Door",
                      is_enabled=True))
    db.commit()
    return source


def test_a_standalone_device_is_judged_by_its_last_check(db, tenant):
    _standalone(db, tenant, status="connected", with_device=True)
    row = next(t for t in connection_overview(db, tenant, now=NOW)["terminals"] if t["name"] == "Front Door")
    assert row["kind"] == "device" and row["state"] == "online" and row["registered"] is True
    assert row["last_contact_at"] == NOW - timedelta(minutes=3)


def test_a_standalone_device_with_no_terminal_record_still_appears(db, tenant):
    source = _standalone(db, tenant, status="connected", with_device=False)
    out = connection_overview(db, tenant, now=NOW)
    row = next(t for t in out["terminals"] if t["name"] == "Front Door")
    assert row["id"] == source.id and row["registered"] is False and row["state"] == "online"
    assert out["terminal_counts"]["total"] == 2  # GATE-01 from the platform, plus this one


def test_an_unreachable_standalone_device_is_flagged_not_assumed_fine(db, tenant):
    _standalone(db, tenant, status="failed", with_device=False)
    out = connection_overview(db, tenant, now=NOW)
    row = next(t for t in out["terminals"] if t["name"] == "Front Door")
    assert row["state"] == "unreachable" and row["last_contact_at"] is None
    assert out["terminal_counts"]["unreachable"] == 1
    assert out["terminals"][0]["name"] == "Front Door"  # worst first


def test_a_standalone_device_not_checked_yet_is_unchecked(db, tenant):
    _standalone(db, tenant, status="unverified", checked_minutes_ago=None, with_device=False)
    row = next(t for t in connection_overview(db, tenant, now=NOW)["terminals"] if t["name"] == "Front Door")
    assert row["state"] == "unchecked"

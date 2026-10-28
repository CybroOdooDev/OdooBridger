"""A server that publishes its device list (BioTime, BioStar 2) decides which
terminals exist. Its old transactions of terminals deleted there must not
bring those terminals back into BioBridge (a real report: BioTime listed 2
terminals, BioBridge showed 9)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.integrations.base import TerminalRecord
from app.models import Device, DeviceSource, OdooDeviceRemoval, PunchRecord, PunchState
from app.services import sync_engine as engine_mod
from app.services.device_links import sync_inventory
from tests.conftest import FakeOdoo, FakeProvider


class ServerProvider(FakeProvider):
    server_inventory = True

    def __init__(self, rows, terminals):
        super().__init__(rows)
        self.terminals = terminals

    def fetch_terminals(self):
        for serial in self.terminals:
            yield TerminalRecord(serial_number=serial, alias=f"Gate {serial}")


def punch(pid, emp, moment, terminal):
    return {"id": pid, "emp_code": emp, "punch_time": moment.strftime("%Y-%m-%d %H:%M:%S"),
            "terminal_sn": terminal, "first_name": "X", "last_name": "Y"}


def run(db, tenant, rows, terminals, monkeypatch, odoo=None):
    provider = ServerProvider(rows, terminals)
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo or FakeOdoo({"1001": (7, "Jane")}))
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: provider)
    return engine_mod.SyncEngine(db, tenant, "test").run_cycle()


def serials(db):
    return {d.serial_number for d in db.scalars(select(Device))}


def test_punches_from_a_terminal_the_server_does_not_list_add_no_terminal(db, tenant, local_day, monkeypatch):
    at = local_day + timedelta(hours=9)
    rows = [punch(1, "1001", at, "GATE-01"), punch(2, "2002", at, "OLD-AUTO-ADD"),
            punch(3, "2003", at + timedelta(minutes=5), "OLD-BIFACE")]
    run(db, tenant, rows, ["GATE-01", "GATE-02"], monkeypatch)
    assert serials(db) == {"GATE-01", "GATE-02"}
    # The punches themselves are kept, just without a terminal.
    old = db.scalars(select(PunchRecord).where(PunchRecord.terminal_sn == "OLD-AUTO-ADD")).one()
    assert old.device_id is None
    # ...and a later sync doesn't restore the terminal from them.
    run(db, tenant, [], ["GATE-01", "GATE-02"], monkeypatch)
    assert serials(db) == {"GATE-01", "GATE-02"}


def test_terminals_added_earlier_without_history_are_removed(db, tenant, local_day, monkeypatch):
    source = db.scalars(select(DeviceSource)).one()
    db.add(Device(tenant_id=tenant.id, source_id=source.id, serial_number="OLD-AUTO-ADD"))
    db.commit()
    run(db, tenant, [], ["GATE-01"], monkeypatch)
    assert serials(db) == {"GATE-01"}
    # It may have reached Odoo's device list: its removal there is queued.
    assert [r.serial_number for r in db.scalars(select(OdooDeviceRemoval))] == ["OLD-AUTO-ADD"]


def test_a_terminal_with_real_history_is_kept_and_flagged_missing(db, tenant, local_day, monkeypatch):
    at = local_day + timedelta(hours=9)
    run(db, tenant, [punch(1, "1001", at, "GATE-02")], ["GATE-01", "GATE-02"], monkeypatch)
    gate2 = db.scalars(select(Device).where(Device.serial_number == "GATE-02")).one()
    assert db.scalars(select(PunchRecord)).one().process_state != PunchState.unmapped.value
    run(db, tenant, [], ["GATE-01"], monkeypatch)
    db.refresh(gate2)
    assert gate2.missing_since is not None


def test_an_empty_or_unreadable_list_changes_nothing(db, tenant, local_day, monkeypatch):
    source = db.scalars(select(DeviceSource)).one()
    db.add(Device(tenant_id=tenant.id, source_id=source.id, serial_number="GATE-09"))
    db.commit()
    run(db, tenant, [], [], monkeypatch)
    assert serials(db) == {"GATE-01", "GATE-09"}

    class Broken(ServerProvider):
        def fetch_terminals(self):
            raise RuntimeError("no permission")

    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: Broken([], []))
    run_ = engine_mod.SyncEngine(db, tenant, "test").run_cycle()
    assert run_.status != "failed" and serials(db) == {"GATE-01", "GATE-09"}


def test_sync_inventory_refreshes_listed_terminals(db, tenant):
    source = db.scalars(select(DeviceSource)).one()
    out = sync_inventory(db, tenant, source, [TerminalRecord(serial_number="GATE-01", alias="Front"),
                                              TerminalRecord(serial_number="GATE-03")])
    assert (out["added"], out["removed"], out["serials"]) == (1, 0, {"GATE-01", "GATE-03"})
    assert db.scalars(select(Device).where(Device.serial_number == "GATE-01")).one().alias == "Front"


def test_a_second_cycle_while_one_runs_returns_the_running_one(db, tenant, local_day, monkeypatch):
    from datetime import datetime, timezone

    from app.models import SyncRun

    busy = SyncRun(tenant_id=tenant.id, status="running", triggered_by="schedule",
                   started_at=datetime.now(timezone.utc), log=[])
    db.add(busy)
    db.commit()
    got = run(db, tenant, [], ["GATE-01"], monkeypatch)
    assert got.id == busy.id
    assert db.query(SyncRun).count() == 1
    # A run left "running" by a process that died long ago doesn't block.
    busy.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.commit()
    assert run(db, tenant, [], ["GATE-01"], monkeypatch).id != busy.id

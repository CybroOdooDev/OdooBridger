"""The device side failing must not stop the Odoo side.

Punches already in the ledger still get pushed, and a shift left open past the
limit still gets closed, on every sync, even while the biometric platform is down.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.integrations.base import ProviderError
from app.models import PunchRecord
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo, FakeProvider
from tests.test_sync_engine import punch, run


class DownProvider(FakeProvider):
    def fetch_punches(self, since=None, until=None):
        raise ProviderError("Cannot reach the biometric server: connection timed out")


def run_down(db, tenant, odoo, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: DownProvider([]))
    return engine_mod.SyncEngine(db, tenant, "test").run_cycle()


def test_open_shift_is_closed_at_the_limit_while_the_device_is_down(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    monkeypatch.setattr(engine_mod, "stale_clock", lambda: datetime(2000, 1, 1))  # leave it open for now
    run(db, tenant, odoo, [punch("in", "1001", local_day.replace(hour=1), direction=True)], monkeypatch)
    (att_id,) = odoo.attendances
    assert odoo.attendances[att_id]["check_out"] is None

    # Now the device goes down and the shift passes the limit.
    check_in = odoo.attendances[att_id]["check_in"]
    later = check_in + timedelta(hours=tenant.max_shift_hours, minutes=5)
    monkeypatch.setattr(engine_mod, "stale_clock", lambda: later)
    result = run_down(db, tenant, odoo, monkeypatch)

    assert result.status == "failed"
    assert "Cannot reach the biometric server" in result.error_message
    assert odoo.attendances[att_id]["check_out"] == check_in + timedelta(hours=tenant.max_shift_hours)
    assert result.attendances_closed == 1


def test_pending_punches_are_pushed_while_the_device_is_down(db, tenant, local_day, monkeypatch):
    from tests.test_odoo_outage_recovery import FlakyOdoo

    odoo = FlakyOdoo()
    odoo.down = True
    run(db, tenant, odoo, [punch(1, "1001", local_day.replace(hour=8))], monkeypatch)
    assert {p.process_state for p in db.scalars(select(PunchRecord))} == {"pending"}

    odoo.down = False
    result = run_down(db, tenant, odoo, monkeypatch)
    assert result.status == "failed"
    assert {p.process_state for p in db.scalars(select(PunchRecord))} == {"synced"}
    assert len(odoo.attendances) == 1


def test_still_counts_as_a_failure_streak_for_the_device_side(db, tenant, monkeypatch):
    before = tenant.consecutive_failures
    run_down(db, tenant, FakeOdoo(), monkeypatch)
    assert tenant.consecutive_failures == before + 1

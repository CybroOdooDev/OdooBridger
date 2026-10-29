"""A check-in that falls inside a record Odoo already has.

Odoo's hr.attendance refuses overlapping records, closed ones included — the
error ("the employee was already checked in on …") even prints the *new*
check-in. Retrying such a write can never work, so the engine must settle it:
shorten a record that BioBridge itself closed at the shift limit (a guess), or
mark the punch covered when a real record already spans it. FakeOdoo enforces
the same overlap rule and raises if the engine ever tries to write one.
"""
from __future__ import annotations

import pytest

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import AttendanceRecord, PunchRecord, PunchState
from tests.conftest import FakeOdoo
from tests.test_odoo_close_crash_recovery import punch, run


def _first_open(db, tenant, odoo, local_day, monkeypatch):
    rows = [punch("in1", "1001", local_day.replace(hour=8), direction=True)]
    run(db, tenant, odoo, rows, monkeypatch)
    (att_id,) = odoo.attendances.keys()
    return att_id, rows


def _state(db, external_id):
    return db.scalars(select(PunchRecord).where(PunchRecord.external_id == external_id)).one()


@pytest.mark.usefixtures("no_stale_close")
def test_checkin_inside_a_record_closed_at_the_limit_shortens_it(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    att_id, rows = _first_open(db, tenant, odoo, local_day, monkeypatch)
    rec = odoo.attendances[att_id]
    # What the stale-shift sweep does to a shift nobody closed.
    rec["check_out"] = rec["check_in"] + timedelta(hours=16)
    rec["auto_closed"] = True

    rows.append(punch("in2", "1001", local_day.replace(hour=10), direction=True))
    run(db, tenant, odoo, rows, monkeypatch)

    second = _state(db, "in2")
    assert second.process_state == PunchState.synced.value, second.error_message
    assert len(odoo.attendances) == 2
    new_id = second.odoo_attendance_id
    assert new_id != att_id
    # The guess is cut back to where the real punch begins; the new one is open.
    assert odoo.attendances[att_id]["check_out"] == odoo.attendances[new_id]["check_in"]
    assert odoo.attendances[new_id]["check_out"] is None


def test_the_shortened_record_is_mirrored_locally(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    att_id, rows = _first_open(db, tenant, odoo, local_day, monkeypatch)
    rec = odoo.attendances[att_id]
    rec["check_out"] = rec["check_in"] + timedelta(hours=16)
    rows.append(punch("in2", "1001", local_day.replace(hour=10), direction=True))
    run(db, tenant, odoo, rows, monkeypatch)

    mirror = db.scalars(
        select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == att_id)
    ).one()
    assert mirror.check_out == odoo.attendances[att_id]["check_out"]
    assert mirror.is_auto_closed is True
    assert abs(mirror.worked_hours - 2.0) < 0.01


@pytest.mark.usefixtures("no_stale_close")
def test_checkin_inside_a_real_record_is_settled_not_retried(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    att_id, rows = _first_open(db, tenant, odoo, local_day, monkeypatch)
    rec = odoo.attendances[att_id]
    rec["check_out"] = rec["check_in"] + timedelta(hours=3)      # a real, punched shift

    rows.append(punch("in2", "1001", local_day.replace(hour=9), direction=True))
    result = run(db, tenant, odoo, rows, monkeypatch)

    second = _state(db, "in2")
    assert second.process_state == PunchState.skipped.value
    assert str(att_id) in second.error_message and "overlapping" in second.error_message
    assert len(odoo.attendances) == 1                              # nothing written
    assert second.attempts == 0                                    # and not a failure to retry
    assert result.error_count == 0


def test_a_later_checkin_outside_any_record_is_unaffected(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    att_id, rows = _first_open(db, tenant, odoo, local_day, monkeypatch)
    odoo.attendances[att_id]["check_out"] = odoo.attendances[att_id]["check_in"] + timedelta(hours=2)

    rows.append(punch("in2", "1001", local_day.replace(hour=14), direction=True))
    run(db, tenant, odoo, rows, monkeypatch)

    assert _state(db, "in2").process_state == PunchState.synced.value
    assert len(odoo.attendances) == 2


# --------------------------------------------------------------------------- #
# The real client's lookup: the same question Odoo's own constraint asks
# --------------------------------------------------------------------------- #
def _client_returning(rows):
    from tests.test_odoo_company_scoping import FakeXmlRpcModels, make_client

    client = make_client(FakeXmlRpcModels(), company_id=None)
    asked = []

    def execute(model, method, args, kwargs=None):
        if method == "search_read" and model == "hr.attendance":
            asked.append((args[0], kwargs))
            return rows
        return {"check_in": {}, "check_out": {}, "employee_id": {}} if method == "fields_get" else []

    client.execute = execute
    return client, asked


def test_covering_asks_for_the_latest_record_starting_at_or_before():
    from datetime import datetime

    client, asked = _client_returning([
        {"id": 5, "check_in": "2026-10-16 12:05:10", "check_out": "2026-10-17 04:05:10"}
    ])
    moment = datetime(2026, 10, 16, 12, 25, 10)
    got = client.attendance_covering(1, moment)

    domain, kwargs = asked[0]
    assert ("employee_id", "=", 1) in domain
    assert any(c[0] == "check_in" and c[1] == "<=" for c in domain)
    assert kwargs["order"] == "check_in desc" and kwargs["limit"] == 1
    assert got["id"] == 5


def test_covering_is_none_when_that_record_ended_before_the_moment():
    from datetime import datetime

    client, _ = _client_returning([
        {"id": 5, "check_in": "2026-10-16 08:00:00", "check_out": "2026-10-16 10:00:00"}
    ])
    assert client.attendance_covering(1, datetime(2026, 10, 16, 12, 0, 0)) is None


def test_covering_returns_the_record_when_it_ends_after_the_moment():
    from datetime import datetime

    client, _ = _client_returning([
        {"id": 5, "check_in": "2026-10-16 12:05:10", "check_out": "2026-10-17 04:05:10"}
    ])
    assert client.attendance_covering(1, datetime(2026, 10, 16, 12, 25, 10))["id"] == 5


def test_covering_ignores_an_open_record():
    from datetime import datetime

    client, _ = _client_returning([
        {"id": 5, "check_in": "2026-10-16 08:00:00", "check_out": False}
    ])
    assert client.attendance_covering(1, datetime(2026, 10, 16, 12, 0, 0)) is None


# --------------------------------------------------------------------------- #
# A record closed in Odoo behind BioBridge's back must stop showing as Open
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("no_stale_close")
def test_a_record_closed_in_odoo_is_closed_here_too(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    att_id, rows = _first_open(db, tenant, odoo, local_day, monkeypatch)
    mirror = db.scalars(select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == att_id)).one()
    assert mirror.check_out is None

    rec = odoo.attendances[att_id]
    rec["check_out"] = rec["check_in"] + timedelta(hours=16)       # the sweep, or someone by hand
    run(db, tenant, odoo, rows, monkeypatch)

    db.refresh(mirror)
    assert mirror.check_out == rec["check_out"]
    assert abs(mirror.worked_hours - 16.0) < 0.01
    assert mirror.is_auto_closed is True


@pytest.mark.usefixtures("no_stale_close")
def test_a_record_still_open_in_odoo_stays_open_here(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    att_id, rows = _first_open(db, tenant, odoo, local_day, monkeypatch)
    run(db, tenant, odoo, rows, monkeypatch)
    mirror = db.scalars(select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == att_id)).one()
    assert mirror.check_out is None


# --------------------------------------------------------------------------- #
# The hourly stale-shift sweep must update BioBridge's own copy, not only Odoo
# --------------------------------------------------------------------------- #
def test_stale_sweep_closes_the_local_record_too(db, tenant, local_day, monkeypatch):
    from app.services import sync_engine as engine_mod

    odoo = FakeOdoo()
    rows = [punch("in1", "1001", local_day.replace(hour=1), direction=True)]
    real_clock = engine_mod.stale_clock
    monkeypatch.setattr(engine_mod, "stale_clock", lambda: datetime(2000, 1, 1))   # the run leaves it open
    run(db, tenant, odoo, rows, monkeypatch)
    (att_id,) = odoo.attendances.keys()
    mirror = db.scalars(select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == att_id)).one()
    assert mirror.check_out is None

    monkeypatch.setattr(engine_mod, "stale_clock", real_clock)
    assert engine_mod.close_stale_attendances(db, tenant) == 1

    # Odoo and the local copy agree immediately — no later sync needed.
    db.refresh(mirror)
    assert odoo.attendances[att_id]["check_out"] is not None
    assert mirror.check_out == odoo.attendances[att_id]["check_out"]
    assert mirror.is_auto_closed is True
    assert abs(mirror.worked_hours - tenant.max_shift_hours) < 0.01


# --------------------------------------------------------------------------- #
# An older record left open behind newer ones (punches processed out of order)
# --------------------------------------------------------------------------- #
def _seed_blocked_open(odoo, local_day):
    """Joseph-style state: an old record still open, a newer one already closed
    after it. Odoo accepts that order when records are created, but refuses to
    close the old one with a check-out that reaches past the newer one."""
    emp = 11                                    # FakeOdoo's "1001"
    old_id = odoo.create_attendance(emp, local_day.replace(hour=1), None)
    newer_id = odoo.create_attendance(emp, local_day.replace(hour=2), local_day.replace(hour=2, minute=30))
    return old_id, newer_id


def test_an_old_open_record_behind_newer_ones_is_closed_where_the_next_begins(
        db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    old_id, newer_id = _seed_blocked_open(odoo, local_day)
    day2 = local_day + timedelta(days=1)
    rows = [punch("a", "1001", day2.replace(hour=3), direction=True),
            punch("b", "1001", day2.replace(hour=3, minute=1), direction=False)]

    result = run(db, tenant, odoo, rows, monkeypatch)

    assert result.error_count == 0
    for external in ("a", "b"):
        assert _state(db, external).process_state == PunchState.synced.value, external
    old = odoo.attendances[old_id]
    assert old["check_out"] == odoo.attendances[newer_id]["check_in"]     # ends where the next begins
    assert old["auto_closed"] is True
    assert odoo.attendances[newer_id]["check_out"] is not None            # the newer one is untouched
    assert len(odoo.attendances) == 3                                      # and the new pair was created


def test_the_sweep_does_not_close_past_the_next_record(db, tenant, local_day, monkeypatch):
    from app.services import sync_engine as engine_mod

    odoo = FakeOdoo()
    old_id, newer_id = _seed_blocked_open(odoo, local_day)
    # Make the mapping known to the engine, as a normal cycle would.
    run(db, tenant, odoo, [punch("z", "1001", local_day.replace(hour=12), direction=True)], monkeypatch)

    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    engine_mod.close_stale_attendances(db, tenant)
    old = odoo.attendances[old_id]
    assert old["check_out"] is not None
    assert old["check_out"] <= odoo.attendances[newer_id]["check_in"]


# --------------------------------------------------------------------------- #
# A check-out may never reach past the next record, on any path
# --------------------------------------------------------------------------- #
def _fmt(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def test_closing_an_existing_record_is_clipped_at_the_next_one(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    start = local_day.replace(hour=6)
    run(db, tenant, odoo, [punch("in1", "1001", start, direction=True)], monkeypatch)
    (att_id,) = odoo.attendances.keys()
    first = odoo.attendances[att_id]["check_in"]
    odoo.attendances[att_id]["check_out"] = first + timedelta(minutes=30)       # closed some other way
    odoo.create_attendance(11, first + timedelta(hours=2), first + timedelta(hours=2, minutes=30))
    row = _state(db, "in1")
    row.process_state = PunchState.pending.value
    row.odoo_attendance_id = None
    db.commit()

    rows = [punch("in1", "1001", start, direction=True),
            punch("out1", "1001", start + timedelta(hours=5), direction=False)]
    result = run(db, tenant, odoo, rows, monkeypatch)

    assert result.error_count == 0
    assert _state(db, "in1").process_state == PunchState.synced.value
    assert _state(db, "out1").process_state == PunchState.synced.value
    # ended where the next record begins, not at the punch five hours on
    assert odoo.attendances[att_id]["check_out"] == first + timedelta(hours=2)
    assert odoo.attendances[att_id]["auto_closed"] is True
    mirror = db.scalars(select(AttendanceRecord).where(AttendanceRecord.odoo_attendance_id == att_id)).one()
    assert mirror.check_out == first + timedelta(hours=2) and mirror.is_auto_closed is True


def test_a_new_closed_record_is_clipped_at_the_next_one(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    start = local_day.replace(hour=6)
    run(db, tenant, odoo, [punch("seed", "1001", start, direction=True)], monkeypatch)
    (seed_id,) = odoo.attendances.keys()
    base = odoo.attendances[seed_id]["check_in"]
    odoo.attendances[seed_id]["check_out"] = base + timedelta(minutes=10)
    odoo.create_attendance(11, base + timedelta(hours=4), base + timedelta(hours=4, minutes=10))

    rows = [punch("seed", "1001", start, direction=True),
            punch("a", "1001", start + timedelta(hours=1), direction=True),
            punch("b", "1001", start + timedelta(hours=6), direction=False)]
    result = run(db, tenant, odoo, rows, monkeypatch)

    assert result.error_count == 0
    assert _state(db, "a").process_state == PunchState.synced.value
    assert _state(db, "b").process_state == PunchState.synced.value
    made = [r for r in odoo.attendances.values() if r["check_in"] == base + timedelta(hours=1)]
    assert made and made[0]["check_out"] == base + timedelta(hours=4)


def test_one_failing_interval_does_not_fail_the_whole_batch(db, tenant, local_day, monkeypatch):
    """Odoo refusing one shift must leave the employee's other shifts syncing."""
    from app.integrations.odoo import OdooError

    odoo = FakeOdoo()
    day1 = local_day.replace(hour=8) - timedelta(days=2)
    day2 = local_day.replace(hour=8) - timedelta(days=1)
    rows = [
        punch("in1", "1001", day1, direction=True),
        punch("out1", "1001", day1 + timedelta(hours=9), direction=False),
        punch("in2", "1001", day2, direction=True),
        punch("out2", "1001", day2 + timedelta(hours=9), direction=False),
    ]

    real_create = odoo.create_attendance
    refused = []

    def create(employee_id, check_in, *args, **kwargs):
        if not refused:
            refused.append(check_in)
            raise OdooError("Odoo hr.attendance.create failed: refused for the test")
        return real_create(employee_id, check_in, *args, **kwargs)

    monkeypatch.setattr(odoo, "create_attendance", create)
    result = run(db, tenant, odoo, rows, monkeypatch)

    assert result.error_count == 1
    states = {r: _state(db, r).process_state for r in ("in1", "out1", "in2", "out2")}
    assert states["in1"] == states["out1"] == PunchState.error.value
    assert states["in2"] == states["out2"] == PunchState.synced.value
    message = _state(db, "in1").error_message
    assert message.startswith("Interval ") and "refused for the test" in message
    assert _state(db, "in1").attempts == 1


# --------------------------------------------------------------------------- #
# Shifts left open past the limit are closed by every sync, not only the sweep
# --------------------------------------------------------------------------- #
def test_every_sync_closes_shifts_open_past_the_limit(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    # A first run makes the employee known (a whole shift, closed).
    rows = [punch("a", "1001", local_day.replace(hour=1), direction=True),
            punch("b", "1001", local_day.replace(hour=2), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    # Then a check-in nobody badges out of, entered in Odoo past the limit.
    stale = (datetime.utcnow() - timedelta(hours=tenant.max_shift_hours + 5)).replace(microsecond=0)
    att = odoo.create_attendance(11, stale, None)                 # FakeOdoo's "1001"
    # The next run brings no new punch for them, and still closes it.
    result = run(db, tenant, odoo, rows, monkeypatch)
    rec = odoo.attendances[att]
    assert rec["check_out"] == stale + timedelta(hours=tenant.max_shift_hours)
    assert rec["auto_closed"] is True
    assert result.attendances_closed >= 1


def test_a_shift_inside_the_limit_is_left_open(db, tenant, local_day, monkeypatch):
    from app.services import sync_engine as engine_mod

    odoo = FakeOdoo()
    run(db, tenant, odoo, [punch("m", "1001", local_day.replace(hour=1), direction=True)], monkeypatch)
    att = odoo.create_attendance(11, datetime.utcnow() - timedelta(hours=tenant.max_shift_hours - 1), None)
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    engine_mod.close_stale_attendances(db, tenant)
    assert odoo.attendances[att]["check_out"] is None


# --------------------------------------------------------------------------- #
# BioBridge's copy follows Odoo: records added or changed there show up here
# --------------------------------------------------------------------------- #
def test_records_added_or_changed_in_odoo_are_copied_here(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo()
    rows = [punch("a", "1001", local_day.replace(hour=8), direction=True),
            punch("b", "1001", local_day.replace(hour=12), direction=False)]
    run(db, tenant, odoo, rows, monkeypatch)
    (ours,) = odoo.attendances.keys()

    # In Odoo: our record is shortened, and another is entered by hand for the same person.
    odoo.attendances[ours]["check_out"] = odoo.attendances[ours]["check_in"] + timedelta(hours=2)
    manual = odoo.create_attendance(11, local_day.replace(hour=18), local_day.replace(hour=20))
    run(db, tenant, odoo, rows, monkeypatch)

    copies = {r.odoo_attendance_id: r for r in db.scalars(select(AttendanceRecord))}
    assert copies[ours].check_out == odoo.attendances[ours]["check_out"]
    assert copies[ours].origin == "biobridge" and abs(copies[ours].worked_hours - 2.0) < 0.01
    assert copies[manual].origin == "odoo" and copies[manual].emp_code == "1001"
    assert copies[manual].check_in == local_day.replace(hour=18) and abs(copies[manual].worked_hours - 2.0) < 0.01
    assert tenant.odoo_attendance_synced_at is not None

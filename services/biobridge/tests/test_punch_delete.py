"""Delete on the Activity screen: out of the queue for good, and restorable."""

from __future__ import annotations

from sqlalchemy import select

from app.models import PunchRecord, PunchState
from tests.conftest import FakeOdoo
from tests.test_punch_ledger import api, punches  # noqa: F401 — the api fixture
from tests.test_sync_engine import punch, run


def _id(api, external_state):  # noqa: F811
    return next(r["id"] for r in punches(api, state=external_state))


def test_delete_hides_the_punch_and_restore_brings_it_back(api):  # noqa: F811
    pid = _id(api, "unmapped")
    response = api.delete(f"/api/v1/punches/{pid}")
    assert response.status_code == 200, response.text

    assert pid not in {r["id"] for r in punches(api)}, "gone from the unfiltered list"
    (deleted,) = punches(api, state="deleted")
    assert deleted["id"] == pid and deleted["error_message"] == "Deleted (was unmapped)"

    assert api.post(f"/api/v1/punches/{pid}/retry").status_code == 200
    assert next(r for r in punches(api) if r["id"] == pid)["process_state"] == "pending"


def test_an_error_punch_can_be_deleted_too(api):  # noqa: F811
    assert api.delete(f"/api/v1/punches/{_id(api, 'error')}").status_code == 200


def test_a_synced_or_pending_punch_cannot_be_deleted(api):  # noqa: F811
    for state in ("synced", "pending"):
        response = api.delete(f"/api/v1/punches/{_id(api, state)}")
        assert response.status_code == 409, state
        assert "can't be deleted" in response.json()["detail"]


def test_an_unknown_punch_is_404(api):  # noqa: F811
    assert api.delete("/api/v1/punches/nope").status_code == 404


def test_the_next_sync_neither_re_ingests_nor_pushes_a_deleted_punch(
    db, tenant, local_day, monkeypatch
):
    rows = [punch(1, "1001", local_day.replace(hour=8))]
    run(db, tenant, FakeOdoo(employees={}), rows, monkeypatch)  # badge unknown → unmapped
    (stored,) = db.scalars(select(PunchRecord)).all()
    stored.process_state = PunchState.deleted.value
    db.commit()

    odoo = FakeOdoo()  # Odoo now knows the badge
    run(db, tenant, odoo, rows, monkeypatch)  # the same punch is on the wire again

    assert odoo.attendances == {}, "a deleted punch is never pushed"
    (again,) = db.scalars(select(PunchRecord)).all()
    assert again.process_state == PunchState.deleted.value, "and not re-ingested as new"


def test_an_error_punch_already_in_odoo_is_protected_with_a_clear_reason(api):  # noqa: F811
    from app.db.session import get_db
    from app.main import app

    pid = _id(api, "error")
    db = next(app.dependency_overrides[get_db]())
    db.get(PunchRecord, pid).odoo_attendance_id = 42
    db.commit()
    db.close()
    r = api.delete(f"/api/v1/punches/{pid}")
    assert r.status_code == 409 and "attendance #42" in r.json()["detail"]


# --- bulk, from the Activity list's checkboxes --------------------------------------------------
def _states(api):  # noqa: F811
    return {r["id"]: r["process_state"] for r in punches(api, state="")} | {
        r["id"]: r["process_state"] for r in punches(api, state="deleted")}


def test_bulk_delete_takes_the_eligible_ones_and_skips_the_rest(api):  # noqa: F811
    unmapped, error, synced = _id(api, "unmapped"), _id(api, "error"), _id(api, "synced")
    r = api.post("/api/v1/punches/bulk", json={"ids": [unmapped, error, synced, "nope"], "action": "delete"})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == 2 and r.json()["skipped"] == 2
    assert r.json()["message"] == "2 punches deleted, 2 skipped"
    states = _states(api)
    assert states[unmapped] == states[error] == "deleted" and states[synced] == "synced"


def test_bulk_move_to_pending_retries_and_restores(api):  # noqa: F811
    unmapped, error = _id(api, "unmapped"), _id(api, "error")
    api.delete(f"/api/v1/punches/{unmapped}")                       # one deleted, one in error
    r = api.post("/api/v1/punches/bulk", json={"ids": [unmapped, error, _id(api, "synced")], "action": "pending"})
    assert r.json()["changed"] == 2 and r.json()["skipped"] == 1
    states = _states(api)
    assert states[unmapped] == states[error] == "pending"
    row = next(x for x in punches(api) if x["id"] == error)
    assert row["error_message"] is None


def test_bulk_never_touches_a_punch_already_in_odoo(api):  # noqa: F811
    from app.db.session import get_db
    from app.main import app

    pid = _id(api, "error")
    db = next(app.dependency_overrides[get_db]())
    db.get(PunchRecord, pid).odoo_attendance_id = 42
    db.commit()
    db.close()
    for action in ("delete", "pending"):
        assert api.post("/api/v1/punches/bulk", json={"ids": [pid], "action": action}).json()["changed"] == 0
    assert _states(api)[pid] == "error"


def test_bulk_rejects_an_empty_or_unknown_action(api):  # noqa: F811
    assert api.post("/api/v1/punches/bulk", json={"ids": [], "action": "delete"}).status_code == 422
    assert api.post("/api/v1/punches/bulk", json={"ids": ["x"], "action": "sync"}).status_code == 422


# --- every matching punch, not just the page on screen ----------------------------------------
def test_selectable_counts_every_matching_punch(api):  # noqa: F811
    counts = api.get("/api/v1/punches/selectable").json()
    listed = punches(api)
    assert counts["delete"] == sum(r["process_state"] in ("error", "skipped", "unmapped") and not r["odoo_attendance_id"] for r in listed)
    assert counts["matching"] == len(listed) >= counts["rerun"] >= counts["delete"]
    only_errors = api.get("/api/v1/punches/selectable", params={"state": "error"}).json()
    assert only_errors["delete"] == sum(r["process_state"] == "error" for r in listed)


def test_bulk_by_filter_reaches_past_the_page(api):  # noqa: F811
    before = api.get("/api/v1/punches/selectable").json()
    r = api.post("/api/v1/punches/bulk", json={"filter": {}, "action": "delete"})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == before["delete"] > 0
    assert all(x["process_state"] not in ("error", "skipped", "unmapped") for x in punches(api))
    deleted = punches(api, state="deleted")
    assert all(x["error_message"].startswith("Deleted (was ") for x in deleted)
    # And back: rerun everything on the Deleted tab.
    r = api.post("/api/v1/punches/bulk", json={"filter": {"state": "deleted"}, "action": "pending"})
    assert r.json()["changed"] == len(deleted)
    assert punches(api, state="deleted") == []


def test_bulk_needs_ids_or_a_filter_not_both(api):  # noqa: F811
    assert api.post("/api/v1/punches/bulk", json={"action": "delete"}).status_code == 422
    assert api.post("/api/v1/punches/bulk", json={"ids": ["x"], "filter": {}, "action": "delete"}).status_code == 422


def test_selectable_counts_synced_and_can_leave_them_out(api):  # noqa: F811
    counts = api.get("/api/v1/punches/selectable").json()
    synced = sum(r["process_state"] == "synced" or bool(r["odoo_attendance_id"]) for r in punches(api))
    assert counts["synced"] == synced > 0
    without = api.get("/api/v1/punches/selectable", params={"exclude_synced": "true"}).json()
    assert without["matching"] == counts["matching"] - synced and without["synced"] == 0
    assert without["delete"] == counts["delete"]
    r = api.post("/api/v1/punches/bulk", json={"filter": {"exclude_synced": True}, "action": "delete"})
    assert r.json()["changed"] == counts["delete"]

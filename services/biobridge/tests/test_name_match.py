"""A badge and a name that disagree are decided by a person, not guessed.

Two cases, both kept unmatched (their punches wait) with the likely Odoo
employee suggested, and listed ahead of the plain unmatched badges:

* the same badge on a different name;
* the same name with a different badge, or none.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.session import get_db
from app.integrations.base import EmployeeRecord
from app.main import app
from app.models import EmployeeMapping, MappingStatus, PunchRecord, PunchState
from app.services import sync_engine as engine_mod
from app.services.name_match import (
    KIND_BADGE_ONLY, KIND_DISMISSED, KIND_NAME_ONLY, compatible, name_candidates, same_name, tokens, usable_name,
)
from tests.conftest import FakeOdoo, FakeProvider
from tests.test_api_isolation import auth, client, signup  # noqa: F401  (the API tests' fixtures)


# --------------------------------------------------------------------------- #
# The comparison itself
# --------------------------------------------------------------------------- #
def test_names_compare_by_their_words_not_their_order_or_accents():
    assert tokens("Sharma, Ahmed") == tokens("ahmed  SHARMA") == ["sharma", "ahmed"] or \
        set(tokens("Sharma, Ahmed")) == set(tokens("ahmed  SHARMA"))
    assert tokens("José Álvarez") == ["jose", "alvarez"]
    assert tokens("Dr. Jane Doe") == ["jane", "doe"]


@pytest.mark.parametrize("device, odoo", [
    ("Jane Doe", "Jane Doe"), ("Doe Jane", "Jane Doe"), ("Jane", "Jane Doe"),
    ("Jane Doe", "Jane Mary Doe"), ("Mohammed Ali", "Muhammad Ali"), ("JANE DOE", "jane doe"),
])
def test_the_same_person_written_differently_is_compatible(device, odoo):
    assert compatible(device, odoo)


@pytest.mark.parametrize("device, odoo", [
    ("Omar Haddad", "Jane Doe"), ("Priya Nakamura", "Liam Okafor"), ("Jane Doe", "Jack Dole Smith"),
])
def test_different_people_are_not(device, odoo):
    assert not compatible(device, odoo)


def test_nothing_to_compare_never_blocks_a_match():
    assert compatible(None, "Jane Doe") and compatible("Jane Doe", "") and compatible("", "")


@pytest.mark.parametrize("name", [None, "", "  ", "Unknown Badge", "Employee 1001", "1001", "X Y", "N/A"])
def test_placeholder_names_say_nothing_about_the_person(name):
    assert usable_name(name, "1001") is None


def test_a_real_name_is_usable():
    assert usable_name("Jane Doe", "1001") == "Jane Doe"


def test_a_name_alone_only_suggests_a_full_name_match():
    assert same_name("Priya Nakamura", "priya nakamura") and same_name("Priya Nakamura", "Nakamura, Priya")
    assert same_name("Priya Nakamura", "Priya K Nakamura")          # a longer version of the same name
    assert not same_name("Priya", "Priya Nakamura")                  # "Priya" is too common to suggest
    assert not same_name("Priya Nakamura", "Priya Fernandes")
    assert same_name("Beevi", "Beevi")                               # one-word names match when equal


def test_candidates_skip_people_who_already_hold_a_badge():
    roster = [{"id": 1, "name": "Priya Nakamura"}, {"id": 2, "name": "Priya Nakamura"}, {"id": 3, "name": "Other"}]
    assert [r["id"] for r in name_candidates("Priya Nakamura", roster, taken={1})] == [2]


# --------------------------------------------------------------------------- #
# The sync
# --------------------------------------------------------------------------- #
def named_punch(pid, emp, moment, first, last):
    return {"id": pid, "emp_code": emp, "punch_time": moment.strftime("%Y-%m-%d %H:%M:%S"),
            "direction": None, "terminal_sn": "GATE-01", "first_name": first, "last_name": last}


def sync(db, tenant, odoo, rows, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: FakeProvider(rows))
    return engine_mod.SyncEngine(db, tenant, "test").run_cycle()


def mapping(db, code):
    db.expire_all()
    return db.scalars(select(EmployeeMapping).where(EmployeeMapping.emp_code == code)).one()


def test_same_badge_different_name_is_not_matched_but_suggested(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo({"1001": (11, "Jane Doe")})
    run = sync(db, tenant, odoo, [named_punch(1, "1001", local_day.replace(hour=8), "Omar", "Haddad")], monkeypatch)
    assert run.status == "success", run.error_message
    m = mapping(db, "1001")
    assert m.status == MappingStatus.unmapped.value and m.odoo_employee_id is None
    assert (m.suggested_employee_id, m.suggested_employee_name, m.suggestion_kind) == (11, "Jane Doe", KIND_BADGE_ONLY)
    assert "Omar Haddad" in m.match_note and "Jane Doe" in m.match_note
    assert odoo.attendances == {}                                   # nobody's attendance was written
    assert db.scalars(select(PunchRecord)).one().process_state == PunchState.unmapped.value


def test_same_badge_same_name_matches_as_before(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo({"1001": (11, "Jane Doe")})
    sync(db, tenant, odoo, [named_punch(1, "1001", local_day.replace(hour=8), "Doe", "Jane")], monkeypatch)
    m = mapping(db, "1001")
    assert m.status == MappingStatus.mapped.value and m.odoo_employee_id == 11 and m.suggested_employee_id is None
    assert len(odoo.attendances) == 1


def test_a_device_that_reports_no_name_still_matches_by_badge(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo({"1001": (11, "Jane Doe")})
    sync(db, tenant, odoo, [named_punch(1, "1001", local_day.replace(hour=8), "", "")], monkeypatch)
    assert mapping(db, "1001").status == MappingStatus.mapped.value


def test_correcting_the_name_in_odoo_lets_the_next_sync_match_it(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo({"1001": (11, "Jane Doe")})
    rows = [named_punch(1, "1001", local_day.replace(hour=8), "Jane", "Dole")]
    sync(db, tenant, FakeOdoo({"1001": (11, "Omar Haddad")}), rows, monkeypatch)
    assert mapping(db, "1001").suggestion_kind == KIND_BADGE_ONLY
    sync(db, tenant, odoo, rows, monkeypatch)                          # Odoo now has the person's name
    m = mapping(db, "1001")
    assert m.status == MappingStatus.mapped.value and m.suggestion_kind is None


def test_same_name_different_badge_is_suggested_not_created(db, tenant, local_day, monkeypatch):
    tenant.auto_create_employees = True
    odoo = FakeOdoo({})
    odoo.roster = [{"id": 21, "name": "Priya Nakamura", "active": True, "barcode": "X-77"}]
    created = []
    odoo.create_employee = lambda name, code: created.append((name, code)) or 99
    sync(db, tenant, odoo, [named_punch(1, "9001", local_day.replace(hour=8), "Priya", "Nakamura")], monkeypatch)
    m = mapping(db, "9001")
    assert created == [], "a person with that name exists: no duplicate is made"
    assert m.status == MappingStatus.unmapped.value
    assert (m.suggested_employee_id, m.suggestion_kind) == (21, KIND_NAME_ONLY)
    assert "X-77" in m.match_note and "9001" in m.match_note


def test_same_name_and_no_badge_in_odoo_is_suggested_too(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo({})
    odoo.roster = [{"id": 21, "name": "Priya Nakamura", "active": True}]
    sync(db, tenant, odoo, [named_punch(1, "9001", local_day.replace(hour=8), "Priya", "Nakamura")], monkeypatch)
    m = mapping(db, "9001")
    assert m.suggested_employee_id == 21 and "no badge" in m.match_note


def test_an_employee_who_already_has_a_badge_here_is_not_suggested_again(db, tenant, local_day, monkeypatch):
    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code="1001", status="mapped", odoo_employee_id=21,
                           odoo_employee_name="Priya Nakamura"))
    db.commit()
    odoo = FakeOdoo({})
    odoo.roster = [{"id": 21, "name": "Priya Nakamura", "active": True, "barcode": "1001"}]
    sync(db, tenant, odoo, [named_punch(1, "9001", local_day.replace(hour=8), "Priya", "Nakamura")], monkeypatch)
    m = mapping(db, "9001")
    assert m.suggested_employee_id is None and "No Odoo employee has" in m.match_note


def test_a_name_nobody_has_still_creates_when_auto_create_is_on(db, tenant, local_day, monkeypatch):
    tenant.auto_create_employees = True
    odoo = FakeOdoo({})
    odoo.roster = [{"id": 21, "name": "Someone Else", "active": True}]
    created = []

    def create(name, code):
        created.append((name, code))
        odoo.employees[code] = (99, name)          # Odoo now knows the new employee,
        odoo.roster.append({"id": 99, "name": name, "active": True})   # and lists them
        return 99
    odoo.create_employee = create
    run = sync(db, tenant, odoo, [named_punch(1, "9001", local_day.replace(hour=8), "Priya", "Nakamura")], monkeypatch)
    assert run.status == "success", run.error_message
    assert created == [("Priya Nakamura", "9001")]
    assert mapping(db, "9001").status == MappingStatus.mapped.value


def test_a_first_name_alone_does_not_suggest_every_namesake(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo({})
    odoo.roster = [{"id": 21, "name": "Priya Nakamura", "active": True}]
    sync(db, tenant, odoo, [named_punch(1, "9001", local_day.replace(hour=8), "Priya", "")], monkeypatch)
    assert mapping(db, "9001").suggested_employee_id is None


# --------------------------------------------------------------------------- #
# The device-user import (before any punch)
# --------------------------------------------------------------------------- #
def import_users(db, tenant, odoo, users, monkeypatch):
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: odoo)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: FakeProvider([], employees=users))
    return engine_mod.SyncEngine(db, tenant, "test").run_cycle()


def test_import_holds_a_badge_whose_name_disagrees(db, tenant, monkeypatch):
    odoo = FakeOdoo()
    odoo.roster = [{"id": 11, "name": "Jane Doe", "active": True, "barcode": "1001"},
                   {"id": 12, "name": "Omar Haddad", "active": True, "barcode": "1002"}]
    import_users(db, tenant, odoo, [EmployeeRecord(None, "1001", "Priya", "Nakamura"),
                                    EmployeeRecord(None, "1002", "Omar", "Haddad")], monkeypatch)
    wrong, right = mapping(db, "1001"), mapping(db, "1002")
    assert wrong.status == MappingStatus.unmapped.value and wrong.suggestion_kind == KIND_BADGE_ONLY
    assert wrong.suggested_employee_id == 11
    assert right.status == MappingStatus.mapped.value and right.odoo_employee_id == 12


def test_import_suggests_a_same_name_employee_with_another_badge(db, tenant, monkeypatch):
    odoo = FakeOdoo()
    odoo.roster = [{"id": 21, "name": "Priya Nakamura", "active": True, "barcode": "X-77"}]
    import_users(db, tenant, odoo, [EmployeeRecord(None, "9001", "Priya", "Nakamura"),
                                    EmployeeRecord(None, "7777", "Nobody", "Known")], monkeypatch)
    m = mapping(db, "9001")
    assert m.status == MappingStatus.unmapped.value and (m.suggested_employee_id, m.suggestion_kind) == (21, KIND_NAME_ONLY)
    assert db.scalars(select(EmployeeMapping).where(EmployeeMapping.emp_code == "7777")).first() is None


# --------------------------------------------------------------------------- #
# The API: review first, confirm, "not the same person"
# --------------------------------------------------------------------------- #
def _setup(client):
    token = signup(client, "Acme", "owner@example.com")
    tid = client.get("/api/v1/tenant", headers=auth(token)).json()["id"]
    db = next(app.dependency_overrides[get_db]())
    return token, tid, db


def test_badges_to_review_are_listed_first_with_their_suggestion(client):
    token, tid, db = _setup(client)
    db.add_all([
        EmployeeMapping(tenant_id=tid, emp_code="0001", status="unmapped", match_note="No Odoo employee has it."),
        EmployeeMapping(tenant_id=tid, emp_code="0002", status="ambiguous"),
        EmployeeMapping(tenant_id=tid, emp_code="9999", status="unmapped", source_name="Priya Nakamura",
                        suggested_employee_id=21, suggested_employee_name="Priya Nakamura", suggestion_kind=KIND_NAME_ONLY),
    ])
    db.commit()
    rows = client.get("/api/v1/mappings", headers=auth(token)).json()
    assert [r["emp_code"] for r in rows] == ["9999", "0002", "0001"]
    assert rows[0]["suggested_employee_id"] == 21 and rows[0]["suggestion_kind"] == KIND_NAME_ONLY


def test_confirming_the_suggestion_matches_and_releases_the_punches(client):
    token, tid, db = _setup(client)
    m = EmployeeMapping(tenant_id=tid, emp_code="9001", status="unmapped", suggested_employee_id=21,
                        suggested_employee_name="Priya Nakamura", suggestion_kind=KIND_NAME_ONLY)
    db.add(m)
    db.commit()
    r = client.patch(f"/api/v1/mappings/{m.id}", headers=auth(token),
                     json={"odoo_employee_id": 21, "odoo_employee_name": "Priya Nakamura"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "mapped" and body["match_method"] == "manual"
    assert body["suggested_employee_id"] is None and body["suggestion_kind"] is None


def test_not_the_same_person_forgets_the_suggestion_and_keeps_the_badge_waiting(client):
    token, tid, db = _setup(client)
    m = EmployeeMapping(tenant_id=tid, emp_code="9001", status="unmapped", suggested_employee_id=21,
                        suggested_employee_name="Priya Nakamura", suggestion_kind=KIND_NAME_ONLY)
    db.add(m)
    db.commit()
    body = client.patch(f"/api/v1/mappings/{m.id}", headers=auth(token), json={"dismiss_suggestion": True}).json()
    assert body["status"] == "unmapped" and body["suggested_employee_id"] is None
    assert body["suggestion_kind"] == KIND_DISMISSED


def test_a_dismissed_suggestion_is_not_made_again_by_the_next_sync(db, tenant, local_day, monkeypatch):
    odoo = FakeOdoo({})
    odoo.roster = [{"id": 21, "name": "Priya Nakamura", "active": True, "barcode": "X-77"}]
    rows = [named_punch(1, "9001", local_day.replace(hour=8), "Priya", "Nakamura")]
    sync(db, tenant, odoo, rows, monkeypatch)
    m = mapping(db, "9001")
    assert m.suggested_employee_id == 21
    m.suggested_employee_id, m.suggested_employee_name, m.suggestion_kind = None, None, KIND_DISMISSED
    db.commit()
    sync(db, tenant, odoo, rows, monkeypatch)
    m = mapping(db, "9001")
    assert m.suggested_employee_id is None and m.status == MappingStatus.unmapped.value


def test_a_dismissed_badge_match_is_never_made_automatically(db, tenant, local_day, monkeypatch):
    db.add(EmployeeMapping(tenant_id=tenant.id, emp_code="1001", status="unmapped", suggestion_kind=KIND_DISMISSED))
    db.commit()
    odoo = FakeOdoo({"1001": (11, "Jane Doe")})
    sync(db, tenant, odoo, [named_punch(1, "1001", local_day.replace(hour=8), "Omar", "Haddad")], monkeypatch)
    m = mapping(db, "1001")
    assert m.status == MappingStatus.unmapped.value and m.suggested_employee_id is None
    assert "said is not" in m.match_note and odoo.attendances == {}

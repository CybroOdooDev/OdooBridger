"""Guards that keep simultaneous requests consistent (tests_load/ runs them for real on PostgreSQL)."""

from __future__ import annotations

from obcore.db import SessionLocal
from obcore.models import AccountService
from obcore.services import provisioning
from tests.conftest import signup


def test_an_older_push_finishing_late_never_lowers_the_acknowledged_revision(client, catalog, service_app):
    signup(client)
    db = SessionLocal()
    row = db.query(AccountService).first()
    row.revision = 7
    db.commit()
    provisioning._ack(db, row, 7)
    provisioning._ack(db, row, 5)          # the push of revision 5 answered last
    db.commit()
    assert db.get(AccountService, row.id).provisioned_revision == 7
    db.close()


def test_a_revision_bump_reads_the_stored_value(client, catalog, service_app):
    signup(client)
    db = SessionLocal()
    row = db.query(AccountService).first()
    stored = row.revision
    other = SessionLocal()
    other.get(AccountService, row.id).revision = stored + 3   # another request got there first
    other.commit()
    other.close()
    provisioning.mark_changed(row)
    assert row.revision == stored + 4
    db.close()


def test_adding_a_service_already_added_by_another_request_is_a_conflict(client, catalog, service_app):
    headers = signup(client, services=[])
    assert client.post("/api/v1/portal/services", headers=headers, json={"service": "biometric"}).status_code == 201
    again = client.post("/api/v1/portal/services", headers=headers, json={"service": "biometric"})
    assert again.status_code in (400, 409)

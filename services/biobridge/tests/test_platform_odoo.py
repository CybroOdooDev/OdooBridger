"""Platform mode: the Odoo connection is OdooBridger's, BioBridge uses its copy."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.crypto import decrypt, encrypt
from app.integrations.odoo import OdooAuthError
from app.models import OdooConnection, Tenant
from app.services import platform_link
from app.services import sync_engine as engine_mod
from tests.conftest import FakeOdoo, FakeProvider
from tests.test_platform_mode import ACCOUNT, client, put, state, token  # noqa: F401 — the fixture

ODOO = {"id": "c" * 32, "name": "Odoo", "url": "https://acme.odoo.com", "db": "acme", "login": "bot@acme.com",
        "api_key": "key-one", "version": "19.0", "api": "json2", "companies": [{"id": 1, "name": "Acme"}],
        "status": "connected"}


def _db(client):
    return client.Session()


def _conns(db) -> list[OdooConnection]:
    return list(db.scalars(select(OdooConnection)).all())


def _headers(bearer: str) -> dict:
    return {"Authorization": f"Bearer {bearer}"}


def _sign_in(client, monkeypatch) -> dict:
    """An owner of the platform tenant, signed in to BioBridge's own API."""
    from tests.test_platform_mode import _sso

    r, _ = _sso(client, monkeypatch)
    return _headers(r.json()["access_token"])


def test_the_connection_arrives_encrypted_and_marked_managed(client):
    assert put(client, state(odoo=ODOO)).status_code == 200
    db = _db(client)
    (conn,) = _conns(db)
    tenant = db.get(Tenant, conn.tenant_id)
    assert conn.managed and conn.platform_connection_id == ODOO["id"] and conn.is_active
    assert conn.url == ODOO["url"] and conn.db_name == "acme" and conn.username == "bot@acme.com"
    assert "key-one" not in conn.api_key_enc and decrypt(conn.api_key_enc, tenant.crypto_key) == "key-one"
    assert conn.status == "unverified"
    db.close()


def test_a_new_key_replaces_the_old_and_drops_the_session(client):
    put(client, state(odoo=ODOO))
    db = _db(client)
    (conn,) = _conns(db)
    conn.uid_cache, conn.status = 7, "connected"
    conn.disabled_company_ids = [2]
    db.commit()
    db.close()
    put(client, state(odoo={**ODOO, "api_key": "key-two"}, revision=2))
    db = _db(client)
    (conn,) = _conns(db)
    tenant = db.get(Tenant, conn.tenant_id)
    assert decrypt(conn.api_key_enc, tenant.crypto_key) == "key-two"
    assert conn.uid_cache is None and conn.status == "unverified"
    assert conn.disabled_company_ids == [2]          # BioBridge's own choice is kept
    db.close()


def test_the_existing_connection_to_the_same_odoo_is_taken_over(client):
    put(client, state())
    db = _db(client)
    tenant = db.scalar(select(Tenant))
    own = OdooConnection(tenant_id=tenant.id, name="Primary Odoo", url="https://acme.odoo.com/", db_name="ACME",
                         username="old@acme.com", api_key_enc=encrypt("old", tenant.crypto_key),
                         disabled_company_ids=[3])
    other = OdooConnection(tenant_id=tenant.id, name="Test Odoo", url="https://test.example", db_name="t",
                           username="x", api_key_enc=encrypt("x", tenant.crypto_key))
    db.add_all([own, other])
    db.commit()
    own_id = own.id
    db.close()
    put(client, state(odoo=ODOO, revision=2))
    db = _db(client)
    conns = {c.id: c for c in _conns(db)}
    assert len(conns) == 2
    taken = conns[own_id]
    assert taken.managed and taken.username == "bot@acme.com" and taken.disabled_company_ids == [3]
    assert not next(c for c in conns.values() if c.id != own_id).is_active
    db.close()


def test_disconnecting_in_odoobridger_stops_using_it_without_deleting(client):
    put(client, state(odoo=ODOO))
    put(client, state(odoo=None, revision=2))
    db = _db(client)
    (conn,) = _conns(db)
    assert not conn.is_active and "disconnected in OdooBridger" in conn.status_message
    db.close()
    # Provisioning without an "odoo" key at all (an identity token, an older
    # core) leaves the connection alone.
    put(client, state(odoo=ODOO, revision=3))
    put(client, state(revision=4))
    db = _db(client)
    assert _conns(db)[0].is_active
    db.close()


def test_a_broken_block_is_refused(client):
    r = put(client, state(odoo={**ODOO, "api_key": ""}))
    assert r.status_code == 409 and "no api_key" in r.json()["detail"]


def test_the_dashboard_cannot_change_what_odoobridger_sets(client, monkeypatch):
    put(client, state(odoo=ODOO))
    headers = _sign_in(client, monkeypatch)
    (conn,) = client.get("/api/v1/odoo-connections", headers=headers).json()
    assert conn["managed"] is True
    r = client.patch(f"/api/v1/odoo-connections/{conn['id']}", json={"url": "https://evil.example"}, headers=headers)
    assert r.status_code == 409 and "OdooBridger" in r.json()["detail"]
    r = client.patch(f"/api/v1/odoo-connections/{conn['id']}", json={"api_key": "mine"}, headers=headers)
    assert r.status_code == 409
    # The form sends the stored values back with the company switches: fine.
    r = client.patch(f"/api/v1/odoo-connections/{conn['id']}", headers=headers, json={
        "url": conn["url"], "db_name": conn["db_name"], "username": conn["username"],
        "disabled_company_ids": [2]})
    assert r.status_code == 200, r.text
    assert r.json()["disabled_company_ids"] == [2]
    assert client.delete(f"/api/v1/odoo-connections/{conn['id']}", headers=headers).status_code == 409
    r = client.post("/api/v1/odoo-connections", headers=headers, json={
        "name": "Mine", "url": "https://other.example", "db_name": "o", "username": "u", "api_key": "k"})
    assert r.status_code == 409 and "OdooBridger" in r.json()["detail"]


def _usage(client, live: bool = False):
    bearer = token("provision", {"sub": "odoobridger-core", "account_id": ACCOUNT,
                                 "body_sha256": __import__("hashlib").sha256(b"").hexdigest()})
    url = f"/platform/v1/accounts/{ACCOUNT}/usage" + ("?check_odoo=1" if live else "")
    return client.get(url, headers=_headers(bearer))


def test_usage_reports_readiness(client, monkeypatch):
    put(client, state(odoo=ODOO))
    assert _usage(client).json()["odoo"]["ready"] is None          # not checked yet

    class Pinged:
        def ping(self):
            return {"uid": 7, "server_version": "19.0", "can_create_attendance": True,
                    "has_companion_addon": False, "has_device_tracking": False, "device_tracking_mode": None}

    monkeypatch.setattr("app.services.connections.build_odoo_client", lambda t, c: Pinged())
    report = _usage(client, live=True).json()["odoo"]
    assert report["ready"] is True and report["auth_failed"] is False
    assert {c["name"]: c["ok"] for c in report["checks"]} == {
        "Sign in": True, "Create attendance": True, "Device tracking": False}

    class Refused:
        def ping(self):
            raise OdooAuthError("Odoo rejected the API key")

    monkeypatch.setattr("app.services.connections.build_odoo_client", lambda t, c: Refused())
    report = _usage(client, live=True).json()["odoo"]
    assert report["ready"] is False and report["auth_failed"] is True
    assert report["message"].startswith("Login failed")
    # Reported again, without a live check, from what was recorded.
    assert _usage(client).json()["odoo"]["auth_failed"] is True


def test_no_odoo_means_no_readiness(client):
    put(client, state())
    assert _usage(client).json()["odoo"] is None


# --------------------------------------------------------------------------- #
# The sync engine
# --------------------------------------------------------------------------- #
@pytest.fixture
def managed(db, tenant):
    conn = db.scalar(select(OdooConnection).where(OdooConnection.tenant_id == tenant.id))
    conn.platform_connection_id = ODOO["id"]
    db.commit()
    return conn


def test_a_key_rotated_mid_run_is_picked_up(db, tenant, managed, monkeypatch, local_day):
    """The run loaded the old key; OdooBridger sends a new one before it signs
    in. The first sign-in fails, the engine reads the connection again, and the
    second attempt (with the new key) goes through."""
    attempts = []

    class Odoo(FakeOdoo):
        def authenticate(self):
            attempts.append(self.key)
            if self.key != "new-key":
                raise OdooAuthError("Odoo rejected the API key")
            return super().authenticate()

    def build(t, c):
        odoo = Odoo()
        odoo.key = decrypt(c.api_key_enc, t.crypto_key)
        if len(attempts) == 0:
            # Meanwhile, in another session: the new key arrives.
            from sqlalchemy.orm import Session

            other = Session(bind=db.get_bind())
            row = other.get(OdooConnection, c.id)
            row.api_key_enc = encrypt("new-key", t.crypto_key)
            other.commit()
            other.close()
        return odoo

    monkeypatch.setattr(engine_mod, "build_odoo_client", build)
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: FakeProvider([]))
    run = engine_mod.SyncEngine(db, tenant, "test").run_cycle()
    assert attempts == ["key", "new-key"]
    assert run.status in ("success", "partial"), run.error_message


def test_a_refused_key_marks_the_connection_failed(db, tenant, managed, monkeypatch):
    class Odoo(FakeOdoo):
        def authenticate(self):
            raise OdooAuthError("Odoo rejected the API key")

    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: Odoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: FakeProvider([]))
    run = engine_mod.SyncEngine(db, tenant, "test").run_cycle()
    db.refresh(managed)
    assert run.status == "failed"
    assert managed.status == "failed" and managed.status_message.startswith("Login failed")
    report = platform_link.odoo_readiness(db, tenant)
    assert report["auth_failed"] is True


def test_the_managed_connection_wins(db, tenant, managed):
    extra = OdooConnection(tenant_id=tenant.id, name="Zz", url="https://zz.example", db_name="z", username="u",
                           api_key_enc=encrypt("k", tenant.crypto_key), is_active=True)
    db.add(extra)
    db.commit()
    assert engine_mod.SyncEngine(db, tenant, "test")._active_odoo_connection().id == managed.id

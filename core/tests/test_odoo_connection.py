"""The account's Odoo connection: test, save, uniqueness, provisioning, readiness."""

from __future__ import annotations

import json
import xmlrpc.client
from datetime import timedelta

import httpx
import pytest

from obcore.db import SessionLocal
from obcore.mailer import OUTBOX
from obcore.models import AuditLog, OdooConnection
from obcore.security import utcnow
from obcore.services import odoo_connections, odoo_probe

from tests.conftest import link_token, signup, staff_headers

GOOD_KEY = "good-api-key-123"


class FakeOdoo:
    """Enough of Odoo 19 (JSON-2) or Odoo 17 (XML-RPC) for the connection test."""

    def __init__(self, version: str = "19.0", db: str = "acme", login: str = "bot@acme.io",
                 key: str = GOOD_KEY, db_uuid: str | None = "uuid-acme"):
        self.version, self.db, self.login, self.key, self.db_uuid = version, db, login, key, db_uuid
        self.companies = [{"id": 1, "name": "Acme HQ"}, {"id": 2, "name": "Acme Gulf"}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        major = int(self.version.split(".")[0])
        if path == "/web/version":
            if major < 17:
                return httpx.Response(404)
            return httpx.Response(200, json={"version": self.version,
                                             "version_info": [major, 0, 0, "final", 0, ""]})
        if path.startswith("/json/2/"):
            if request.headers.get("authorization") != f"bearer {self.key}":
                return httpx.Response(401, json={"name": "werkzeug.exceptions.Unauthorized",
                                                 "message": "Invalid apikey"})
            if request.headers.get("x-odoo-database") != self.db:
                return httpx.Response(404, json={"name": "KeyError", "message": "database not found"})
            _, _, _, model, method = path.split("/")
            if (model, method) == ("res.users", "context_get"):
                return httpx.Response(200, json={"uid": 7, "lang": "en_US"})
            if (model, method) == ("res.users", "read"):
                return httpx.Response(200, json=[{"id": 7, "login": self.login}])
            if (model, method) == ("res.company", "search_read"):
                return httpx.Response(200, json=self.companies)
            if (model, method) == ("ir.config_parameter", "get_param"):
                if self.db_uuid is None:
                    return httpx.Response(403, json={"name": "odoo.exceptions.AccessError", "message": "no"})
                return httpx.Response(200, json=self.db_uuid)
            return httpx.Response(404, json={"name": "NotFound", "message": path})
        if path.startswith("/xmlrpc/2/"):
            params, method = xmlrpc.client.loads(request.content)
            if method == "version":
                value = {"server_version": self.version}
            elif method == "authenticate":
                db, login, key, _ = params
                value = 7 if (db, login, key) == (self.db, self.login, self.key) else False
            elif method == "execute_kw":
                model, meth = params[3], params[4]
                if (model, meth) == ("res.company", "search_read"):
                    value = self.companies
                elif (model, meth) == ("ir.config_parameter", "get_param"):
                    value = self.db_uuid or False
                else:
                    value = False
            else:
                value = False
            return httpx.Response(200, content=xmlrpc.client.dumps((value,), methodresponse=True, allow_none=True),
                                  headers={"Content-Type": "text/xml"})
        return httpx.Response(404)


@pytest.fixture()
def odoo(monkeypatch):
    fakes: dict[str, FakeOdoo] = {"acme.odoo.com": FakeOdoo()}

    def route(request: httpx.Request) -> httpx.Response:
        fake = fakes.get(request.url.host)
        if fake is None:
            raise httpx.ConnectError("Name or service not known", request=request)
        return fake.handler(request)

    monkeypatch.setattr(odoo_probe, "check_target", lambda url: None)   # the fake hosts don't resolve
    monkeypatch.setattr(odoo_probe, "client_factory",
                        lambda: httpx.Client(transport=httpx.MockTransport(route), follow_redirects=False))
    return fakes


def connect(client, headers, **overrides):
    body = {"url": "https://acme.odoo.com", "db": "acme", "login": "bot@acme.io", "api_key": GOOD_KEY}
    body.update(overrides)
    return client.post("/api/v1/portal/odoo", json=body, headers=headers)


def account_id(client, headers) -> str:
    return client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]


def test_connect_tests_saves_and_never_shows_the_key(client, catalog, odoo):
    headers = signup(client)
    r = connect(client, headers)
    assert r.status_code == 201, r.text
    (conn,) = r.json()["connections"]
    assert conn["status"] == "connected" and conn["version"] == "19.0" and conn["api"] == "json2"
    assert [c["name"] for c in conn["companies"]] == ["Acme HQ", "Acme Gulf"]
    assert conn["services"][0]["slug"] == "biometric"
    assert GOOD_KEY not in json.dumps(client.get("/api/v1/portal/odoo", headers=headers).json())
    db = SessionLocal()
    stored = db.query(OdooConnection).one()
    assert GOOD_KEY not in stored.api_key_enc and odoo_connections.api_key(stored) == GOOD_KEY
    assert stored.db_uuid == "uuid-acme"
    assert all(GOOD_KEY not in (a.detail or "") for a in db.query(AuditLog))
    db.close()


def test_the_database_name_is_guessed_on_odoo_online(client, catalog, odoo):
    headers = signup(client)
    r = connect(client, headers, db="")
    assert r.status_code == 201, r.text
    assert r.json()["connections"][0]["db"] == "acme"


def test_a_connection_that_fails_its_test_is_not_saved(client, catalog, odoo):
    headers = signup(client)
    r = connect(client, headers, api_key="wrong-api-key-1")
    assert r.status_code == 400 and "rejected the API key" in r.json()["detail"]
    r = connect(client, headers, url="https://nowhere.example")
    assert r.status_code == 400 and "No server is known" in r.json()["detail"]
    assert client.get("/api/v1/portal/odoo", headers=headers).json()["connections"] == []


def test_a_key_for_another_user_is_refused(client, catalog, odoo):
    headers = signup(client)
    r = connect(client, headers, login="someone@acme.io")
    assert r.status_code == 400 and "belongs to the Odoo user 'bot@acme.io'" in r.json()["detail"]


def test_older_odoo_is_tested_over_xmlrpc(client, catalog, odoo):
    odoo["acme.odoo.com"] = FakeOdoo(version="17.0")
    headers = signup(client)
    r = connect(client, headers)
    assert r.status_code == 201, r.text
    conn = r.json()["connections"][0]
    assert conn["api"] == "xmlrpc" and conn["version"] == "17.0" and len(conn["companies"]) == 2
    assert connect(client, signup(client, email="b@other.io", company="Other"),
                   api_key="wrong-api-key-1").status_code == 400


def test_one_odoo_database_belongs_to_one_account(client, catalog, odoo):
    first = signup(client)
    assert connect(client, first).status_code == 201
    second = signup(client, email="eve@other.io", company="Other Ltd")
    # The same database written another way is still the same database.
    for url in ("http://acme.odoo.com/", "https://www.acme.odoo.com/odoo", "ACME.odoo.com"):
        r = connect(client, second, url=url, db="ACME")
        assert r.status_code == 400 and "another OdooBridger account" in r.json()["detail"], url
    # A different address that is the same database (same database.uuid).
    odoo["erp.acme.com"] = odoo["acme.odoo.com"]
    r = connect(client, second, url="https://erp.acme.com")
    assert r.status_code == 400 and "another OdooBridger account" in r.json()["detail"]
    # Only one connection per account for now.
    assert connect(client, first).status_code == 400


def test_only_odoo_services_are_sent_the_connection(client, catalog, odoo, service_app):
    headers = signup(client, services=[{"service": "biometric"}, {"service": "shipping"}])
    assert connect(client, headers).status_code == 201
    acc_id = account_id(client, headers)
    sent = service_app.payloads[("biometric", acc_id)]["odoo"]
    assert sent["url"] == "https://acme.odoo.com" and sent["db"] == "acme" and sent["api_key"] == GOOD_KEY
    assert sent["login"] == "bot@acme.io" and sent["api"] == "json2" and len(sent["companies"]) == 2
    assert "odoo" not in service_app.payloads[("shipping", acc_id)]


def test_changing_the_key_resends_it_and_a_bad_change_keeps_the_old_one(client, catalog, odoo, service_app):
    headers = signup(client)
    conn_id = connect(client, headers).json()["connections"][0]["id"]
    acc_id = account_id(client, headers)
    odoo["acme.odoo.com"].key = "rotated-key-456"
    r = client.patch(f"/api/v1/portal/odoo/{conn_id}", json={"api_key": "not-the-key"}, headers=headers)
    assert r.status_code == 400
    assert service_app.payloads[("biometric", acc_id)]["odoo"]["api_key"] == GOOD_KEY
    r = client.patch(f"/api/v1/portal/odoo/{conn_id}", json={"api_key": "rotated-key-456"}, headers=headers)
    assert r.status_code == 200, r.text
    assert service_app.payloads[("biometric", acc_id)]["odoo"]["api_key"] == "rotated-key-456"
    # Renaming needs no test and keeps the key.
    r = client.patch(f"/api/v1/portal/odoo/{conn_id}", json={"name": "Main Odoo"}, headers=headers)
    assert r.json()["connections"][0]["name"] == "Main Odoo"
    assert service_app.payloads[("biometric", acc_id)]["odoo"]["api_key"] == "rotated-key-456"


def test_disconnecting_tells_the_service(client, catalog, odoo, service_app):
    headers = signup(client)
    conn_id = connect(client, headers).json()["connections"][0]["id"]
    acc_id = account_id(client, headers)
    r = client.delete(f"/api/v1/portal/odoo/{conn_id}", headers=headers)
    assert r.status_code == 200 and r.json()["connections"] == []
    assert r.json()["services_without"] == [{"slug": "biometric", "name": "BioBridge"}]
    assert service_app.payloads[("biometric", acc_id)]["odoo"] is None


def test_a_service_added_later_is_bound_to_the_connection(client, catalog, odoo, service_app):
    headers = signup(client, services=[])
    assert connect(client, headers).status_code == 201
    r = client.post("/api/v1/portal/services", json={"service": "biometric"}, headers=headers)
    assert r.status_code == 201, r.text
    assert service_app.payloads[("biometric", account_id(client, headers))]["odoo"]["db"] == "acme"


def test_members_can_test_but_not_change(client, catalog, odoo):
    headers = signup(client)
    conn_id = connect(client, headers).json()["connections"][0]["id"]


    client.post("/api/v1/portal/users", json={"email": "mo@acme.io", "full_name": "Mo", "role": "member"},
                headers=headers)
    r = client.post("/api/v1/auth/link/use", json={"token": link_token("mo@acme.io"), "password": "Secret-Pass-1"})
    member = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.post(f"/api/v1/portal/odoo/{conn_id}/test", headers=member).status_code == 200
    assert client.patch(f"/api/v1/portal/odoo/{conn_id}", json={"name": "x"}, headers=member).status_code == 403
    assert client.delete(f"/api/v1/portal/odoo/{conn_id}", headers=member).status_code == 403


def test_service_readiness_is_shown_and_its_auth_failure_marks_the_connection(client, catalog, odoo, service_app):
    headers = signup(client)
    service_app.readiness = {"ready": False, "message": "The biobridge_attendance add-on is not installed.",
                             "checks": [{"name": "Add-on", "ok": False}], "auth_failed": False}
    conn = connect(client, headers).json()["connections"][0]
    assert service_app.usage_calls[-1] == {"check_odoo": "1"}
    (svc,) = conn["services"]
    assert svc["ready"] is False and "add-on" in svc["message"]

    # Later the service finds Odoo refusing the key: the connection is failed
    # and the managers are told once.
    service_app.readiness = {"ready": False, "message": "Odoo rejected the API key.", "auth_failed": True}
    db = SessionLocal()
    from obcore.services.jobs import refresh_odoo_readiness, sweep_once

    assert refresh_odoo_readiness(db, now=utcnow() + timedelta(hours=1)) == 1
    db.commit()
    row = db.query(OdooConnection).one()
    assert row.status == "failed" and "rejected" in row.status_message
    OUTBOX.clear()
    sweep_once(db)
    sweep_once(db)
    db.commit()
    db.close()
    assert [m["subject"] for m in OUTBOX] == ["Your Odoo connection stopped working"]

    # Testing again from the portal, once Odoo accepts, puts it right.
    service_app.readiness = {"ready": True, "message": "Ready.", "auth_failed": False}
    r = client.post(f"/api/v1/portal/odoo/{conn['id']}/test", headers=headers)
    assert r.json()["connections"][0]["status"] == "connected"
    assert r.json()["connections"][0]["services"][0]["ready"] is True


def test_key_expiry_is_warned_once(client, catalog, odoo):
    headers = signup(client)
    soon = (utcnow() + timedelta(days=5)).date().isoformat()
    assert connect(client, headers, key_expires_at=soon).status_code == 201
    db = SessionLocal()
    OUTBOX.clear()
    odoo_connections.notify_failures(db)
    odoo_connections.notify_failures(db)
    db.commit()
    db.close()
    assert [m["subject"] for m in OUTBOX] == ["Your Odoo API key expires soon"]


def test_staff_see_the_connection_without_the_key(client, catalog, odoo):


    headers = signup(client)
    connect(client, headers)
    detail = client.get(f"/api/v1/staff/accounts/{account_id(client, headers)}", headers=staff_headers(client))
    assert detail.json()["odoo"][0]["url"] == "https://acme.odoo.com"
    assert GOOD_KEY not in detail.text


def test_url_normalising():
    assert odoo_probe.clean_url("acme.odoo.com/odoo/action-1") == "https://acme.odoo.com"
    assert odoo_probe.url_key("http://www.Acme.odoo.com:443/") == "acme.odoo.com"
    assert odoo_probe.url_key("https://erp.acme.com:8069/") == "erp.acme.com:8069"
    assert odoo_probe.guess_db("https://acme.odoo.com") == "acme"
    assert odoo_probe.guess_db("https://erp.acme.com") is None


def test_new_columns_are_added_to_an_existing_database():
    from sqlalchemy import inspect, text

    from obcore.db import add_missing_columns, engine

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE service DROP COLUMN uses_odoo"))
        conn.execute(text("DROP INDEX IF EXISTS ix_account_service_odoo_connection_id"))
        conn.execute(text("ALTER TABLE account_service DROP COLUMN odoo_readiness"))
    add_missing_columns()
    columns = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in ("service", "account_service")}
    assert "uses_odoo" in columns["service"] and "odoo_readiness" in columns["account_service"]
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO service (id, slug, name, base_url, sso_callback_path, is_active, is_public, "
                          "sort_order, created_at, updated_at) VALUES ('s1', 'x', 'X', 'http://x', '/', 1, 1, 0, "
                          "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        assert conn.execute(text("SELECT uses_odoo FROM service WHERE id='s1'")).scalar() in (0, False)


def test_private_addresses_are_refused_unless_allowed(monkeypatch):
    from obcore.config import settings

    monkeypatch.setattr(settings, "odoo_allow_private_targets", False)
    for url in ("http://127.0.0.1:8069", "http://10.1.2.3", "http://169.254.169.254", "http://localhost:8069"):
        with pytest.raises(odoo_probe.OdooProbeError, match="private network"):
            odoo_probe.check_target(url)
    monkeypatch.setattr(settings, "odoo_allow_private_targets", True)
    odoo_probe.check_target("http://127.0.0.1:8069")
    monkeypatch.setattr(settings, "odoo_allow_private_targets", None)
    monkeypatch.setattr(settings, "environment", "production")
    assert odoo_probe.private_allowed() is False

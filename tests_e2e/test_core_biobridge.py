"""End to end: sign up at OdooBridger, open BioBridge through SSO, and watch
the core's decisions (package, suspension, deletion) arrive in BioBridge."""

from __future__ import annotations

import re

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as bb_main
import obcore.main as core_main
from app.db.base import Base as BBBase
from app.db.session import SessionLocal as BBSession
from app.db.session import engine as bb_engine
from app.models import Tenant, User
from app.services import platform_link
from obcore.mailer import OUTBOX
from obcore.services import ops, provisioning


@pytest.fixture()
def world(monkeypatch):
    BBBase.metadata.create_all(bb_engine)
    with TestClient(core_main.app, base_url="http://core.test") as core, \
            TestClient(bb_main.app, base_url="http://biobridge.test") as bb:
        # core -> BioBridge
        monkeypatch.setattr(provisioning, "client_factory", lambda: TestClient(bb_main.app, base_url="http://biobridge.test"))
        monkeypatch.setattr(ops, "client_factory", lambda: TestClient(bb_main.app, base_url="http://biobridge.test"))
        ops.forget()

        # BioBridge -> core
        def to_core(method):
            def call(url, **kwargs):
                kwargs.pop("timeout", None)
                return getattr(core, method)(url.replace("http://core.test", ""), **kwargs)
            return call
        monkeypatch.setattr(platform_link.httpx, "get", to_core("get"))
        monkeypatch.setattr(platform_link.httpx, "post", to_core("post"))
        platform_link.reset_key_cache()
        yield core, bb


def _link(email):
    for mail in reversed(OUTBOX):
        if mail["to"] == email:
            return re.search(r"token=([\w\-]+)", mail["body"]).group(1)
    raise AssertionError("no mail")


def _tenant(account_id):
    db = BBSession()
    try:
        return db.scalar(select(Tenant).where(Tenant.platform_account_id == account_id))
    finally:
        db.close()


def test_the_whole_journey(world):
    core, bb = world
    # 1. Sign up on the website with the biometric service.
    catalog = core.get("/api/v1/public/catalog").json()
    service = next(s for s in catalog["services"] if s["slug"] == "biometric")
    growth = next(p for p in service["packages"] if p["name"] == "Growth")
    r = core.post("/api/v1/public/signup", json={
        "company": "Acme Ltd", "full_name": "Olive Owner", "email": "owner@acme.io",
        "services": [{"service": "biometric", "package_id": growth["id"]}]})
    assert r.status_code == 201, r.text
    tokens = core.post("/api/v1/auth/link/use", json={"token": _link("owner@acme.io"),
                                                       "password": "Secret-Pass-1"}).json()
    customer = {"Authorization": f"Bearer {tokens['access_token']}"}
    account_id = core.get("/api/v1/portal/account", headers=customer).json()["account"]["id"]

    # 2. BioBridge was provisioned with the package's limits.
    tenant = _tenant(account_id)
    assert tenant is not None and tenant.status == "trialing"
    assert tenant.limit_max_devices == 10 and tenant.limit_max_employees == 300

    # 3. Open BioBridge from the portal: authorize at the core, finish at BioBridge.
    r = core.post("/api/v1/sso/authorize", headers=customer, json={
        "service": "biometric", "redirect_uri": "https://biometric.test/app/#/sso", "state": "s1"})
    assert r.status_code == 200, r.text
    code = re.search(r"code=([\w\-]+)", r.json()["redirect_url"]).group(1)
    r = bb.post("/api/v1/auth/platform/sso", json={"code": code})
    assert r.status_code == 200, r.text
    bb_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = bb.get("/api/v1/auth/me", headers=bb_headers).json()
    assert me["email"] == "owner@acme.io" and me["role"] == "owner"
    assert bb.get("/api/v1/tenant", headers=bb_headers).json()["platform_state"]["package"]["name"] == "Growth"
    # The code was spent.
    assert bb.post("/api/v1/auth/platform/sso", json={"code": code}).status_code == 401

    # 4. A package change in the portal reaches BioBridge.
    starter = next(p for p in service["packages"] if p["name"] == "Starter")
    r = core.patch("/api/v1/portal/services/biometric", headers=customer, json={"package_id": starter["id"]})
    assert r.status_code == 200, r.text
    assert _tenant(account_id).limit_max_devices == 2

    # 5. Staff suspend the account: BioBridge stops syncing it.
    staff = core.post("/api/v1/auth/staff/login", json={"email": "staff@odoobridger.io",
                                                         "password": "Staff-Password-1"}).json()
    staff_h = {"Authorization": f"Bearer {staff['access_token']}"}
    r = core.post(f"/api/v1/staff/accounts/{account_id}/suspend", headers=staff_h, json={"reason": "test"})
    assert r.status_code == 200
    assert _tenant(account_id).status == "suspended"
    assert bb.post("/api/v1/sync/run", headers=bb_headers).status_code == 403
    usage = core.get(f"/api/v1/staff/accounts/{account_id}/services/biometric/usage", headers=staff_h)
    assert usage.status_code == 200 and usage.json()["status"] == "suspended"
    core.post(f"/api/v1/staff/accounts/{account_id}/reactivate", headers=staff_h)
    assert _tenant(account_id).status == "trialing"

    # 6. An invited admin can open BioBridge too; removing them locks them out.
    r = core.post("/api/v1/portal/users", headers=customer,
                  json={"email": "ann@acme.io", "full_name": "Ann", "role": "admin"})
    ann_id = r.json()["id"]
    db = BBSession()
    ann = db.scalar(select(User).where(User.email == "ann@acme.io"))
    assert ann is not None and ann.role == "admin" and ann.is_active
    db.close()
    core.delete(f"/api/v1/portal/users/{ann_id}", headers=customer)
    db = BBSession()
    assert db.scalar(select(User).where(User.email == "ann@acme.io")).is_active is False
    db.close()

    # 7. The owner deletes the account: BioBridge's data goes with it.
    r = core.post("/api/v1/portal/account/delete", headers=customer, json={"confirm_name": "Acme Ltd"})
    assert r.status_code == 200, r.text
    assert _tenant(account_id) is None


def test_biobridge_refuses_its_own_signup_in_platform_mode(world):
    _, bb = world
    assert bb.post("/api/v1/auth/login", json={"email": "a@b.io", "password": "x"}).status_code == 404
    mode = bb.get("/api/v1/auth/mode").json()
    assert mode["authorize_url"] == "http://core.test/app/#/open/biometric"


def test_the_odoo_connection_is_set_in_the_core_and_used_by_biobridge(world, monkeypatch):
    from app.core.crypto import decrypt
    from app.models import OdooConnection
    from obcore.services import odoo_probe

    core, bb = world
    r = core.post("/api/v1/public/signup", json={
        "company": "Gulf Steel", "full_name": "Gus", "email": "gus@gulf.io", "services": [{"service": "biometric"}]})
    assert r.status_code == 201, r.text
    tokens = core.post("/api/v1/auth/link/use", json={"token": _link("gus@gulf.io"), "password": "Secret-Pass-1"}).json()
    customer = {"Authorization": f"Bearer {tokens['access_token']}"}
    account_id = core.get("/api/v1/portal/account", headers=customer).json()["account"]["id"]

    # The core's test (reachability, login, companies) and BioBridge's own
    # (can it write attendance) both pass against a stand-in Odoo.
    monkeypatch.setattr(odoo_probe, "probe", lambda url, db, login, key, **_kw: odoo_probe.ProbeResult(
        version="19.0", api="json2", uid=7, companies=[{"id": 1, "name": "Gulf HQ"}], db_uuid="uuid-gulf"))

    class Odoo:
        def ping(self):
            return {"uid": 7, "server_version": "19.0", "can_create_attendance": True,
                    "has_companion_addon": False, "has_device_tracking": True, "device_tracking_mode": "bootstrap"}

    monkeypatch.setattr("app.services.connections.build_odoo_client", lambda tenant, conn: Odoo())

    r = core.post("/api/v1/portal/odoo", headers=customer, json={
        "url": "https://gulf.odoo.com", "login": "bot@gulf.io", "api_key": "first-key-123"})
    assert r.status_code == 201, r.text
    (conn,) = r.json()["connections"]
    assert conn["db"] == "gulf"
    (svc,) = conn["services"]
    assert svc["slug"] == "biometric" and svc["ready"] is True, svc

    def bb_conn():
        db = BBSession()
        try:
            tenant = db.scalar(select(Tenant).where(Tenant.platform_account_id == account_id))
            row = db.scalar(select(OdooConnection).where(OdooConnection.tenant_id == tenant.id))
            return row, decrypt(row.api_key_enc, tenant.crypto_key)
        finally:
            db.close()

    row, key = bb_conn()
    assert row.managed and row.url == "https://gulf.odoo.com" and row.db_name == "gulf" and key == "first-key-123"
    assert row.status == "connected"

    # A new key in the portal reaches BioBridge.
    r = core.patch(f"/api/v1/portal/odoo/{conn['id']}", headers=customer, json={"api_key": "second-key-456"})
    assert r.status_code == 200, r.text
    assert bb_conn()[1] == "second-key-456"

    # Disconnecting in the portal stops BioBridge using it.
    core.delete(f"/api/v1/portal/odoo/{conn['id']}", headers=customer)
    row, _ = bb_conn()
    assert not row.is_active


def test_the_staff_overview_reads_biobridges_own_metrics(world):
    core, _ = world
    r = core.post("/api/v1/public/signup", json={
        "company": "Metrics Co", "full_name": "M", "email": "m@metrics.io", "services": [{"service": "biometric"}]})
    assert r.status_code == 201, r.text
    core.post("/api/v1/auth/link/use", json={"token": _link("m@metrics.io"), "password": "Secret-Pass-1"})
    staff = core.post("/api/v1/auth/staff/login", json={"email": "staff@odoobridger.io",
                                                         "password": "Staff-Password-1"}).json()
    o = core.get("/api/v1/staff/overview?fresh=true",
                 headers={"Authorization": f"Bearer {staff['access_token']}"}).json()
    bio = next(s for s in o["services"] if s["slug"] == "biometric")
    # BioBridge answered both its health check and the signed metrics call.
    assert bio["has_metrics"] is True and bio["status"] in ("operational", "degraded")
    assert set(bio["integrations"]) == {"active", "failed"}
    assert bio["customers"] >= 1

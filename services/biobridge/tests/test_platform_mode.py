"""BioBridge as OdooBridger's biometric service (PLATFORM_MODE=true).

The core is simulated by signing tokens with a throwaway RSA key whose public
half is BioBridge's PLATFORM_PUBLIC_KEY_PEM. The real core ↔ BioBridge round
trip is covered by the monorepo's end-to-end test (tests_e2e/).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import Tenant, User
from app.services import platform_link

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
PUBLIC = _KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
ACCOUNT = "a" * 32


def token(typ: str, claims: dict, *, aud: str = "biometric", key: str = PRIVATE) -> str:
    now = int(time.time())
    return jwt.encode({"iss": "https://core.test", "aud": aud, "typ": typ, "iat": now, "exp": now + 120,
                       "jti": uuid.uuid4().hex, **claims}, key, algorithm="RS256")


def state(**over) -> dict:
    base = {
        "account": {"id": ACCOUNT, "name": "Acme Ltd", "slug": "acme", "timezone": "Asia/Kolkata"},
        "status": "trialing",
        "package": {"id": "p1", "name": "Starter"},
        "entitlements": {"max_devices": 2, "max_employees": 50, "min_sync_interval_minutes": 15},
        "trial_ends_at": "2026-11-04T00:00:00+00:00", "renews_at": None,
        "users": [{"id": "u" * 32, "email": "owner@acme.com", "full_name": "Olive", "role": "owner"}],
        "revision": 1,
    }
    base.update(over)
    return base


def put(client, body: dict, *, account_id: str = ACCOUNT, tamper: bool = False, **token_over):
    raw = json.dumps(body).encode()
    digest = hashlib.sha256(raw if not tamper else raw + b" ").hexdigest()
    bearer = token("provision", {"sub": "odoobridger-core", "account_id": account_id,
                                 "body_sha256": digest}, **token_over)
    return client.put(f"/platform/v1/accounts/{account_id}", content=raw,
                      headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", True)
    monkeypatch.setattr(settings, "platform_public_key_pem", PUBLIC)
    monkeypatch.setattr(settings, "platform_core_url", "https://core.test")
    monkeypatch.setattr(settings, "platform_client_secret", "s3cret")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        c.Session = Session
        yield c
    app.dependency_overrides.clear()


def _tenant(client) -> Tenant:
    db = client.Session()
    tenant = db.scalar(select(Tenant).where(Tenant.platform_account_id == ACCOUNT))
    db.expunge_all()
    db.close()
    return tenant


def test_provisioning_creates_the_tenant_with_package_limits(client):
    r = put(client, state())
    assert r.status_code == 200, r.text
    tenant = _tenant(client)
    assert tenant.name == "Acme Ltd" and tenant.status == "trialing"
    assert (tenant.limit_max_devices, tenant.limit_max_employees, tenant.limit_min_sync_interval_minutes) == (2, 50, 15)
    assert tenant.plan_id is None and tenant.subscription_renews_at is None
    assert tenant.sync_interval_minutes >= 15
    assert tenant.platform_state["package"]["name"] == "Starter"


def test_unlimited_entitlements_become_no_limit(client):
    put(client, state(entitlements={"max_devices": None, "max_employees": None}, revision=2))
    tenant = _tenant(client)
    assert tenant.limit_max_devices == 0 and tenant.limit_max_employees == 0   # 0 = no limit
    assert tenant.limit_min_sync_interval_minutes is None


def test_status_changes_stop_syncing(client):
    put(client, state())
    put(client, state(status="past_due", revision=2))
    tenant = _tenant(client)
    assert tenant.status == "past_due" and not tenant.syncable


def test_an_older_revision_does_not_undo_a_newer_one(client):
    put(client, state(status="active", revision=5))
    put(client, state(status="suspended", revision=4))
    assert _tenant(client).status == "active"


def test_tokens_are_checked(client):
    assert put(client, state(), tamper=True).status_code == 403
    assert put(client, state(), aud="shipping").status_code == 401
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    assert put(client, state(), key=other).status_code == 401
    raw = json.dumps(state()).encode()
    bearer = token("identity", {"account_id": ACCOUNT, "body_sha256": hashlib.sha256(raw).hexdigest()})
    r = client.put(f"/platform/v1/accounts/{ACCOUNT}", content=raw, headers={"Authorization": f"Bearer {bearer}"})
    assert r.status_code == 401   # an identity token is not a provisioning token
    assert client.put(f"/platform/v1/accounts/{ACCOUNT}", content=raw).status_code == 401
    assert _tenant(client) is None


def test_users_follow_the_core(client):
    people = [{"id": "u" * 32, "email": "owner@acme.com", "full_name": "Olive", "role": "owner"},
              {"id": "m" * 32, "email": "mo@acme.com", "full_name": "Mo", "role": "member"}]
    put(client, state(users=people))
    db = client.Session()
    roles = {u.email: (u.role, u.is_active) for u in db.scalars(select(User))}
    assert roles == {"owner@acme.com": ("owner", True), "mo@acme.com": ("viewer", True)}
    db.close()
    put(client, state(users=people[:1], revision=2))
    db = client.Session()
    assert db.scalar(select(User).where(User.email == "mo@acme.com")).is_active is False
    db.close()


def _sso(client, monkeypatch, claims_over: dict | None = None):
    claims = {"sub": "u" * 32, "email": "owner@acme.com", "name": "Olive", "role": "owner",
              "account": state()["account"], "status": "trialing", "package": {"id": "p1", "name": "Starter"},
              "entitlements": {"max_devices": 2}, "trial_ends_at": None, "renews_at": None, "revision": 1,
              **(claims_over or {})}
    seen = {}

    class Response:
        status_code = 200

        def json(self):
            return {"id_token": token("identity", claims)}

    def fake_post(url, json=None, timeout=None):
        seen.update(url=url, body=json)
        return Response()

    monkeypatch.setattr(platform_link.httpx, "post", fake_post)
    r = client.post("/api/v1/auth/platform/sso", json={"code": "one-time"})
    return r, seen


def test_sso_signs_in_and_creates_the_tenant_on_first_visit(client, monkeypatch):
    r, seen = _sso(client, monkeypatch)
    assert r.status_code == 200, r.text
    assert seen["url"] == "https://core.test/api/v1/sso/token"
    assert seen["body"] == {"client_id": "biometric", "client_secret": "s3cret", "code": "one-time"}
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    tenant = client.get("/api/v1/tenant", headers=headers).json()
    assert tenant["name"] == "Acme Ltd"
    me = client.get("/api/v1/auth/me", headers=headers).json()
    assert me["email"] == "owner@acme.com" and me["role"] == "owner"


def test_sso_refuses_an_email_that_belongs_to_another_tenant(client, monkeypatch):
    db = client.Session()
    other = Tenant(name="Other", slug="other")
    db.add(other)
    db.flush()
    db.add(User(tenant_id=other.id, email="owner@acme.com", hashed_password="x"))
    db.commit()
    db.close()
    r, _ = _sso(client, monkeypatch)
    assert r.status_code == 401 and "another BioBridge account" in r.json()["detail"]


def test_standalone_doors_are_closed(client):
    assert client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": "x"}).status_code == 404
    assert client.post("/api/v1/auth/signup", json={}).status_code == 404
    assert client.get("/api/v1/public/plans").status_code == 404
    assert client.get("/api/v1/billing").status_code == 404
    mode = client.get("/api/v1/auth/mode").json()
    assert mode["platform_mode"] is True
    assert mode["authorize_url"] == "https://core.test/app/#/open/biometric"


def test_plan_changes_are_refused_in_platform_mode(client, monkeypatch):
    r, _ = _sso(client, monkeypatch)
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    refused = client.patch("/api/v1/tenant", headers=headers, json={"plan_id": "x"})
    assert refused.status_code == 400 and "OdooBridger" in refused.json()["detail"]
    assert client.patch("/api/v1/tenant", headers=headers, json={"timezone": "UTC"}).status_code == 200


def test_delete_and_usage(client):
    put(client, state())
    bearer = token("provision", {"account_id": ACCOUNT, "body_sha256": hashlib.sha256(b"").hexdigest()})
    usage = client.get(f"/platform/v1/accounts/{ACCOUNT}/usage", headers={"Authorization": f"Bearer {bearer}"})
    assert usage.status_code == 200 and usage.json()["devices"] == 0
    r = client.delete(f"/platform/v1/accounts/{ACCOUNT}", headers={"Authorization": f"Bearer {bearer}"})
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert _tenant(client) is None


def test_platform_endpoints_are_off_in_standalone_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", False)
    assert put(client, state()).status_code == 404
    assert client.get("/api/v1/auth/mode").json() == {"platform_mode": False}

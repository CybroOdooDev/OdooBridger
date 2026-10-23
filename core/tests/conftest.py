"""Core test fixtures: a throwaway database, a fake Stripe, a fake service."""

from __future__ import annotations

import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="ob-core-"), "core.db")
os.environ["RUN_BACKGROUND_JOBS"] = "false"
os.environ["STRIPE_SECRET_KEY"] = ""
os.environ["SMTP_HOST"] = ""
os.environ["BIOMETRIC_CLIENT_SECRET"] = ""
for _var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_var, None)

import itertools
import re
import time
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from jose import jwt

from obcore import keys
from obcore.db import Base, engine
from obcore.main import app
from obcore.mailer import OUTBOX
from obcore.models import Service, ServicePackage, StaffUser
from obcore.security import hash_password, hash_token
from obcore.services import accounts as acc
from obcore.services import provisioning
from obcore.services import stripe_api

SERVICE_SECRET = "service-secret-for-tests"
STAFF_PASSWORD = "Staff-Password-1"


# --------------------------------------------------------------------------- #
# A fake service: records what the core provisions, verifying every token.
# --------------------------------------------------------------------------- #
class FakeService:
    def __init__(self):
        self.accounts: dict[str, dict] = {}
        self.deleted: list[str] = []
        #: Every service's last payload, by (service slug, account id).
        self.payloads: dict[tuple[str, str], dict] = {}
        #: What the usage endpoint reports as the service's Odoo readiness.
        self.readiness: dict | None = None
        self.usage_calls: list[dict] = []
        self.down = False
        self.app = FastAPI()

        @self.app.put("/platform/v1/accounts/{account_id}")
        async def put(account_id: str, request: Request):
            body = await request.body()
            aud = self._verify(request, account_id, body)
            if self.down:
                return _error(503)
            self.payloads[(aud, account_id)] = await request.json()
            if aud == "biometric":
                self.accounts[account_id] = await request.json()
            return {"ok": True}

        @self.app.delete("/platform/v1/accounts/{account_id}")
        async def delete(account_id: str, request: Request):
            aud = self._verify(request, account_id, b"")
            if self.down:
                return _error(503)
            if aud == "biometric":
                self.accounts.pop(account_id, None)
                self.deleted.append(account_id)
            return {"ok": True}

        @self.app.get("/platform/v1/accounts/{account_id}/usage")
        async def usage(account_id: str, request: Request):
            self._verify(request, account_id, b"")
            self.usage_calls.append(dict(request.query_params))
            out = {"devices": 3, "employees": 12}
            if self.readiness is not None:
                out["odoo"] = self.readiness
            return out

    def _verify(self, request: Request, account_id: str, body: bytes) -> str:
        token = request.headers["authorization"].split()[1]
        aud = jwt.get_unverified_claims(token)["aud"]
        assert aud in ("biometric", "shipping")
        claims = jwt.decode(token, keys.public_pem(), algorithms=["RS256"], audience=aud)
        assert claims["typ"] == "provision"
        assert claims["account_id"] == account_id
        assert claims["body_sha256"] == keys.body_digest(body)
        return aud


def _error(code: int):
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": "service unavailable"}, status_code=code)


# --------------------------------------------------------------------------- #
# A fake Stripe, enough of the API for subscriptions with several items.
# --------------------------------------------------------------------------- #
class FakeStripe:
    def __init__(self):
        self.ids = itertools.count(1)
        self.customers: dict[str, dict] = {}
        self.subscriptions: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def _id(self, prefix: str) -> str:
        return f"{prefix}_{next(self.ids)}"

    def request(self, method: str, path: str, data: dict | None = None, idempotency_key=None) -> dict:
        data = data or {}
        self.calls.append((method, path, data))
        if method == "POST" and path == "customers":
            cid = self._id("cus")
            self.customers[cid] = {"id": cid, **data}
            return self.customers[cid]
        if method == "GET" and path.startswith("customers/"):
            return {"id": path.split("/")[1], "invoice_settings": {"default_payment_method": {
                "type": "card", "card": {"brand": "visa", "last4": "4242", "exp_month": 1, "exp_year": 2030}}}}
        if method == "POST" and path == "checkout/sessions":
            sid = self._id("cs")
            self.sessions[sid] = {"id": sid, **data}
            return {"id": sid, "url": f"https://checkout.stripe.test/{sid}"}
        if method == "POST" and path == "billing_portal/sessions":
            return {"url": "https://billing.stripe.test/portal"}
        if path.startswith("subscriptions/"):
            sub = self.subscriptions[path.split("/")[1]]
            if method == "POST":
                if "cancel_at_period_end" in data:
                    sub["cancel_at_period_end"] = data["cancel_at_period_end"]
            if method == "DELETE":
                sub["status"] = "canceled"
                sub["items"]["data"] = []
            return sub
        if method == "POST" and path == "subscription_items":
            sub = self.subscriptions[data["subscription"]]
            item = {"id": self._id("si"), "price": {"id": data["price"], "recurring": {"interval": "month"}}}
            sub["items"]["data"].append(item)
            return item
        m = re.fullmatch(r"subscription_items/(si_\d+)", path)
        if m:
            for sub in self.subscriptions.values():
                for item in sub["items"]["data"]:
                    if item["id"] == m.group(1):
                        if method == "DELETE":
                            sub["items"]["data"].remove(item)
                            return {"id": item["id"], "deleted": True}
                        item["price"] = {"id": data["price"], "recurring": {"interval": "month"}}
                        return item
            raise stripe_api.BillingError("Stripe: No such subscription item")
        if method == "GET" and path == "invoices":
            return {"data": [{"id": "in_1", "status": "paid", "amount_paid": 2900, "currency": "usd",
                              "created": int(time.time())}]}
        if method == "GET" and path == "balance":
            return {"livemode": False}
        raise AssertionError(f"FakeStripe does not handle {method} {path}")

    def complete_checkout(self, session_id: str, interval: str = "month") -> dict:
        """What paying in Checkout does: a subscription with the session's items."""
        session = self.sessions[session_id]
        sid = self._id("sub")
        self.subscriptions[sid] = {
            "id": sid, "status": "active", "cancel_at_period_end": False,
            "current_period_end": int(time.time()) + 30 * 86400,
            "items": {"data": [{"id": self._id("si"),
                                "price": {"id": li["price"], "recurring": {"interval": interval}}}
                               for li in session["line_items"]]},
        }
        return {"id": f"evt_{sid}", "type": "checkout.session.completed", "data": {"object": {
            "mode": "subscription", "subscription": sid, "customer": session["customer"],
            "client_reference_id": session["client_reference_id"]}}}


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    keys.reset_cache()
    OUTBOX.clear()
    acc.reset_rate_limits()
    yield


@pytest.fixture()
def service_app(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(provisioning, "client_factory", lambda: TestClient(fake.app, base_url="http://biometric.test"))
    return fake


@pytest.fixture()
def fake_stripe(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(stripe_api, "request", fake.request)
    monkeypatch.setattr(stripe_api, "secret_key", lambda: "sk_test_fake")
    monkeypatch.setattr(stripe_api, "webhook_secret", lambda: "whsec_test")
    return fake


@pytest.fixture()
def client(service_app):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def catalog(client):
    from obcore.db import SessionLocal

    db = SessionLocal()
    service = Service(slug="biometric", name="Biometric Attendance", base_url="https://biometric.test",
                      internal_url="http://biometric.test", client_secret_hash=hash_token(SERVICE_SECRET),
                      uses_odoo=True)
    db.add(service)
    db.flush()
    starter = ServicePackage(service_id=service.id, name="Starter", monthly_price_cents=2900,
                             yearly_price_cents=29000, stripe_price_id="price_starter_m",
                             stripe_yearly_price_id="price_starter_y", is_default=True, trial_days=14,
                             entitlements={"max_devices": 2, "max_employees": 50})
    growth = ServicePackage(service_id=service.id, name="Growth", monthly_price_cents=7900,
                            stripe_price_id="price_growth_m", trial_days=14,
                            entitlements={"max_devices": 10, "max_employees": 300})
    other = Service(slug="shipping", name="Shipping", base_url="https://shipping.test",
                    client_secret_hash=hash_token("ship-secret"))
    db.add_all([starter, growth, other])
    db.flush()
    ship = ServicePackage(service_id=other.id, name="Basic", monthly_price_cents=1900,
                          stripe_price_id="price_ship_m", is_default=True)
    db.add(ship)
    db.add(StaffUser(email="staff@odoobridger.io", full_name="Sam Staff",
                     hashed_password=hash_password(STAFF_PASSWORD)))
    db.commit()
    ids = {"service": service.id, "starter": starter.id, "growth": growth.id, "shipping": other.id,
           "ship_basic": ship.id}
    db.close()
    return ids


def link_token(email: str) -> str:
    for mail in reversed(OUTBOX):
        if mail["to"] == email:
            m = re.search(r"token=([A-Za-z0-9_\-]+)", mail["body"])
            if m:
                return m.group(1)
    raise AssertionError(f"no link mailed to {email}")


def signup(client, email="owner@acme.io", company="Acme Ltd", services=None) -> dict[str, Any]:
    """Sign up from the website, set the password from the email, return auth headers."""
    r = client.post("/api/v1/public/signup", json={
        "company": company, "full_name": "Olive Owner", "email": email,
        "services": services if services is not None else [{"service": "biometric"}]})
    assert r.status_code == 201, r.text
    r = client.post("/api/v1/auth/link/use", json={"token": link_token(email), "password": "Secret-Pass-1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def staff_headers(client) -> dict[str, str]:
    r = client.post("/api/v1/auth/staff/login", json={"email": "staff@odoobridger.io", "password": STAFF_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}

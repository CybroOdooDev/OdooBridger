"""Signup, the two sign-in doors, SSO into a service, and provisioning."""

from __future__ import annotations

from jose import jwt

from obcore import keys
from obcore.mailer import OUTBOX
from tests.conftest import SERVICE_SECRET, link_token, signup, staff_headers


def test_signup_emails_a_set_password_link_not_a_password(client, catalog):
    r = client.post("/api/v1/public/signup", json={
        "company": "Acme Ltd", "full_name": "Olive Owner", "email": "Owner@Acme.io",
        "services": [{"service": "biometric"}]})
    assert r.status_code == 201
    mail = OUTBOX[-1]
    assert mail["to"] == "owner@acme.io"
    assert "set-password?token=" in mail["body"]
    assert "password:" not in mail["body"].lower()
    # No password yet: sign-in explains what to do.
    r = client.post("/api/v1/auth/login", json={"email": "owner@acme.io", "password": "whatever"})
    assert r.status_code == 403 and "Confirm your email" in r.json()["detail"]


def test_link_sets_password_signs_in_and_is_single_use(client, catalog):
    client.post("/api/v1/public/signup", json={"company": "Acme", "full_name": "O", "email": "o@acme.io"})
    token = link_token("o@acme.io")
    assert client.post("/api/v1/auth/link/check", json={"token": token}).json()["account"] == "Acme"
    weak = client.post("/api/v1/auth/link/use", json={"token": token, "password": "short"})
    assert weak.status_code == 422
    r = client.post("/api/v1/auth/link/use", json={"token": token, "password": "Secret-Pass-1"})
    assert r.status_code == 200 and r.json()["scope"] == "customer"
    again = client.post("/api/v1/auth/link/use", json={"token": token, "password": "Secret-Pass-2"})
    assert again.status_code == 400
    login = client.post("/api/v1/auth/login", json={"email": "o@acme.io", "password": "Secret-Pass-1"})
    assert login.status_code == 200


def test_signup_starts_a_trial_and_provisions_the_service(client, catalog, service_app):
    headers = signup(client)
    account = client.get("/api/v1/portal/account", headers=headers).json()["account"]
    (row,) = account["services"]
    assert row["status"] == "trialing" and row["package"]["name"] == "Starter"
    sent = service_app.accounts[account["id"]]
    assert sent["status"] == "trialing"
    assert sent["entitlements"] == {"max_devices": 2, "max_employees": 50}
    assert sent["users"][0]["email"] == "owner@acme.io"


def test_doors_do_not_cross(client, catalog):
    customer = signup(client)
    staff = staff_headers(client)
    assert client.get("/api/v1/staff/overview", headers=customer).status_code == 403
    assert client.get("/api/v1/portal/account", headers=staff).status_code == 403
    # Customer credentials at the staff door are simply wrong.
    r = client.post("/api/v1/auth/staff/login", json={"email": "owner@acme.io", "password": "Secret-Pass-1"})
    assert r.status_code == 401


def test_sso_code_exchanges_once_for_a_signed_identity(client, catalog):
    headers = signup(client)
    r = client.post("/api/v1/sso/authorize", headers=headers, json={
        "service": "biometric", "redirect_uri": "https://biometric.test/app/#/sso", "state": "xyz"})
    assert r.status_code == 200, r.text
    url = r.json()["redirect_url"]
    assert url.startswith("https://biometric.test/app/#/sso?code=") and "state=xyz" in url
    code = url.split("code=")[1].split("&")[0]

    bad = client.post("/api/v1/sso/token", json={"client_id": "biometric", "client_secret": "nope", "code": code})
    assert bad.status_code == 400
    ok = client.post("/api/v1/sso/token", json={"client_id": "biometric", "client_secret": SERVICE_SECRET, "code": code})
    assert ok.status_code == 200, ok.text
    claims = jwt.decode(ok.json()["id_token"], keys.public_pem(), algorithms=["RS256"], audience="biometric")
    assert claims["typ"] == "identity"
    assert claims["email"] == "owner@acme.io" and claims["role"] == "owner"
    assert claims["status"] == "trialing" and claims["entitlements"]["max_devices"] == 2
    assert claims["account"]["name"] == "Acme Ltd"
    replay = client.post("/api/v1/sso/token", json={"client_id": "biometric", "client_secret": SERVICE_SECRET, "code": code})
    assert replay.status_code == 400 and "already used" in replay.json()["detail"]


def test_sso_refuses_foreign_redirects_and_services_not_on_the_account(client, catalog):
    headers = signup(client)
    evil = client.post("/api/v1/sso/authorize", headers=headers,
                       json={"service": "biometric", "redirect_uri": "https://evil.test/steal"})
    assert evil.status_code == 400
    other = client.post("/api/v1/sso/authorize", headers=headers, json={"service": "shipping"})
    assert other.status_code == 400 and "does not have" in other.json()["detail"]


def test_a_code_for_one_service_cannot_be_redeemed_by_another(client, catalog):
    headers = signup(client, services=[{"service": "biometric"}, {"service": "shipping"}])
    url = client.post("/api/v1/sso/authorize", headers=headers, json={"service": "biometric"}).json()["redirect_url"]
    code = url.split("code=")[1].split("&")[0]
    r = client.post("/api/v1/sso/token", json={"client_id": "shipping", "client_secret": "ship-secret", "code": code})
    assert r.status_code == 400


def test_a_service_that_is_down_is_retried_by_the_sweep(client, catalog, service_app):
    service_app.down = True
    headers = signup(client)
    account_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
    assert account_id not in service_app.accounts
    service_app.down = False
    from obcore.db import SessionLocal
    from obcore.services.jobs import sweep_once

    db = SessionLocal()
    assert sweep_once(db)["provisioned"] == 1
    db.commit()
    db.close()
    assert service_app.accounts[account_id]["status"] == "trialing"


def test_suspending_an_account_tells_the_service(client, catalog, service_app):
    headers = signup(client)
    account_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
    staff = staff_headers(client)
    r = client.post(f"/api/v1/staff/accounts/{account_id}/suspend", headers=staff, json={"reason": "unpaid"})
    assert r.status_code == 200
    assert service_app.accounts[account_id]["status"] == "suspended"
    client.post(f"/api/v1/staff/accounts/{account_id}/reactivate", headers=staff)
    assert service_app.accounts[account_id]["status"] == "trialing"


def test_invited_user_reaches_the_service_user_list(client, catalog, service_app):
    headers = signup(client)
    r = client.post("/api/v1/portal/users", headers=headers,
                    json={"email": "ann@acme.io", "full_name": "Ann", "role": "admin"})
    assert r.status_code == 201, r.text
    account_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
    emails = {u["email"] for u in service_app.accounts[account_id]["users"]}
    assert emails == {"owner@acme.io", "ann@acme.io"}
    assert "set-password?token=" in OUTBOX[-1]["body"] and "invited you" in OUTBOX[-1]["body"]


def test_members_cannot_manage(client, catalog):
    headers = signup(client)
    client.post("/api/v1/portal/users", headers=headers, json={"email": "m@acme.io", "role": "member"})
    r = client.post("/api/v1/auth/link/use", json={"token": link_token("m@acme.io"), "password": "Secret-Pass-9"})
    member = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.post("/api/v1/portal/services", headers=member, json={"service": "shipping"}).status_code == 403
    assert client.get("/api/v1/portal/account", headers=member).status_code == 200


def test_owner_deletes_account_and_every_service_forgets_it(client, catalog, service_app):
    headers = signup(client)
    account_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
    wrong = client.post("/api/v1/portal/account/delete", headers=headers, json={"confirm_name": "nope"})
    assert wrong.status_code == 400
    r = client.post("/api/v1/portal/account/delete", headers=headers, json={"confirm_name": "Acme Ltd"})
    assert r.status_code == 200, r.text
    assert service_app.deleted == [account_id]
    closures = client.get("/api/v1/staff/closures", headers=staff_headers(client)).json()
    assert closures[0]["account_name"] == "Acme Ltd" and closures[0]["services"] == ["biometric"]


def test_deletion_stops_if_a_service_cannot_delete_its_data(client, catalog, service_app):
    headers = signup(client)
    service_app.down = True
    r = client.post("/api/v1/portal/account/delete", headers=headers, json={"confirm_name": "Acme Ltd"})
    assert r.status_code == 502
    assert client.get("/api/v1/portal/account", headers=headers).status_code == 200


def test_refresh_rotates_and_logout_ends_the_session(client, catalog):
    signup(client)
    tokens = client.post("/api/v1/auth/login", json={"email": "owner@acme.io", "password": "Secret-Pass-1"}).json()
    new = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert new.status_code == 200
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    client.post("/api/v1/auth/logout", json={"refresh_token": new.json()["refresh_token"]})
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": new.json()["refresh_token"]}).status_code == 401


def test_jwks_matches_the_signing_key(client, catalog):
    jwk = client.get("/.well-known/jwks.json").json()["keys"][0]
    token = keys.sign_service_token("biometric", "identity", {"sub": "x"})
    claims = jwt.decode(token, jwk, algorithms=["RS256"], audience="biometric")
    assert claims["sub"] == "x"


def test_pages_are_served(client):
    for path in ("/", "/site/signup.html", "/site/contact.html", "/site/contact.js", "/site/terms.html", "/site/privacy.html", "/app/", "/staff/", "/assets/app.css", "/assets/shell.js",
                 "/app/app.js", "/staff/app.js", "/staff/leads.js", "/site/site.js"):
        assert client.get(path).status_code == 200, path


def test_demo_service_is_registered_only_when_its_secret_is_set(client, monkeypatch):
    from sqlalchemy import select

    from obcore.config import settings
    from obcore.db import SessionLocal
    from obcore.models import Service
    from obcore.services.catalog import ensure_demo

    db = SessionLocal()
    monkeypatch.setattr(settings, "demo_client_secret", "")
    assert ensure_demo(db) is None
    monkeypatch.setattr(settings, "demo_client_secret", "s3cret")
    svc = ensure_demo(db)
    db.commit()
    assert svc.slug == "demo" and svc.sso_callback_path == "/sso"
    assert db.scalar(select(Service).where(Service.slug == "demo")).packages[0].entitlements == {"max_seats": 3}
    db.close()


def test_contact_page_details_come_from_settings(client, monkeypatch):
    from obcore.config import settings

    monkeypatch.setattr(settings, "contact_email", "")
    monkeypatch.setattr(settings, "sales_email", "sales@acme.io")
    monkeypatch.setattr(settings, "contact_phone", "+91 1")
    info = client.get("/api/v1/public/site").json()
    assert info["email"] == "sales@acme.io" and info["phone"] == "+91 1" and info["company"]
    assert set(info) == {"company", "email", "phone", "address", "hours", "website"}


def test_buy_now_goes_to_checkout_once_the_email_is_confirmed(client, catalog, fake_stripe):
    r = client.post("/api/v1/public/signup", json={
        "company": "Buyer Ltd", "full_name": "Bea Buyer", "email": "bea@buyer.io", "mode": "buy",
        "interval": "year", "services": [{"service": "biometric"}]})
    assert r.status_code == 201
    assert "checkout_url" not in r.json() and "payment" in r.json()["message"]
    assert not any(path == "checkout/sessions" for _, path, _ in fake_stripe.calls)
    r = client.post("/api/v1/auth/link/use", json={"token": link_token("bea@buyer.io"), "password": "Secret-Pass-1"})
    assert r.status_code == 200, r.text
    assert r.json()["checkout_url"].startswith("https://checkout.stripe.test/") and r.json()["access_token"]


def test_buy_now_without_stripe_or_yearly_price_is_refused_before_creating_anything(client, catalog):
    body = {"company": "Buyer Ltd", "full_name": "Bea Buyer", "email": "bea@buyer.io", "mode": "buy",
            "services": [{"service": "biometric"}]}
    r = client.post("/api/v1/public/signup", json=body)
    assert r.status_code == 409 and "free trial" in r.json()["detail"].lower()


def test_buy_now_yearly_needs_a_yearly_price(client, catalog, fake_stripe):
    body = {"company": "Buyer Ltd", "full_name": "Bea Buyer", "email": "bea@buyer.io", "mode": "buy",
            "services": [{"service": "biometric"}]}
    assert client.post("/api/v1/public/signup", json={**body, "interval": "year",
        "services": [{"service": "biometric", "package_id": catalog["growth"]}]}).status_code == 409



# --- nothing exists until the email is confirmed -----------------------------------
def _count(model) -> int:
    from obcore.db import SessionLocal

    db = SessionLocal()
    try:
        return db.query(model).count()
    finally:
        db.close()


def test_signup_creates_nothing_until_the_link_is_used(client, catalog, service_app):
    from obcore.models import Account, AccountService, PendingSignup, User

    r = client.post("/api/v1/public/signup", json={
        "company": "Acme Ltd", "full_name": "Olive Owner", "email": "Owner@Acme.io",
        "services": [{"service": "biometric", "package_id": catalog["growth"]}]})
    assert r.status_code == 201
    assert (_count(Account), _count(User), _count(AccountService), _count(PendingSignup)) == (0, 0, 0, 1)
    assert service_app.accounts == {}
    # The link shows who is signing up, and still creates nothing.
    token = link_token("owner@acme.io")
    info = client.post("/api/v1/auth/link/check", json={"token": token}).json()
    assert info["email"] == "owner@acme.io" and info["account"] == "Acme Ltd" and info["signup"] is True
    assert _count(Account) == 0
    # Signing in before confirming explains what to do.
    r = client.post("/api/v1/auth/login", json={"email": "owner@acme.io", "password": "Secret-Pass-1"})
    assert r.status_code == 403 and "Confirm your email" in r.json()["detail"]

    r = client.post("/api/v1/auth/link/use", json={"token": token, "password": "Secret-Pass-1"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    account = client.get("/api/v1/portal/account", headers=headers).json()["account"]
    (row,) = account["services"]
    assert row["status"] == "trialing" and row["package"]["name"] == "Growth"
    assert (_count(Account), _count(User), _count(PendingSignup)) == (1, 1, 0)
    assert service_app.accounts[account["id"]]["status"] == "trialing"
    # The link is spent.
    assert client.post("/api/v1/auth/link/use", json={"token": token, "password": "Secret-Pass-1"}).status_code == 400
    assert client.post("/api/v1/auth/login", json={"email": "owner@acme.io", "password": "Secret-Pass-1"}).status_code == 200


def test_signing_up_again_replaces_the_unconfirmed_signup(client, catalog):
    from obcore.models import PendingSignup

    body = {"company": "Acme Ltd", "full_name": "Olive", "email": "owner@acme.io", "services": [{"service": "biometric"}]}
    client.post("/api/v1/public/signup", json=body)
    first = link_token("owner@acme.io")
    client.post("/api/v1/public/signup", json={**body, "company": "Acme Group"})
    second = link_token("owner@acme.io")
    assert first != second and _count(PendingSignup) == 1
    assert client.post("/api/v1/auth/link/check", json={"token": first}).status_code == 400
    assert client.post("/api/v1/auth/link/check", json={"token": second}).json()["account"] == "Acme Group"
    # Resend: a new link, the old one stops working.
    client.post("/api/v1/public/resend", json={"email": "owner@acme.io"})
    assert link_token("owner@acme.io") != second
    assert client.post("/api/v1/auth/link/check", json={"token": second}).status_code == 400


def test_an_unconfirmed_signup_expires_and_is_cleared(client, catalog):
    from datetime import timedelta

    from obcore.db import SessionLocal
    from obcore.models import Account, PendingSignup
    from obcore.security import utcnow
    from obcore.services.jobs import sweep_once

    client.post("/api/v1/public/signup", json={"company": "Acme Ltd", "full_name": "O", "email": "owner@acme.io",
                                               "services": [{"service": "biometric"}]})
    token = link_token("owner@acme.io")
    db = SessionLocal()
    db.query(PendingSignup).one().expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    assert client.post("/api/v1/auth/link/use", json={"token": token, "password": "Secret-Pass-1"}).status_code == 400
    sweep_once(db)
    db.commit()
    db.close()
    assert (_count(PendingSignup), _count(Account)) == (0, 0)


def test_a_weak_password_creates_nothing(client, catalog):
    from obcore.models import Account, PendingSignup

    client.post("/api/v1/public/signup", json={"company": "Acme Ltd", "full_name": "O", "email": "owner@acme.io",
                                               "services": [{"service": "biometric"}]})
    r = client.post("/api/v1/auth/link/use", json={"token": link_token("owner@acme.io"), "password": "short"})
    assert r.status_code == 422
    assert (_count(Account), _count(PendingSignup)) == (0, 1)

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
    for path in ("/", "/site/signup.html", "/site/terms.html", "/site/privacy.html", "/app/", "/staff/", "/assets/app.css", "/assets/shell.js",
                 "/app/app.js", "/staff/app.js", "/staff/leads.js", "/site/site.js"):
        assert client.get(path).status_code == 200, path

"""The customer's reports in the portal: billing, plan & usage, activity."""

from __future__ import annotations

from tests.conftest import link_token, signup, staff_headers


def test_usage_shows_each_service_against_its_limits(client, catalog, service_app):
    headers = signup(client)
    r = client.get("/api/v1/portal/reports/usage", headers=headers)
    assert r.status_code == 200, r.text
    (svc,) = r.json()["services"]
    assert svc["slug"] == "biometric" and svc["status"] == "trialing"
    limits = {l["label"]: l for l in svc["limits"]}
    assert limits["Employees"]["used"] == 12 and limits["Employees"]["limit"] == 50
    assert limits["Employees"]["percent"] == 24
    assert limits["Terminals"]["used"] == 3 and limits["Terminals"]["limit"] == 2
    assert limits["Terminals"]["percent"] == 150


def test_billing_report_without_online_payment_is_empty_not_an_error(client, catalog):
    headers = signup(client)
    d = client.get("/api/v1/portal/reports/billing", headers=headers).json()
    assert d["invoices"] == [] and d["totals"]["paid_cents"] == 0 and d["pays_online"] is False


def test_billing_report_totals_invoices(client, catalog, fake_stripe):
    headers = signup(client)
    client.post("/api/v1/portal/billing/checkout", headers=headers, json={"interval": "month"})
    from obcore.db import SessionLocal
    from obcore.models import Account

    db = SessionLocal()
    for a in db.query(Account):
        a.stripe_customer_id = a.stripe_customer_id or "cus_x"
    db.commit()
    db.close()
    d = client.get("/api/v1/portal/reports/billing", headers=headers).json()
    assert d["totals"]["invoices"] == 1 and d["totals"]["paid_cents"] == 2900 and d["totals"]["outstanding_cents"] == 0


def test_activity_lists_the_accounts_own_events_and_hides_staff(client, catalog):
    headers = signup(client)
    client.patch("/api/v1/portal/account", headers=headers, json={"name": "Acme Holdings"})
    account_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
    staff = staff_headers(client)
    client.post(f"/api/v1/staff/accounts/{account_id}/suspend", headers=staff, json={"reason": "secret staff note"})

    d = client.get("/api/v1/portal/reports/activity", headers=headers).json()
    actions = [e["action"] for e in d["events"]]
    assert "Company details changed" in actions and "Account created" in actions
    staff_events = [e for e in d["events"] if e["who"] == "OdooBridger"]
    assert all(e["detail"] is None and e["ip"] is None for e in staff_events)
    assert not any("staff@" in e["who"] for e in d["events"])
    assert not any("secret staff note" in (e["detail"] or "") for e in d["events"])


def test_activity_is_for_owners_and_admins(client, catalog):
    headers = signup(client)
    client.post("/api/v1/portal/users", headers=headers, json={"email": "m@acme.io", "role": "member"})
    r = client.post("/api/v1/auth/link/use", json={"token": link_token("m@acme.io"), "password": "Secret-Pass-9"})
    member = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/v1/portal/reports/activity", headers=member).status_code == 403
    assert client.get("/api/v1/portal/reports/usage", headers=member).status_code == 200


def test_reports_show_only_the_signed_in_account(client, catalog):
    a = signup(client, email="a@one.io", company="One Ltd")
    b = signup(client, email="b@two.io", company="Two Ltd")
    client.patch("/api/v1/portal/account", headers=a, json={"name": "One Holdings"})
    events_b = client.get("/api/v1/portal/reports/activity", headers=b).json()["events"]
    assert all(e["who"] != "a@one.io" for e in events_b)


def test_setup_state_comes_from_the_service(client, catalog, service_app):
    from obcore.db import SessionLocal
    from obcore.models import AccountService
    from obcore.services import odoo_connections

    headers = signup(client)
    row = client.get("/api/v1/portal/account", headers=headers).json()["account"]["services"][0]
    assert row["setup_complete"] is None
    db = SessionLocal()
    odoo_connections.record_setup(db.query(AccountService).one(), {"setup_complete": True})
    db.commit()
    db.close()
    row = client.get("/api/v1/portal/account", headers=headers).json()["account"]["services"][0]
    assert row["setup_complete"] is True

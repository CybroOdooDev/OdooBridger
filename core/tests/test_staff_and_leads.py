"""Staff console: leads pipeline, conversion, accounts, catalog, payments."""

from __future__ import annotations

from datetime import timedelta

from obcore.mailer import OUTBOX
from tests.conftest import signup, staff_headers


def _contact(client, **extra):
    body = {"topic": "Demo", "name": "Lena Lead", "email": "lena@prospect.io", "company": "Prospect Co",
            "services": ["biometric", "nonsense"], "details": {"biometric_system": "ZKTeco BioTime"},
            "preferred_date": "2026-11-02", "preferred_window": "Morning", **extra}
    return client.post("/api/v1/public/contact", json=body)


def test_contact_form_creates_a_lead_and_tells_sales(client, catalog):
    r = _contact(client)
    assert r.status_code == 201
    leads = client.get("/api/v1/staff/leads", headers=staff_headers(client)).json()
    (lead,) = leads
    assert lead["stage"] == "new" and lead["demo_status"] == "pending"
    assert lead["services"] == ["biometric"]             # unknown slugs dropped
    assert lead["details"]["biometric_system"] == "ZKTeco BioTime"
    recipients = {m["to"] for m in OUTBOX}
    assert {"staff@odoobridger.io", "lena@prospect.io"} <= recipients


def test_honeypot_is_thanked_and_dropped(client, catalog):
    assert _contact(client, website="http://spam").status_code == 201
    assert client.get("/api/v1/staff/leads", headers=staff_headers(client)).json() == []


def test_pipeline_moves_and_demo_substage(client, catalog):
    _contact(client)
    staff = staff_headers(client)
    lead_id = client.get("/api/v1/staff/leads", headers=staff).json()[0]["id"]
    # Scheduling a demo moves a new lead to Contacted.
    r = client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"demo_status": "scheduled"})
    assert r.json()["stage"] == "contacted" and r.json()["demo_status"] == "scheduled"
    client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "qualified"})
    client.post(f"/api/v1/staff/leads/{lead_id}/notes", headers=staff, json={"note": "Wants 6 terminals"})
    r = client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "lost", "lost_reason": "Budget"})
    assert r.json()["lost_reason"] == "Budget"
    detail = client.get(f"/api/v1/staff/leads/{lead_id}", headers=staff).json()
    kinds = [(e["kind"], e["to"]) for e in detail["events"]]
    assert ("stage", "contacted") in kinds and ("demo", "scheduled") in kinds and ("stage", "lost") in kinds
    assert any(e["kind"] == "note" and e["note"] == "Wants 6 terminals" for e in detail["events"])
    pipeline = client.get("/api/v1/staff/leads/pipeline", headers=staff).json()
    assert [lead["id"] for lead in pipeline["stages"]["lost"]] == [lead_id]
    bad = client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "maybe"})
    assert bad.status_code == 400


def test_converting_a_lead_creates_the_account_with_its_services(client, catalog, service_app):
    _contact(client)
    staff = staff_headers(client)
    lead_id = client.get("/api/v1/staff/leads", headers=staff).json()[0]["id"]
    r = client.post(f"/api/v1/staff/leads/{lead_id}/convert", headers=staff, json={})
    assert r.status_code == 201, r.text
    account = r.json()["account"]
    assert account["name"] == "Prospect Co"
    assert [s["service"]["slug"] for s in account["services"]] == ["biometric"]
    assert r.json()["lead"]["stage"] == "won" and r.json()["lead"]["account_id"] == account["id"]
    assert account["id"] in service_app.accounts
    assert any(m["to"] == "lena@prospect.io" and "set-password" in m["body"] for m in OUTBOX)
    again = client.post(f"/api/v1/staff/leads/{lead_id}/convert", headers=staff, json={})
    assert again.status_code == 409


def test_staff_lists_and_filters_accounts(client, catalog):
    signup(client, email="a@one.io", company="One")
    signup(client, email="b@two.io", company="Two", services=[{"service": "shipping"}])
    staff = staff_headers(client)
    assert {a["name"] for a in client.get("/api/v1/staff/accounts", headers=staff).json()} == {"One", "Two"}
    by_service = client.get("/api/v1/staff/accounts?service=shipping", headers=staff).json()
    assert [a["name"] for a in by_service] == ["Two"]
    by_email = client.get("/api/v1/staff/accounts?q=a@one", headers=staff).json()
    assert [a["name"] for a in by_email] == ["One"]
    overview = client.get("/api/v1/staff/overview", headers=staff).json()
    assert overview["kpis"]["new_customers"]["value"] == 2


def test_staff_creates_an_account_with_a_comped_service(client, catalog, service_app):
    staff = staff_headers(client)
    r = client.post("/api/v1/staff/accounts", headers=staff, json={
        "company": "Partner GmbH", "owner_email": "p@partner.io",
        "services": [{"service": "biometric", "package_id": catalog["growth"], "trial_days": 30}]})
    assert r.status_code == 201, r.text
    account = r.json()
    r = client.patch(f"/api/v1/staff/accounts/{account['id']}/services/biometric", headers=staff,
                     json={"status": "active"})
    assert r.json()["services"][0]["status"] == "active"
    assert service_app.accounts[account["id"]]["status"] == "active"
    usage = client.get(f"/api/v1/staff/accounts/{account['id']}/services/biometric/usage", headers=staff)
    assert usage.json() == {"devices": 3, "employees": 12}


def test_catalog_editing_and_secret_rotation(client, catalog, service_app):
    headers = signup(client)
    staff = staff_headers(client)
    services = client.get("/api/v1/staff/services", headers=staff).json()
    bio = next(s for s in services if s["slug"] == "biometric")
    assert bio["accounts"] == 1 and bio["has_client_secret"]
    r = client.post(f"/api/v1/staff/services/{bio['id']}/secret", headers=staff)
    assert len(r.json()["client_secret"]) > 30
    # Changing a package's limits re-provisions everyone on it.
    r = client.patch(f"/api/v1/staff/packages/{catalog['starter']}", headers=staff,
                     json={"entitlements": {"max_devices": 3, "max_employees": 60}})
    assert r.status_code == 200
    account_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
    assert service_app.accounts[account_id]["entitlements"]["max_devices"] == 3
    bad = client.patch(f"/api/v1/staff/packages/{catalog['starter']}", headers=staff,
                       json={"stripe_price_id": "prod_123"})
    assert bad.status_code == 400
    new = client.post("/api/v1/staff/services", headers=staff, json={
        "slug": "marketplace", "name": "Marketplace", "base_url": "https://market.test"})
    assert new.status_code == 201 and new.json()["client_secret"]


def test_payments_page_and_stripe_keys(client, catalog, fake_stripe):
    headers = signup(client)
    r = client.post("/api/v1/portal/billing/checkout", headers=headers, json={})
    session_id = r.json()["url"].rsplit("/", 1)[1]
    from tests.test_billing import _hook

    _hook(client, fake_stripe.complete_checkout(session_id))
    staff = staff_headers(client)
    page = client.get("/api/v1/staff/payments", headers=staff).json()
    assert page["subscribers"][0]["monthly_total_cents"] == 2900
    assert page["invoices"][0]["id"] == "in_1"
    bad = client.patch("/api/v1/staff/payments/stripe", headers=staff, json={"secret_key": "pk_live_x"})
    assert bad.status_code == 400


def test_stripe_keys_are_stored_encrypted(client, catalog):
    staff = staff_headers(client)
    r = client.patch("/api/v1/staff/payments/stripe", headers=staff,
                     json={"secret_key": "sk_test_abcdef1234", "webhook_secret": "whsec_abc"})
    assert r.json()["enabled"] and r.json()["secret_key_hint"] == "…1234" and r.json()["source"] == "console"
    from obcore.db import SessionLocal
    from obcore.models import Setting

    db = SessionLocal()
    assert "sk_test_abcdef1234" not in db.get(Setting, "stripe").value
    db.close()


def test_staff_team_management(client, catalog):
    staff = staff_headers(client)
    r = client.post("/api/v1/staff/team", headers=staff,
                    json={"email": "new@odoobridger.io", "password": "New-Staff-Pass-1"})
    assert r.status_code == 201
    login = client.post("/api/v1/auth/staff/login", json={"email": "new@odoobridger.io", "password": "New-Staff-Pass-1"})
    assert login.status_code == 200
    me = client.get("/api/v1/auth/staff/me", headers=staff).json()
    assert client.patch(f"/api/v1/staff/team/{me['id']}", headers=staff, json={"is_active": False}).status_code == 400
    new_id = r.json()["id"]
    # Rename and reset another member's password.
    r = client.patch(f"/api/v1/staff/team/{new_id}", headers=staff,
                     json={"full_name": "Nina New", "password": "Changed-Pass-22"})
    assert r.status_code == 200 and r.json()["full_name"] == "Nina New"
    assert client.post("/api/v1/auth/staff/login",
                       json={"email": "new@odoobridger.io", "password": "Changed-Pass-22"}).status_code == 200
    # Your own password needs your current one.
    assert client.patch(f"/api/v1/staff/team/{me['id']}", headers=staff,
                        json={"password": "Another-Pass-33"}).status_code == 400
    r = client.patch(f"/api/v1/staff/team/{me['id']}", headers=staff,
                     json={"password": "Another-Pass-33", "current_password": "Staff-Password-1"})
    assert r.status_code == 200
    # Weak passwords are refused.
    assert client.patch(f"/api/v1/staff/team/{new_id}", headers=staff, json={"password": "short"}).status_code == 422


def test_a_won_lead_is_closed(client, catalog):
    _contact(client)
    staff = staff_headers(client)
    lead_id = client.get("/api/v1/staff/leads", headers=staff).json()[0]["id"]
    assert client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "won"}).status_code == 200
    # It can't move back, nor be edited...
    r = client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "qualified"})
    assert r.status_code == 409
    assert client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"value_cents": 100}).status_code == 409
    assert client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"demo_status": "done"}).status_code == 409
    # ...but notes still go into its history.
    r = client.post(f"/api/v1/staff/leads/{lead_id}/notes", headers=staff, json={"note": "Signed the contract"})
    assert r.status_code == 201
    assert client.get(f"/api/v1/staff/leads/{lead_id}", headers=staff).json()["stage"] == "won"


# --- the accounts list: one Billing pill however many services -------------------------
def _state(statuses, *, trial_in_days=None, account_status="active"):
    """Billing state of an account whose services have the given statuses."""
    from types import SimpleNamespace as NS

    from obcore.security import utcnow
    from obcore.services import dashboard

    now = utcnow()
    rows = [NS(status=s, trial_ends_at=(now + timedelta(days=trial_in_days, hours=-1)) if s == "trialing" and trial_in_days is not None else None)
            for s in statuses]
    return dashboard.billing_state(NS(services=rows, status=account_status, renews_at=now + timedelta(days=20)), now)


def test_billing_pill_is_worst_wins_whatever_the_number_of_services():
    assert _state([])["key"] == "none"
    assert _state(["cancelled"])["key"] == "none"
    paying = _state(["active"])
    assert (paying["label"], paying["renews_at"]) == ("Paying", paying["renews_at"]) and paying["renews_at"]
    assert _state(["active", "active", "active"])["key"] == "paying"
    trial = _state(["trialing"], trial_in_days=12)
    assert (trial["key"], trial["label"], trial["note"]) == ("trial", "Trial · ends in 12 d", None)
    mixed = _state(["active", "trialing"], trial_in_days=20)
    assert (mixed["key"], mixed["label"], mixed["note"]) == ("mixed", "Paying + Trial", "1 paying · 1 on trial")
    ending = _state(["active", "trialing"], trial_in_days=3)
    assert (ending["key"], ending["label"], ending["tone"]) == ("trial_ending", "Trial · ends in 3 d", "warn")
    assert ending["note"] == "1 paying · 1 on trial"
    assert _state(["trialing"], trial_in_days=0)["label"] == "Trial · ends today"
    due = _state(["active", "trialing", "past_due"], trial_in_days=2)       # past due beats a trial about to end
    assert (due["key"], due["label"], due["note"]) == ("past_due", "Payment Due", "1 of 3 services")


def test_urgency_order_is_payment_due_then_suspended_then_trials_by_end():
    rank = lambda *a, **k: _state(*a, **k)["rank"]          # noqa: E731
    order = [rank(["past_due"]), rank(["active"], account_status="suspended"), rank(["trialing"], trial_in_days=2),
             rank(["trialing"], trial_in_days=12), rank(["active", "trialing"], trial_in_days=20), rank(["active"]), rank([])]
    assert order == sorted(order) and len(set(order)) == len(order)


def test_accounts_list_carries_billing_and_filters_by_it(client, catalog, service_app):
    signup(client)
    signup(client, email="b@beta.io", company="Beta")
    headers = staff_headers(client)
    rows = client.get("/api/v1/staff/accounts", headers=headers).json()
    assert {r["billing"]["key"] for r in rows} <= {"trial", "trial_ending", "none"}
    assert all("paying_cents" in r and "after_trial_cents" in r for r in rows)
    none = client.get("/api/v1/staff/accounts", params={"billing_filter": "past_due"}, headers=headers).json()
    assert none == []
    trial = client.get("/api/v1/staff/accounts", params={"billing_filter": "trial"}, headers=headers).json()
    assert len(trial) == len(rows) - sum(r["billing"]["key"] == "none" for r in rows)

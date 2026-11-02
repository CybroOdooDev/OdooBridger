"""Staff console: leads pipeline, conversion, accounts, catalog, payments."""

from __future__ import annotations

from datetime import timedelta

from obcore.config import settings
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
    # Correcting the reason on an already-lost lead sticks, and the change is in its history.
    r = client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "lost", "lost_reason": "Timing"})
    assert r.json()["lost_reason"] == "Timing"
    detail = client.get(f"/api/v1/staff/leads/{lead_id}", headers=staff).json()
    kinds = [(e["kind"], e["to"]) for e in detail["events"]]
    assert ("stage", "contacted") in kinds and ("demo", "scheduled") in kinds and ("stage", "lost") in kinds
    assert any(e["kind"] == "note" and e["note"] == "Wants 6 terminals" for e in detail["events"])
    assert any(e["kind"] == "note" and "Timing" in e["note"] for e in detail["events"])
    pipeline = client.get("/api/v1/staff/leads/pipeline", headers=staff).json()
    assert [lead["id"] for lead in pipeline["stages"]["lost"]] == [lead_id]
    bad = client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "maybe"})
    assert bad.status_code == 400


def test_converting_a_lead_creates_the_account_with_its_services(client, catalog, service_app):
    _contact(client)
    staff = staff_headers(client)
    lead_id = client.get("/api/v1/staff/leads", headers=staff).json()[0]["id"]
    # Only a Qualified lead converts; a new one is refused, and Won can't be set by hand either.
    assert client.post(f"/api/v1/staff/leads/{lead_id}/convert", headers=staff, json={}).status_code == 409
    assert client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "won"}).status_code == 400
    client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "contacted"})
    assert client.post(f"/api/v1/staff/leads/{lead_id}/convert", headers=staff, json={}).status_code == 409
    client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "qualified"})
    assert client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "won"}).status_code == 400
    assert client.get(f"/api/v1/staff/leads/{lead_id}", headers=staff).json()["stage"] == "qualified"
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
    assert "invoices" not in page and "error" not in page      # the page itself never waits on Stripe
    assert client.get("/api/v1/staff/payments/invoices", headers=staff).json()["invoices"][0]["id"] == "in_1"
    assert client.post("/api/v1/staff/payments/stripe/test", headers=staff).status_code == 200
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


def test_a_won_lead_is_closed(client, catalog, service_app):
    _contact(client)
    staff = staff_headers(client)
    lead_id = client.get("/api/v1/staff/leads", headers=staff).json()[0]["id"]
    client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "qualified"})
    assert client.post(f"/api/v1/staff/leads/{lead_id}/convert", headers=staff, json={}).status_code == 201
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
    assert _state(["trialing"], trial_in_days=7)["tone"] == "warn"           # amber up to a week...
    assert _state(["trialing"], trial_in_days=2)["tone"] == "bad"            # ...red under three days
    assert _state(["trialing"], trial_in_days=8)["tone"] == "trialing"
    assert ending["note"] == "1 paying · 1 on trial"
    assert _state(["trialing"], trial_in_days=0)["label"] == "Trial · ends today"
    due = _state(["active", "trialing", "past_due"], trial_in_days=2)       # past due beats a trial about to end
    assert (due["key"], due["label"], due["note"]) == ("past_due", "Payment Due", "1 of 3 services")


def test_urgency_order_is_payment_due_urgent_trial_suspended_ending_trial_then_the_rest():
    rank = lambda *a, **k: _state(*a, **k)["rank"]          # noqa: E731
    order = [rank(["past_due"]), rank(["trialing"], trial_in_days=2), rank(["active"], account_status="suspended"),
             rank(["trialing"], trial_in_days=6), rank(["trialing"], trial_in_days=12),
             rank(["active", "trialing"], trial_in_days=20), rank(["active"]), rank([])]
    assert order == sorted(order) and len(set(order)) == len(order)
    # A failed payment still wins on a suspended account; a suspended trial ending tomorrow is the trial's call.
    assert rank(["past_due"], account_status="suspended") == rank(["past_due"])
    assert rank(["trialing"], trial_in_days=1, account_status="suspended") == rank(["trialing"], trial_in_days=1)


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


def test_yearly_accounts_show_what_they_are_billed_per_year():
    from types import SimpleNamespace as NS

    from obcore.services import billing

    package = NS(price_cents_for=lambda interval: 28992 if interval == "year" else 2900)
    rows = [NS(package=package, status="active"), NS(package=package, status="trialing")]
    yearly = NS(services=rows, billing_interval="year")
    monthly = NS(services=rows, billing_interval="month")
    assert billing.billed_period_cents(yearly) == 28992            # the trial is not billed yet
    assert billing.billed_period_cents(monthly) == 2900
    assert billing.split_monthly_cents(yearly) == (2416, 2416)      # $289.92 a year is $24.16 a month


def test_account_count_is_for_the_filtered_list_header(client, catalog, service_app):
    signup(client)
    signup(client, email="b@beta.io", company="Beta")
    headers = staff_headers(client)
    assert client.get("/api/v1/staff/accounts/count", headers=headers).json() == {"total": 2}
    assert client.get("/api/v1/staff/accounts/count").status_code in (401, 403)


def test_leads_carry_their_last_activity(client, catalog):
    _contact(client)
    staff = staff_headers(client)
    (lead,) = client.get("/api/v1/staff/leads", headers=staff).json()
    assert lead["last_activity_at"]                                  # the day it came in, at least
    client.post(f"/api/v1/staff/leads/{lead['id']}/notes", headers=staff, json={"note": "called, voicemail"})
    after = client.get("/api/v1/staff/leads/pipeline", headers=staff).json()["stages"]["new"][0]
    assert after["last_activity_at"] >= lead["last_activity_at"]


def test_a_lead_can_be_assigned_to_a_staff_member_and_it_is_recorded(client, catalog):
    _contact(client)
    staff = staff_headers(client)
    me = client.get("/api/v1/auth/staff/me", headers=staff).json()
    lead_id = client.get("/api/v1/staff/leads", headers=staff).json()[0]["id"]
    r = client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"owner_staff_id": me["id"]})
    assert r.status_code == 200 and r.json()["owner_staff_id"] == me["id"]
    events = client.get(f"/api/v1/staff/leads/{lead_id}", headers=staff).json()["events"]
    assert any(e["kind"] == "owner" and e["note"].startswith("Assigned to") for e in events)
    # Someone who isn't staff can't own a lead; clearing the owner is allowed.
    bad = client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"owner_staff_id": "nobody"})
    assert bad.status_code == 400
    cleared = client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"owner_staff_id": None})
    assert cleared.json()["owner_staff_id"] is None


def test_lead_trends_rebuild_the_past_from_the_history(client, catalog):
    from datetime import timedelta

    from obcore.db import SessionLocal
    from obcore.models import Lead, LeadEvent
    from obcore.security import utcnow

    staff = staff_headers(client)
    now = utcnow()
    with SessionLocal() as db:
        def lead(name, created, stage, value, moves):
            row = Lead(name=name, email=f"{name}@x.io", company=name, stage=stage, value_cents=value,
                       created_at=created, services=[])
            db.add(row)
            db.flush()
            db.add(LeadEvent(lead_id=row.id, kind="stage", to_value="new", created_at=created))
            for when, to in moves:
                db.add(LeadEvent(lead_id=row.id, kind="stage", from_value="new", to_value=to, created_at=when))
            return row
        d = lambda n: now - timedelta(days=n)       # noqa: E731
        lead("old-open", d(50), "new", 1000, [])                       # open then and now
        lead("old-won-recently", d(50), "won", 2000, [(d(5), "won")])  # open then, won in the window
        lead("new-in-window", d(10), "qualified", 4000, [(d(8), "qualified")])
        lead("prev-window-won", d(45), "won", 3000, [(d(40), "won")])  # won in the previous window
        db.commit()
    t = client.get("/api/v1/staff/leads/trends", params={"days": 30}, headers=staff).json()
    # 30 days ago the open leads were old-open and old-won-recently; prev-window-won had already been won.
    assert t["open_leads"] == {"value": 2, "previous": 2}
    assert t["pipeline_cents"]["value"] == 5000 and t["pipeline_cents"]["previous"] == 3000
    assert t["won"]["value"] == 1 and t["won"]["previous"] == 1 and t["won"]["all_time"] == 2
    assert t["conversion"]["total"] == 1 and t["conversion"]["won"] == 0 and t["conversion"]["value"] == 0.0


def test_follow_up_date_on_a_lead(client, catalog):
    _contact(client)
    staff = staff_headers(client)
    lead_id = client.get("/api/v1/staff/leads", headers=staff).json()[0]["id"]
    assert client.get(f"/api/v1/staff/leads/{lead_id}", headers=staff).json()["follow_up_on"] is None
    r = client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"follow_up_on": "2026-11-05"})
    assert r.status_code == 200 and r.json()["follow_up_on"] == "2026-11-05"
    detail = client.get(f"/api/v1/staff/leads/{lead_id}", headers=staff).json()
    assert any(e["kind"] == "followup" and "Thu 5 Nov 2026" in e["note"] for e in detail["events"])
    # Planning a follow-up isn't contact: "last activity" ignores it.
    row = client.get("/api/v1/staff/leads/pipeline", headers=staff).json()["stages"]["new"][0]
    assert row["follow_up_on"] == "2026-11-05"
    last_stage = [e for e in detail["events"] if e["kind"] == "stage"][-1]
    assert row["last_activity_at"] == last_stage["created_at"]
    assert client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"follow_up_on": "soon"}).status_code == 422
    # Clearing it is recorded too, and losing the lead clears it and refuses a new one.
    client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"follow_up_on": None})
    client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"follow_up_on": "2026-11-06"})
    client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"stage": "lost", "lost_reason": "Timing"})
    assert client.get(f"/api/v1/staff/leads/{lead_id}", headers=staff).json()["follow_up_on"] is None
    assert client.patch(f"/api/v1/staff/leads/{lead_id}", headers=staff, json={"follow_up_on": "2026-11-09"}).status_code == 400


# --- Services & Packages catalog ----------------------------------------------------------------
def test_limits_must_be_numbers(client, catalog):
    staff = staff_headers(client)
    url = f"/api/v1/staff/packages/{catalog['starter']}"
    for bad in ({"max_devices": "ten"}, {"max_devices": -1}, {"min_sync_interval_minutes": 0}, {"max_employees": True}):
        r = client.patch(url, headers=staff, json={"entitlements": bad})
        assert r.status_code == 400, bad
    # Empty means unlimited; a limit only some other service knows about passes through.
    r = client.patch(url, headers=staff, json={"entitlements": {"max_devices": "", "max_employees": 80, "seats_note": "x"}})
    assert r.status_code == 200
    assert r.json()["entitlements"] == {"max_devices": None, "max_employees": 80, "seats_note": "x"}


def test_retiring_a_package_hides_it_from_signup_but_not_from_its_accounts(client, catalog):
    headers = signup(client)                                   # on Starter, the recommended package
    staff = staff_headers(client)
    r = client.patch(f"/api/v1/staff/packages/{catalog['starter']}", headers=staff, json={"is_active": False})
    assert r.status_code == 200
    assert r.json()["is_active"] is False and r.json()["is_default"] is False   # off sale can't be the default
    public = client.get("/api/v1/public/catalog").json()
    services = public["services"] if isinstance(public, dict) else public
    names = [p["name"] for s in services if s["slug"] == "biometric" for p in s["packages"]]
    assert "Starter" not in names and "Growth" in names
    # The account on it keeps it, and staff still see it.
    sub = next(x for x in client.get("/api/v1/portal/account", headers=headers).json()["account"]["services"] if x["service"]["slug"] == "biometric")
    assert sub["package"]["name"] == "Starter"
    listed = next(s for s in client.get("/api/v1/staff/services", headers=staff).json() if s["slug"] == "biometric")
    starter = next(p for p in listed["packages"] if p["name"] == "Starter")
    assert starter["is_active"] is False and starter["accounts"] == 1
    # And it can come back.
    r = client.patch(f"/api/v1/staff/packages/{catalog['starter']}", headers=staff, json={"is_active": True})
    assert r.json()["is_active"] is True


def test_catalog_shows_the_trial_length_each_package_really_gets(client, catalog):
    staff = staff_headers(client)
    url = f"/api/v1/staff/packages/{catalog['growth']}"
    client.patch(url, headers=staff, json={"trial_days": None})
    listed = {s["slug"]: s for s in client.get("/api/v1/staff/services", headers=staff).json()}
    growth = next(p for p in listed["biometric"]["packages"] if p["name"] == "Growth")
    assert growth["trial_is_default"] is True and growth["trial_days_effective"] == settings.default_trial_days
    assert listed["biometric"]["trial_default_days"] == settings.default_trial_days
    client.patch(url, headers=staff, json={"trial_days": 0})
    growth = next(p for p in next(s for s in client.get("/api/v1/staff/services", headers=staff).json()
                                  if s["slug"] == "biometric")["packages"] if p["name"] == "Growth")
    assert growth["trial_is_default"] is False and growth["trial_days_effective"] == 0


def test_a_service_cannot_be_saved_at_localhost_in_production(client, catalog, monkeypatch):
    staff = staff_headers(client)
    svc = catalog["service"]
    # Development: allowed, no warning.
    ok = client.patch(f"/api/v1/staff/services/{svc}", headers=staff, json={"base_url": "http://localhost:8001"})
    assert ok.status_code == 200 and ok.json()["url_warnings"] == []
    monkeypatch.setattr(settings, "environment", "production")
    # An existing localhost value is flagged for staff to see ...
    listed = next(s for s in client.get("/api/v1/staff/services", headers=staff).json() if s["slug"] == "biometric")
    assert [w["field"] for w in listed["url_warnings"]] == ["base_url"]
    # ... and a new one is refused, public or internal.
    for field, value in (("base_url", "http://localhost:8001"), ("internal_url", "http://127.0.0.1:8000"),
                         ("base_url", "http://app.localhost")):
        r = client.patch(f"/api/v1/staff/services/{svc}", headers=staff, json={field: value})
        assert r.status_code == 400 and "localhost" in r.json()["detail"], (field, value)
    r = client.patch(f"/api/v1/staff/services/{svc}", headers=staff,
                     json={"base_url": "https://biometric.example.com", "internal_url": "http://biobridge:8000"})
    assert r.status_code == 200 and r.json()["url_warnings"] == []


def test_local_address_check():
    from obcore.urlcheck import is_local
    assert all(is_local(u) for u in ("http://localhost:8001", "http://127.0.0.1", "http://[::1]:9", "http://x.localhost", "http://0.0.0.0:1"))
    assert not any(is_local(u) for u in ("https://biometric.example.com", "http://biobridge:8000", "http://10.0.0.5", "", None))


def test_production_refuses_to_boot_with_the_dev_biometric_address(monkeypatch):
    import pytest
    from obcore import main
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret", "x" * 40)
    monkeypatch.setattr(settings, "master_encryption_key", "y" * 40)
    monkeypatch.setattr(settings, "biometric_client_secret", "")
    main._check_production()                                  # no built-in BioBridge: nothing to check
    monkeypatch.setattr(settings, "biometric_client_secret", "s" * 40)
    with pytest.raises(RuntimeError, match="BIOMETRIC_BASE_URL"):
        main._check_production()
    monkeypatch.setattr(settings, "biometric_base_url", "https://biometric.example.com")
    main._check_production()


def test_service_status_and_test_connection(client, catalog, service_app):
    staff = staff_headers(client)
    health = client.get("/api/v1/staff/services/health", headers=staff).json()
    assert health[catalog["service"]]["status"] == "operational"
    r = client.post(f"/api/v1/staff/services/{catalog['service']}/test", headers=staff)
    assert r.status_code == 200 and r.json()["reachable"] is True and r.json()["url"].startswith("http://biometric.test")
    assert client.post("/api/v1/staff/services/nope/test", headers=staff).status_code == 404


def test_stripe_check_compares_every_price_with_the_catalog(client, catalog, fake_stripe):
    staff = staff_headers(client)
    check = client.get("/api/v1/staff/services/stripe-check", headers=staff).json()
    assert check["enabled"] is True
    starter = check["packages"][catalog["starter"]]
    assert starter["month"]["state"] == "ok" and starter["year"]["state"] == "ok"
    assert check["packages"][catalog["growth"]]["year"]["state"] == "missing"   # no yearly Stripe price at all
    # Stripe now charges a different amount, one price is archived, one id doesn't exist.
    fake_stripe.prices["price_starter_m"]["unit_amount"] = 3900
    fake_stripe.prices["price_starter_y"]["recurring"] = {"interval": "month"}
    fake_stripe.prices["price_growth_m"]["active"] = False
    fake_stripe.prices.pop("price_ship_m")
    check = client.get("/api/v1/staff/services/stripe-check", headers=staff).json()["packages"]
    assert check[catalog["starter"]]["month"]["state"] == "mismatch" and "39.00" in check[catalog["starter"]]["month"]["detail"]
    assert check[catalog["starter"]]["year"]["state"] == "mismatch" and "per month" in check[catalog["starter"]]["year"]["detail"]
    assert check[catalog["growth"]]["month"]["state"] == "mismatch" and "archived" in check[catalog["growth"]]["month"]["detail"]
    assert check[catalog["ship_basic"]]["month"]["state"] == "notfound"
    # A Stripe outage is "couldn't check", never "out of sync".
    from obcore.services import stripe_api
    def boom(*a, **k):
        raise stripe_api.BillingError("Could not reach Stripe: timeout")
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(stripe_api, "request", boom)
    try:
        down = client.get("/api/v1/staff/services/stripe-check", headers=staff).json()["packages"]
    finally:
        mp.undo()
    assert down[catalog["starter"]]["month"]["state"] == "error"


def test_stripe_check_when_stripe_is_not_connected(client, catalog):
    staff = staff_headers(client)
    assert client.get("/api/v1/staff/services/stripe-check", headers=staff).json() == {"enabled": False, "packages": {}}

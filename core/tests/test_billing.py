"""One Stripe subscription per account, one item per service."""

from __future__ import annotations

import json
from datetime import timedelta

from obcore.db import SessionLocal
from obcore.models import AccountService
from obcore.security import utcnow
from obcore.services import stripe_api
from obcore.services.jobs import sweep_once
from tests.conftest import signup, staff_headers


def _hook(client, event: dict):
    body = json.dumps(event).encode()
    sig = stripe_api.sign_for_tests(body, "whsec_test")
    return client.post("/api/v1/billing/webhook", content=body,
                       headers={"stripe-signature": sig, "content-type": "application/json"})


def _account(client, headers):
    return client.get("/api/v1/portal/account", headers=headers).json()["account"]


def _pay(client, headers, fake_stripe, interval="month"):
    r = client.post("/api/v1/portal/billing/checkout", headers=headers, json={"interval": interval})
    assert r.status_code == 200, r.text
    session_id = r.json()["url"].rsplit("/", 1)[1]
    assert _hook(client, fake_stripe.complete_checkout(session_id, interval)).json()["result"] == "linked"
    return session_id


def test_one_checkout_bills_every_service(client, catalog, fake_stripe, service_app):
    headers = signup(client, services=[{"service": "biometric"}, {"service": "shipping"}])
    session_id = _pay(client, headers, fake_stripe)
    prices = [li["price"] for li in fake_stripe.sessions[session_id]["line_items"]]
    assert sorted(prices) == ["price_ship_m", "price_starter_m"]
    account = _account(client, headers)
    assert account["pays_online"] is True
    assert {r["status"] for r in account["services"]} == {"active"}
    assert all(r["billed"] for r in account["services"])
    assert service_app.accounts[account["id"]]["status"] == "active"
    assert len(fake_stripe.subscriptions) == 1


def test_yearly_checkout_uses_yearly_prices(client, catalog, fake_stripe):
    headers = signup(client)
    session_id = _pay(client, headers, fake_stripe, "year")
    assert [li["price"] for li in fake_stripe.sessions[session_id]["line_items"]] == ["price_starter_y"]
    assert _account(client, headers)["billing_interval"] == "year"


def test_a_service_added_later_joins_the_same_subscription(client, catalog, fake_stripe, service_app):
    headers = signup(client)
    _pay(client, headers, fake_stripe)
    r = client.post("/api/v1/portal/services", headers=headers, json={"service": "shipping"})
    assert r.status_code == 201, r.text
    (sub,) = fake_stripe.subscriptions.values()
    assert sorted(i["price"]["id"] for i in sub["items"]["data"]) == ["price_ship_m", "price_starter_m"]
    shipping = next(s for s in r.json()["services"] if s["service"]["slug"] == "shipping")
    assert shipping["status"] == "active" and shipping["billed"]
    assert any(c[1] == "subscription_items" and c[2]["proration_behavior"] == "create_prorations"
               for c in fake_stripe.calls)


def test_package_change_on_a_billed_service_changes_its_item(client, catalog, fake_stripe, service_app):
    headers = signup(client)
    _pay(client, headers, fake_stripe)
    r = client.patch("/api/v1/portal/services/biometric", headers=headers, json={"package_id": catalog["growth"]})
    assert r.status_code == 200, r.text
    (sub,) = fake_stripe.subscriptions.values()
    assert sub["items"]["data"][0]["price"]["id"] == "price_growth_m"
    account_id = _account(client, headers)["id"]
    assert service_app.accounts[account_id]["entitlements"]["max_devices"] == 10


def test_removing_one_of_two_billed_services_deletes_its_item(client, catalog, fake_stripe, service_app):
    headers = signup(client, services=[{"service": "biometric"}, {"service": "shipping"}])
    _pay(client, headers, fake_stripe)
    r = client.delete("/api/v1/portal/services/shipping", headers=headers)
    assert r.status_code == 200, r.text
    (sub,) = fake_stripe.subscriptions.values()
    assert [i["price"]["id"] for i in sub["items"]["data"]] == ["price_starter_m"]
    rows = {s["service"]["slug"]: s for s in r.json()["account"]["services"]}
    assert rows["shipping"]["status"] == "cancelled" and rows["biometric"]["status"] == "active"


def test_removing_the_last_service_cancels_at_period_end(client, catalog, fake_stripe):
    headers = signup(client)
    _pay(client, headers, fake_stripe)
    r = client.delete("/api/v1/portal/services/biometric", headers=headers)
    assert r.status_code == 200
    assert "stays on until" in r.json()["message"]
    account = r.json()["account"]
    assert account["cancel_at_period_end"] is True
    assert account["services"][0]["status"] == "active"   # still paid for


def test_failed_renewal_marks_services_past_due_and_tells_the_service(client, catalog, fake_stripe, service_app):
    headers = signup(client)
    _pay(client, headers, fake_stripe)
    (sub,) = fake_stripe.subscriptions.values()
    sub["status"] = "past_due"
    r = _hook(client, {"id": "evt_fail", "type": "invoice.payment_failed",
                       "data": {"object": {"subscription": sub["id"]}}})
    assert r.json()["result"] == "applied"
    account = _account(client, headers)
    assert account["services"][0]["status"] == "past_due"
    assert service_app.accounts[account["id"]]["status"] == "past_due"
    # Delivered again: applied once.
    again = _hook(client, {"id": "evt_fail", "type": "invoice.payment_failed",
                           "data": {"object": {"subscription": sub["id"]}}})
    assert again.json()["result"] == "duplicate"


def test_subscription_ending_cancels_every_billed_service(client, catalog, fake_stripe):
    headers = signup(client)
    _pay(client, headers, fake_stripe)
    (sub,) = fake_stripe.subscriptions.values()
    sub["status"] = "canceled"
    _hook(client, {"id": "evt_end", "type": "customer.subscription.deleted", "data": {"object": {"id": sub["id"]}}})
    account = _account(client, headers)
    assert account["pays_online"] is False
    assert account["services"][0]["status"] == "cancelled"


def test_bad_signature_is_refused(client, catalog, fake_stripe):
    r = client.post("/api/v1/billing/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=bad"})
    assert r.status_code == 400


def test_unpaid_trial_ends_past_due(client, catalog, service_app):
    headers = signup(client)
    account_id = _account(client, headers)["id"]
    db = SessionLocal()
    row = db.query(AccountService).one()
    row.trial_ends_at = utcnow() - timedelta(minutes=1)
    db.commit()
    assert sweep_once(db)["trials_ended"] == 1
    db.commit()
    db.close()
    assert service_app.accounts[account_id]["status"] == "past_due"


def test_staff_extending_a_trial_reopens_it(client, catalog, service_app):
    headers = signup(client)
    account_id = _account(client, headers)["id"]
    db = SessionLocal()
    row = db.query(AccountService).one()
    row.trial_ends_at = utcnow() - timedelta(minutes=1)
    db.commit()
    sweep_once(db)
    db.commit()
    db.close()
    staff = staff_headers(client)
    later = (utcnow() + timedelta(days=7)).isoformat()
    r = client.patch(f"/api/v1/staff/accounts/{account_id}/services/biometric", headers=staff,
                     json={"trial_ends_at": later})
    assert r.status_code == 200, r.text
    assert service_app.accounts[account_id]["status"] == "trialing"


def test_staff_overrides_reach_the_service(client, catalog, service_app):
    headers = signup(client)
    account_id = _account(client, headers)["id"]
    staff = staff_headers(client)
    client.patch(f"/api/v1/staff/accounts/{account_id}/services/biometric", headers=staff,
                 json={"entitlement_overrides": {"max_devices": 8}})
    assert service_app.accounts[account_id]["entitlements"] == {"max_devices": 8, "max_employees": 50}


def test_checkout_without_stripe_says_so(client, catalog):
    headers = signup(client)
    r = client.post("/api/v1/portal/billing/checkout", headers=headers, json={})
    assert r.status_code == 400 and "not set up" in r.json()["detail"]


def test_package_change_can_wait_for_the_next_renewal(client, catalog, fake_stripe, service_app):
    from datetime import timedelta

    from obcore.db import SessionLocal
    from obcore.security import utcnow
    from obcore.services import billing

    headers = signup(client)
    _pay(client, headers, fake_stripe)
    r = client.patch("/api/v1/portal/services/biometric", headers=headers,
                     json={"package_id": catalog["growth"], "when": "period_end"})
    assert r.status_code == 200, r.text
    row = r.json()["services"][0]
    assert row["package"]["id"] == catalog["starter"] and row["pending_change"]["package"]["id"] == catalog["growth"]
    (sub,) = fake_stripe.subscriptions.values()
    assert sub["items"]["data"][0]["price"]["id"] == "price_starter_m"      # Stripe is untouched until then

    db = SessionLocal()
    assert billing.apply_pending_changes(db) == []                           # the period has not ended
    assert billing.apply_pending_changes(db, now=utcnow() + timedelta(days=400))
    db.commit()
    db.close()
    assert sub["items"]["data"][0]["price"]["id"] == "price_growth_m"
    assert any(str(c[1]).startswith("subscription_items") and c[2].get("proration_behavior") == "none" for c in fake_stripe.calls)
    row = client.get("/api/v1/portal/account", headers=headers).json()["account"]["services"][0]
    assert row["package"]["id"] == catalog["growth"] and row["pending_change"] is None


def test_a_scheduled_change_can_be_cancelled_or_replaced_by_changing_now(client, catalog, fake_stripe, service_app):
    headers = signup(client)
    _pay(client, headers, fake_stripe)
    client.patch("/api/v1/portal/services/biometric", headers=headers,
                 json={"package_id": catalog["growth"], "when": "period_end"})
    r = client.delete("/api/v1/portal/services/biometric/pending-change", headers=headers)
    assert r.json()["services"][0]["pending_change"] is None
    client.patch("/api/v1/portal/services/biometric", headers=headers,
                 json={"package_id": catalog["growth"], "when": "period_end"})
    r = client.patch("/api/v1/portal/services/biometric", headers=headers, json={"package_id": catalog["growth"]})
    row = r.json()["services"][0]
    assert row["package"]["id"] == catalog["growth"] and row["pending_change"] is None


def test_waiting_for_renewal_needs_a_paid_period(client, catalog, service_app):
    headers = signup(client)
    r = client.patch("/api/v1/portal/services/biometric", headers=headers,
                     json={"package_id": catalog["growth"], "when": "period_end"})
    assert r.status_code in (400, 409, 422)

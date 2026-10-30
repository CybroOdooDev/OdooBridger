"""The staff overview: figures, service health and what needs attention."""

from __future__ import annotations

from datetime import timedelta

import pytest

from obcore.db import SessionLocal
from obcore.models import Account, AccountService, OdooConnection, ServiceStatus
from obcore.security import utcnow
from obcore.services import ops
from tests.conftest import signup, staff_headers


def _overview(client, **params):
    r = client.get("/api/v1/staff/overview", params={"fresh": "true", **params}, headers=staff_headers(client))
    assert r.status_code == 200, r.text
    return r.json()


def test_overview_counts_customers_and_subscriptions(client, catalog, service_app):
    signup(client)
    signup(client, email="b@beta.io", company="Beta")
    o = _overview(client)
    k = o["kpis"]
    assert o["range"]["key"] == "30d"
    assert k["new_customers"]["value"] == 2 and k["new_customers"]["previous"] == 0
    assert k["active_customers"]["value"] == 0       # trials are not paying customers
    assert o["mix"]["trial"] == 2
    # A young business gets a short chart, not 30 days of which 28 are empty.
    assert len(o["acquisition"]["buckets"]) == 7 and o["acquisition"]["buckets"][-1]["new"] == 2
    assert {r["name"] for r in o["recent_customers"]} == {"Beta", "Acme Ltd"}
    funnel = {f["key"]: f["value"] for f in o["funnel"]}
    assert funnel["registered"] == 2 and funnel["verified"] == 2 and funnel["trial"] == 2 and funnel["paying"] == 0
    assert "leads" not in funnel and o["funnel"][0]["pct_prev"] is None and o["funnel"][0]["pct_top"] == 100.0
    assert any(a["title"] == "New customer registered" for a in o["activity"])
    # No revenue history before the first snapshot of the period start.
    assert k["mrr"]["previous"] is None and k["mrr"]["change_pct"] is None
    # Ranges: 6 and 12 months by month; all time has nothing to compare with.
    assert 3 <= len(_overview(client, period="6m")["acquisition"]["buckets"]) <= 7
    everything = _overview(client, period="all")
    assert everything["range"]["compare"] is None and everything["kpis"]["new_customers"]["previous"] is None


def test_service_health_uses_the_services_metrics(client, catalog, service_app):
    signup(client)
    db = SessionLocal()
    account_id = db.query(Account).first().id
    db.close()
    service_app.metrics = {
        "service": "biometric", "version": "1.0.0", "status": "degraded",
        "integrations": {"active": 4, "failed": 1}, "jobs": {"pending": 7, "oldest_pending_at": None},
        "last_success_at": None, "workers": {"scheduler": {"ok": True, "last_run_at": None}},
        "accounts": [{"account_id": account_id, "active": 4, "failed": 1,
                      "failures": [{"key": "devices", "issue": "Sync failing",
                                    "detail": "1 of 4 devices not syncing", "since": None}]}],
    }
    o = _overview(client)
    bio = next(s for s in o["services"] if s["slug"] == "biometric")
    assert bio["status"] == "degraded" and bio["customers"] == 1 and bio["integrations"]["failed"] == 1
    assert o["kpis"]["failed_integrations"]["value"] == 1
    assert bio["jobs"]["pending"] == 7 and bio["availability_pct"] == 100.0
    assert o["core"]["slug"] == "core" and o["core"]["database"]["ok"] is True
    assert all(sv["slug"] != "core" for sv in o["services"])          # Core has its own tile, not a row
    failing = next(i for i in o["incidents"] if i["key"].startswith("int:"))
    assert failing["issue"] == "Sync failing" and failing["detail"] == "1 of 4 devices not syncing"
    assert any(i["key"] == "svc:biometric" and i["severity"] == "high" for i in o["incidents"])
    assert failing["category"] == "integration"


def test_a_service_that_does_not_answer_is_unavailable(client, catalog, service_app):
    service_app.down = True
    o = _overview(client)
    bio = next(s for s in o["services"] if s["slug"] == "biometric")
    assert bio["status"] == "unavailable" and bio["down_since"]
    assert o["incidents"][0]["severity"] == "critical"
    # A service without the metrics endpoint is still up or down, just without numbers.
    service_app.down = False
    ops.forget()
    o = _overview(client)
    bio = next(s for s in o["services"] if s["slug"] == "biometric")
    assert bio["status"] == "operational" and bio["has_metrics"] is False


def test_failed_odoo_and_past_due_need_attention(client, catalog, service_app):
    signup(client)
    db = SessionLocal()
    account = db.query(Account).first()
    row = db.query(AccountService).first()
    row.status = ServiceStatus.past_due.value
    row.trial_ends_at = utcnow() - timedelta(days=1)
    db.add(OdooConnection(account_id=account.id, url="https://acme.odoo.com", db_name="acme", login="a",
                          api_key_enc="x", url_key="acme.odoo.com", db_key="acme", status="failed",
                          status_message="Odoo rejected the login or API key.",
                          failing_since=utcnow() - timedelta(hours=3)))
    db.commit()
    db.close()
    o = _overview(client)
    issues = {i["key"].split(":")[0]: i for i in o["incidents"]}
    assert issues["odoo"]["severity"] == "high" and "rejected" in issues["odoo"]["detail"]
    assert issues["pay"]["issue"] == "Trial ended unpaid"
    assert o["mix"]["past_due"] == 1
    assert issues["odoo"]["category"] == "integration" and issues["pay"]["category"] == "payment"


def test_revenue_change_compares_with_the_snapshot_at_the_period_start(client, catalog, service_app):
    db = SessionLocal()
    snap = ops.record_snapshot(db, today=(utcnow() - timedelta(days=40)).date())
    snap.mrr_cents = 1000
    db.commit()
    db.close()
    o = _overview(client, period="30d")
    assert o["kpis"]["mrr"]["previous"] == 1000
    assert o["kpis"]["mrr"]["change_pct"] == -100.0   # nobody pays online in the test catalog
    assert o["revenue"]["summary"]["growth_pct"] == -100.0


def test_a_service_dashboard_shows_each_customer_not_totals(client, catalog, service_app):
    signup(client)
    db = SessionLocal()
    account_id = db.query(Account).first().id
    db.close()
    service_app.metrics = {
        "status": "operational", "integrations": {"active": 2, "failed": 0}, "jobs": {"pending": 0},
        "workers": {"scheduler": {"ok": True, "last_run_at": None}}, "accounts": [],
        "dashboard": {
            "tiles": [], "series": None,
            "columns": [{"key": "sync_enabled", "label": "Automatic sync", "kind": "onoff"},
                        {"key": "activity", "label": "Activity", "kind": "activity"},
                        {"key": "health", "label": "Scheduler health", "kind": "health"}],
            "accounts": {account_id: {"sync_enabled": True, "activity": "active", "health": "delayed"}},
            "attention": [{"account_id": account_id, "severity": "medium", "title": "Sync delayed",
                           "detail": "No sync yet; interval 15 min.", "since": None},
                          {"account_id": "someone-else", "severity": "high", "title": "x", "detail": "y"}],
        },
    }
    r = client.get("/api/v1/staff/services/biometric/dashboard?fresh=true", headers=staff_headers(client))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["service"]["name"] == "BioBridge" and d["service"]["has_dashboard"] is True
    # The tiles are the core's own: customers and how they pay, plus sync issues.
    tiles = {t["key"]: t["value"] for t in d["tiles"]}
    assert tiles == {"customers": 1, "trialing": 1, "active": 0, "past_due": 0, "sync_issues": 1}
    [row] = d["customers"]
    assert row["name"] == "Acme Ltd" and row["plan"] == "Starter" and row["since"]
    assert row["billing"]["label"] == "Trial ends" and row["billing"]["date"]
    assert row["values"] == {"sync_enabled": True, "activity": "active", "health": "delayed"}
    # Only this service's own customers' notices, named from the core.
    assert [(a["title"], a["account"]) for a in d["attention"]] == [("Sync delayed", "Acme Ltd")]
    assert client.get("/api/v1/staff/services/nope/dashboard", headers=staff_headers(client)).status_code == 404


def test_customer_history_counts_a_deleted_account_only_while_it_existed(client, catalog, service_app):
    signup(client)
    signup(client, email="b@beta.io", company="Beta")
    staff = staff_headers(client)
    beta = next(a for a in client.get("/api/v1/staff/accounts", headers=staff).json() if a["name"] == "Beta")
    r = client.post(f"/api/v1/staff/accounts/{beta['id']}/delete", headers=staff, json={"reason": "test"})
    assert r.status_code == 200, r.text
    o = _overview(client, period="12m")
    # A deleted account still counts as acquired when it signed up.
    assert o["acquisition"]["buckets"][-1]["new"] == 2
    assert all(m["new"] == 0 for m in o["acquisition"]["buckets"][:-1])
    assert any(a["title"] == "Account deleted" for a in o["activity"])



def test_needs_attention_covers_customers_not_just_failures(client, catalog, service_app):
    signup(client)
    db = SessionLocal()
    account = db.query(Account).first()
    account.created_at = utcnow() - timedelta(days=5)
    row = db.query(AccountService).first()
    row.trial_ends_at = utcnow() + timedelta(days=2)
    db.commit()
    db.close()
    o = _overview(client)
    by = {i["category"]: i for i in o["incidents"]}
    assert by["trial"]["issue"] == "Trial ending soon" and by["trial"]["severity"] == "low"
    assert by["onboarding"]["detail"] == "Odoo is not connected yet."


def test_revenue_events_feed_new_revenue_churn_and_conversion(client, catalog, fake_stripe, service_app):
    from obcore.models import BillingEvent
    from obcore.services import billing

    signup(client)
    db = SessionLocal()
    account = db.query(Account).first()
    row = db.query(AccountService).first()
    row.stripe_item_id, row.status = "si_1", ServiceStatus.active.value
    account.stripe_subscription_id = "sub_1"
    event = billing.note_revenue(db, account)
    assert event.kind == "new" and event.mrr_after_cents == 2900 and account.first_paid_at
    db.add(BillingEvent(account_id=account.id, account_name=account.name, kind="payment_failed",
                        amount_cents=2900, external_id="fail:in_1:1"))
    db.commit()
    db.close()
    o = _overview(client)
    s = o["revenue"]["summary"]
    assert s["new_cents"] == 2900 and s["failed_payments"] == 1
    assert o["kpis"]["trial_conversion"]["converted"] == 1 and o["kpis"]["trial_conversion"]["value"] == 100.0
    assert o["kpis"]["active_customers"]["value"] == 1
    assert any(a["title"] == "Subscription activated" for a in o["activity"])

    db = SessionLocal()
    account = db.query(Account).first()
    db.query(AccountService).first().status = ServiceStatus.cancelled.value
    assert billing.note_revenue(db, account).kind == "churn"
    db.commit()
    db.close()
    assert _overview(client)["revenue"]["summary"]["churned_cents"] == 2900


def test_funnel_never_exceeds_its_top_and_counts_closed_accounts(client, catalog, service_app):
    from obcore.models import AccountClosure

    signup(client)
    signup(client, email="b@beta.io", company="Beta")
    db = SessionLocal()
    db.add(AccountClosure(account_id="x" * 32, account_name="Gone", owner_email="g@gone.io", services=["biobridge"],
                          closed_by="staff", account_created_at=utcnow() - timedelta(days=2)))
    db.commit()
    o = _overview(client)
    funnel = o["funnel"]
    values = [f["value"] for f in funnel]
    assert values == sorted(values, reverse=True)                 # never wider than the stage before it
    assert funnel[0]["value"] == 3 and funnel[0]["pct_top"] == 100.0
    assert all(f["pct_prev"] is None or f["pct_prev"] <= 100 for f in funnel)
    assert o["funnel_closed"] == 1
    # 3 customers came in, one of them has since closed: that is the gap to the 2 live ones.
    assert o["kpis"]["new_customers"]["value"] == 3 and o["kpis"]["new_customers"]["closed"] == 1
    assert sum(o["mix"].values()) == 2
    assert o["kpis"]["trial_conversion"]["running"] == 2


def test_churn_explains_itself_when_it_cannot_be_worked_out(client, catalog, service_app):
    signup(client)
    churn = _overview(client)["kpis"]["churn"]
    assert churn["value"] is None and churn["why"]


def test_revenue_summary_has_net_new_instead_of_arr(client, catalog, service_app):
    signup(client)
    summary = _overview(client)["revenue"]["summary"]
    assert "arr_cents" not in summary and summary["net_new_cents"] == 0
    assert _overview(client)["revenue"]["buckets"] == [] or all(b["mrr_cents"] is not None for b in _overview(client)["revenue"]["buckets"])


# --- Service Health --------------------------------------------------------------
@pytest.fixture()
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def test_response_is_a_p95_and_failed_checks_are_counted_apart(db_session):
    from obcore.models import ServiceCheck

    now = utcnow()
    for i in range(20):                                        # 19 fast answers and one slow
        db_session.add(ServiceCheck(service_slug="biometric", ok=True, latency_ms=900 if i == 0 else 100,
                                    created_at=now - timedelta(minutes=i + 1)))
    for i in range(5):
        db_session.add(ServiceCheck(service_slug="biometric", ok=False, latency_ms=None,
                                    created_at=now - timedelta(minutes=30 + i)))
    db_session.flush()
    h = ops.check_history(db_session, now=now)["biometric"]
    assert h["p95_latency_ms"] == 100 and h["avg_latency_ms"] == 140     # one outlier does not set the p95
    assert h["checks_24h"] == 25 and h["failed_checks_24h"] == 5
    assert h["availability_pct"] == 80.0 and h["days_of_data"] < 1


def test_p95_catches_a_slow_tail_that_the_average_hides(db_session):
    from obcore.models import ServiceCheck

    now = utcnow()
    for i in range(10):
        db_session.add(ServiceCheck(service_slug="biometric", ok=True, latency_ms=3000 if i < 2 else 80,
                                    created_at=now - timedelta(minutes=i + 1)))
    db_session.flush()
    assert ops.check_history(db_session, now=now)["biometric"]["p95_latency_ms"] == 3000


def test_outages_are_runs_of_failed_checks(db_session):
    from obcore.models import ServiceCheck

    now = utcnow()
    pattern = [True, True, False, False, False, True, True, False]       # oldest first; still failing at the end
    for i, ok in enumerate(pattern):
        db_session.add(ServiceCheck(service_slug="biometric", ok=ok, latency_ms=50 if ok else None,
                                    created_at=now - timedelta(minutes=(len(pattern) - i) * 10)))
    db_session.flush()
    detail = ops.history_detail(db_session, "biometric", now=now)
    assert len(detail["hours"]) == 24 and detail["checks"] == 8
    latest, earlier = detail["outages"]
    assert latest["until"] is None and latest["checks"] == 1               # newest first
    assert earlier["checks"] == 3 and earlier["until"] is not None


def test_history_endpoint_is_for_staff_and_for_known_services(client, catalog, service_app):
    assert client.get("/api/v1/staff/services/biometric/health-history").status_code in (401, 403)
    headers = staff_headers(client)
    assert client.get("/api/v1/staff/services/nope/health-history", headers=headers).status_code == 404
    body = client.get("/api/v1/staff/services/biometric/health-history", headers=headers).json()
    assert body["slug"] == "biometric" and len(body["hours"]) == 24


def test_a_degraded_service_says_why_and_an_unreachable_one_says_what_failed(client, catalog, service_app):
    service_app.metrics = {"service": "biometric", "version": "1", "status": "degraded",
                           "status_reason": "2 of 4 integrations are failing (the limit is 25%).",
                           "integrations": {"active": 4, "failed": 2}, "jobs": {"pending": 0}, "accounts": []}
    bio = next(s for s in _overview(client)["services"] if s["slug"] == "biometric")
    assert bio["status_reason"] == "2 of 4 integrations are failing (the limit is 25%)."
    service_app.metrics = {"service": "biometric", "version": "1", "status": "degraded", "accounts": []}
    bio = next(s for s in _overview(client)["services"] if s["slug"] == "biometric")
    assert "degraded" in bio["status_reason"]                       # a service that gives none still gets a sentence
    service_app.down = True
    bio = next(s for s in _overview(client)["services"] if s["slug"] == "biometric")
    assert bio["status"] == "unavailable" and bio["status_reason"]


def test_request_meter_ignores_monitoring_polls_and_counts_sign_in_errors_apart():
    from obcore import request_meter

    request_meter.reset()
    for _ in range(50):
        request_meter.note(200, "/health")
        request_meter.note(200, "/platform/v1/metrics")
    request_meter.note(200, "/api/v1/staff/overview")
    request_meter.note(401, "/api/v1/auth/staff/me")
    request_meter.note(403, "/api/v1/staff/accounts")
    request_meter.note(500, "/api/v1/staff/accounts")
    summary = request_meter.summary()
    assert (summary["total"], summary["failed"], summary["auth_failed"]) == (4, 1, 2)
    assert summary["started_at"] and summary["scope"] == "this process"
    request_meter.reset()


def test_core_has_its_own_tile_and_a_problem_raises_an_attention_item(client, catalog, service_app, monkeypatch):
    o = _overview(client)
    assert o["core"]["status"] == "operational" and not any(i["key"] == "svc:core" for i in o["incidents"])
    from obcore.config import settings

    monkeypatch.setattr(settings, "run_background_jobs", True)        # a sweep is expected, and none has run
    o = _overview(client)
    assert o["core"]["status"] == "degraded" and "sweep" in o["core"]["status_reason"]
    item = next(i for i in o["incidents"] if i["key"] == "svc:core")
    assert item["severity"] == "high" and item["category"] == "service" and "sweep" in item["detail"]

"""The staff overview: figures, service health and what needs attention."""

from __future__ import annotations

from datetime import timedelta

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
    assert k["customers"] == {"value": 2, "delta": 2}
    assert k["subscriptions"]["value"] == 2 and k["subscriptions"]["trials"] == 2
    assert o["mix"]["trial"] == 2
    assert o["growth"][-1]["total"] == 2 and o["growth"][-1]["added"] == 2 and len(o["growth"]) == 12
    assert {r["name"] for r in o["recent_customers"]} == {"Beta", "Acme Ltd"}
    # No revenue history before the first snapshot of the period start.
    assert k["mrr"]["delta_pct"] is None


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
    assert o["kpis"]["integrations"] == {"value": 4, "failed": 1, "pending_jobs": 7, "reported_by": 1}
    assert o["kpis"]["services"]["not_operational"] == ["BioBridge"]
    failing = next(i for i in o["incidents"] if i["key"].startswith("int:"))
    assert failing["issue"] == "Sync failing" and failing["detail"] == "1 of 4 devices not syncing"
    assert any(i["key"] == "svc:biometric" and i["severity"] == "high" for i in o["incidents"])
    assert o["customer_facts"]["with_failures"] == 1


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
    assert o["kpis"]["attention"]["by_severity"]["high"] >= 1


def test_revenue_change_compares_with_the_snapshot_at_the_period_start(client, catalog, service_app):
    db = SessionLocal()
    snap = ops.record_snapshot(db, today=(utcnow() - timedelta(days=40)).date())
    snap.mrr_cents = 1000
    db.commit()
    db.close()
    o = _overview(client, period="30d")
    assert o["kpis"]["mrr"]["has_history"] is True
    assert o["kpis"]["mrr"]["delta_pct"] == -100.0   # nobody pays online in the test catalog


def test_a_service_dashboard_joins_the_services_figures_with_its_customers(client, catalog, service_app):
    signup(client)
    db = SessionLocal()
    account_id = db.query(Account).first().id
    db.close()
    service_app.metrics = {
        "status": "operational", "integrations": {"active": 2, "failed": 0}, "jobs": {"pending": 0},
        "workers": {"scheduler": {"ok": True, "last_run_at": None}}, "accounts": [],
        "dashboard": {
            "tiles": [{"key": "devices", "label": "Devices Connected", "value": 2, "icon": "device"}],
            "series": {"title": "Punches Synced Per Day", "unit": "punches",
                       "points": [{"date": "2026-10-01", "value": 5}]},
            "columns": [{"key": "devices", "label": "Devices", "kind": "usage"},
                        {"key": "health", "label": "Sync health", "kind": "health"}],
            "accounts": {account_id: {"devices": {"used": 2, "limit": 2}, "health": "healthy"}},
            "attention": [{"account_id": account_id, "severity": "medium", "title": "At device limit",
                           "detail": "2 of 2 devices on Starter.", "since": None},
                          {"account_id": "someone-else", "severity": "high", "title": "x", "detail": "y"}],
        },
    }
    r = client.get("/api/v1/staff/services/biometric/dashboard?fresh=true", headers=staff_headers(client))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["service"]["name"] == "BioBridge" and d["service"]["has_dashboard"] is True
    assert d["tiles"][0]["value"] == 2 and d["series"]["points"][0]["value"] == 5
    [row] = d["customers"]
    assert row["name"] == "Acme Ltd" and row["plan"] == "Starter" and row["values"]["devices"] == {"used": 2, "limit": 2}
    # Only this service's own customers' notices, named from the core.
    assert [(a["title"], a["account"]) for a in d["attention"]] == [("At device limit", "Acme Ltd")]
    assert client.get("/api/v1/staff/services/nope/dashboard", headers=staff_headers(client)).status_code == 404

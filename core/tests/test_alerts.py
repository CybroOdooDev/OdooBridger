"""Alerts from Needs Attention: once when it starts, reminders, once when it clears."""

from __future__ import annotations

from datetime import timedelta

import httpx

from obcore.db import SessionLocal
from obcore.mailer import OUTBOX
from obcore.models import AlertState
from obcore.security import utcnow
from obcore.services import alerts
from tests.conftest import staff_headers

ODOO = {"key": "odoo:1", "severity": "high", "issue": "Odoo connection failing", "detail": "refused",
        "account_id": "a1", "account": "Acme Ltd", "service": "Odoo"}
SLOW = {"key": "sync:2", "severity": "medium", "issue": "Change not picked up yet", "detail": None,
        "account_id": "a2", "account": "Globex", "service": "BioBridge"}


def _alert_mails():
    return [m for m in OUTBOX if "Needs attention" in m["subject"]]


def test_alerts_once_reminds_and_reports_the_fix(client, catalog):
    db = SessionLocal()
    OUTBOX.clear()
    t0 = utcnow()
    # Seen, but inside the grace period: nothing yet. Medium is below the default floor.
    assert alerts.check(db, now=t0, incidents=[ODOO, SLOW])["new"] == 0
    assert _alert_mails() == []
    # After the grace period: one message, to every active staff member.
    assert alerts.check(db, now=t0 + timedelta(minutes=4), incidents=[ODOO, SLOW])["new"] == 1
    [mail] = _alert_mails()
    assert mail["to"] == "staff@odoobridger.io" and "HIGH" in mail["subject"]
    assert "Odoo connection failing — Acme Ltd · Odoo" in mail["body"] and "Globex" not in mail["body"]
    # Still open a minute later: no repeat.
    assert alerts.check(db, now=t0 + timedelta(minutes=5), incidents=[ODOO])["new"] == 0
    assert len(_alert_mails()) == 1
    # After the reminder period: one reminder.
    assert alerts.check(db, now=t0 + timedelta(hours=7), incidents=[ODOO])["reminders"] == 1
    assert "STILL OPEN" in _alert_mails()[-1]["body"]
    # It clears: announced as resolved, with how long it lasted.
    assert alerts.check(db, now=t0 + timedelta(hours=8), incidents=[])["resolved"] == 1
    assert "RESOLVED" in _alert_mails()[-1]["body"] and "lasted 8 h" in _alert_mails()[-1]["body"]
    assert db.get(AlertState, "odoo:1").resolved_at is not None
    db.commit()
    db.close()


def test_a_problem_that_clears_within_the_grace_period_is_never_announced(client, catalog):
    db = SessionLocal()
    OUTBOX.clear()
    t0 = utcnow()
    alerts.check(db, now=t0, incidents=[ODOO])
    assert alerts.check(db, now=t0 + timedelta(minutes=1), incidents=[]) == {"new": 0, "reminders": 0, "resolved": 0}
    assert _alert_mails() == []
    db.close()


def test_webhook_and_settings(client, catalog, monkeypatch):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.read().decode())
        return httpx.Response(200)

    monkeypatch.setattr(alerts, "webhook_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    staff = staff_headers(client)
    assert client.put("/api/v1/staff/alerts", headers=staff,
                      json={"webhook_url": "http://insecure"}).status_code == 422
    r = client.put("/api/v1/staff/alerts", headers=staff, json={
        "webhook_url": "https://hooks.slack.test/T/B/secret", "min_severity": "medium", "all_staff": False,
        "emails": ["Ops@Example.com"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["settings"]["has_webhook"] and "secret" not in r.text      # the webhook is never shown back
    assert out["recipients"] == ["ops@example.com"]
    assert client.post("/api/v1/staff/alerts/test", headers=staff).status_code == 200
    assert posted and '"text"' in posted[-1]
    db = SessionLocal()
    t0 = utcnow()
    alerts.check(db, now=t0, incidents=[SLOW])
    assert alerts.check(db, now=t0 + timedelta(minutes=5), incidents=[SLOW])["new"] == 1   # medium counts now
    assert "Change not picked up yet" in posted[-1]
    db.close()

"""Sync Now / First Sync start a cycle in the background and are polled."""
from __future__ import annotations

import app.services.sync_engine as engine_mod
from app.models import SyncRun
from tests.conftest import FakeOdoo, FakeProvider
from tests.test_device_discovery import auth, client, make_odoo, signup  # noqa: F401


def test_start_returns_a_run_that_finishes_in_the_background(client, monkeypatch):
    token = signup(client, "Acme", "owner@acme.com")
    make_odoo(client, token)
    monkeypatch.setattr(engine_mod, "build_odoo_client", lambda t, c: FakeOdoo())
    monkeypatch.setattr(engine_mod, "build_source_provider", lambda t, s: FakeProvider([]))
    import app.db.session as session_mod

    monkeypatch.setattr(session_mod, "SessionLocal", client.SessionLocal)
    r = client.post("/api/v1/sync/start", headers=auth(token))
    assert r.status_code == 202, r.text
    run_id = r.json()["id"]
    # TestClient runs the background task before returning.
    done = client.get(f"/api/v1/sync/runs/{run_id}", headers=auth(token)).json()
    assert done["status"] != "running" and done["finished_at"]
    db = client.SessionLocal()
    assert db.query(SyncRun).count() == 1
    other = signup(client, "Beta", "owner@beta.com")
    assert client.get(f"/api/v1/sync/runs/{run_id}", headers=auth(other)).status_code == 404

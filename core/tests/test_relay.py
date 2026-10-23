"""OdooBridger Agents: a private Odoo reached through the relay hub.

The agent here is simulated over the test client's WebSocket: a thread plays
the agent, answering each request from a fake Odoo that lives "on the
customer's network" (an address the core itself refuses to dial).
"""

from __future__ import annotations

import base64
import threading

import httpx
import pytest

from obcore.config import settings
from obcore.services import odoo_probe
from obcore.services.relay import hub
from tests.conftest import SERVICE_SECRET, signup
from tests.test_odoo_connection import GOOD_KEY, FakeOdoo

PRIVATE = "http://192.168.1.20:8069"


class FakeAgent:
    """Plays an OdooBridger Agent on a test WebSocket, in a thread."""

    def __init__(self, client, key: str, allowed: list[str], odoo: FakeOdoo):
        self.ws_cm = client.websocket_connect("/relay/v1/agent", headers={"Authorization": f"Bearer {key}"})
        self.ws = self.ws_cm.__enter__()
        self.allowed, self.odoo = allowed, odoo
        self.seen: list[str] = []
        self.ws.send_json({"type": "hello", "version": "1.0.0", "hostname": "office-pc", "allowed": allowed})
        self.welcome = self.ws.receive_json()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            while True:
                msg = self.ws.receive_json()
                if msg.get("type") != "request":
                    continue
                self.seen.append(msg["url"])
                request = httpx.Request(msg["method"], msg["url"], headers=msg["headers"],
                                        content=base64.b64decode(msg["body"]))
                response = self.odoo.handler(request)
                self.ws.send_json({"type": "response", "id": msg["id"], "status": response.status_code,
                                   "headers": dict(response.headers),
                                   "body": base64.b64encode(response.content).decode()})
        except Exception:  # noqa: BLE001 — the socket closed
            pass

    def close(self):
        try:
            self.ws_cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture()
def private_only(monkeypatch):
    """The core refuses private addresses, as in production."""
    monkeypatch.setattr(settings, "odoo_allow_private_targets", False)


def _agent(client, headers, name="Office server"):
    r = client.post("/api/v1/portal/agents", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _connect(client, headers, agent_id, **over):
    body = {"url": PRIVATE, "db": "acme", "login": "bot@acme.io", "api_key": GOOD_KEY, "agent_id": agent_id}
    body.update(over)
    return client.post("/api/v1/portal/odoo", json=body, headers=headers)


def test_a_private_odoo_is_reached_through_the_agent(client, catalog, private_only, service_app):
    headers = signup(client)
    # Directly: refused, it is a private address.
    r = client.post("/api/v1/portal/odoo", headers=headers, json={
        "url": PRIVATE, "db": "acme", "login": "bot@acme.io", "api_key": GOOD_KEY})
    assert r.status_code == 400 and "private network" in r.json()["detail"]

    created = _agent(client, headers)
    assert created["key"].startswith("obag_") and created["relay_url"].endswith("/relay/v1/agent")
    agent = FakeAgent(client, created["key"], [PRIVATE], FakeOdoo())
    try:
        assert agent.welcome == {"type": "welcome", "name": "Office server", "protocol": 1}
        listed = client.get("/api/v1/portal/agents", headers=headers).json()["agents"][0]
        assert listed["online"] is True and listed["allowed"] == [PRIVATE] and listed["hostname"] == "office-pc"

        r = _connect(client, headers, created["agent"]["id"])
        assert r.status_code == 201, r.text
        conn = r.json()["connections"][0]
        assert conn["status"] == "connected" and conn["version"] == "19.0"
        assert conn["agent"] == {"id": created["agent"]["id"], "name": "Office server", "online": True}
        assert all(u.startswith(PRIVATE) for u in agent.seen) and len(agent.seen) >= 3
        # The service is told to go through the relay.
        acc_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
        sent = service_app.payloads[("biometric", acc_id)]["odoo"]
        assert sent["url"] == PRIVATE and sent["relay"] == {"agent_id": created["agent"]["id"]}
    finally:
        agent.close()


def test_the_agent_only_reaches_what_it_allows_and_offline_is_explained(client, catalog, private_only):
    headers = signup(client)
    created = _agent(client, headers)
    r = _connect(client, headers, created["agent"]["id"])
    assert r.status_code == 400 and "offline" in r.json()["detail"]

    agent = FakeAgent(client, created["key"], ["http://192.168.1.99:8069"], FakeOdoo())
    try:
        r = _connect(client, headers, created["agent"]["id"])
        assert r.status_code == 400 and "not set up to reach http://192.168.1.20:8069" in r.json()["detail"]
        assert agent.seen == []          # never sent to the agent at all
    finally:
        agent.close()


def test_a_wrong_or_removed_key_is_refused(client, catalog):
    from starlette.websockets import WebSocketDisconnect

    headers = signup(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/relay/v1/agent", headers={"Authorization": "Bearer obag_nope"}) as ws:
            ws.receive_json()
    created = _agent(client, headers)
    client.delete(f"/api/v1/portal/agents/{created['agent']['id']}", headers=headers)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/relay/v1/agent",
                                      headers={"Authorization": f"Bearer {created['key']}"}) as ws:
            ws.receive_json()


def test_an_agent_in_use_cant_be_removed_and_a_new_key_cuts_the_old_one(client, catalog, private_only):
    headers = signup(client)
    created = _agent(client, headers)
    agent = FakeAgent(client, created["key"], [PRIVATE], FakeOdoo())
    try:
        assert _connect(client, headers, created["agent"]["id"]).status_code == 201
        r = client.delete(f"/api/v1/portal/agents/{created['agent']['id']}", headers=headers)
        assert r.status_code == 409 and "Change that connection first" in r.json()["detail"]
        r = client.post(f"/api/v1/portal/agents/{created['agent']['id']}/key", headers=headers)
        assert r.status_code == 200 and r.json()["key"] != created["key"]
        assert not hub.online(created["agent"]["id"])
    finally:
        agent.close()


def _forward(client, account_id, connection_id, secret=SERVICE_SECRET, slug="biometric"):
    auth = base64.b64encode(f"{slug}:{secret}".encode()).decode()
    return client.post("/api/v1/relay/forward", headers={"Authorization": f"Basic {auth}"}, json={
        "account_id": account_id, "connection_id": connection_id, "method": "GET",
        "url": f"{PRIVATE}/web/version", "headers": {}, "body": ""})


def test_services_forward_only_for_their_own_bound_connection(client, catalog, private_only):
    headers = signup(client)
    created = _agent(client, headers)
    agent = FakeAgent(client, created["key"], [PRIVATE], FakeOdoo())
    try:
        conn_id = _connect(client, headers, created["agent"]["id"]).json()["connections"][0]["id"]
        acc_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
        r = _forward(client, acc_id, conn_id)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == 200 and b"19.0" in base64.b64decode(r.json()["body"])
        assert _forward(client, acc_id, conn_id, secret="wrong").status_code == 401
        assert _forward(client, acc_id, conn_id, slug="shipping", secret="ship-secret").status_code == 403
        other = signup(client, email="eve@other.io", company="Other")
        other_id = client.get("/api/v1/portal/account", headers=other).json()["account"]["id"]
        assert _forward(client, other_id, conn_id).status_code == 403
    finally:
        agent.close()
    # Agent gone: the service gets a clear 503.
    r = _forward(client, acc_id, conn_id)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "offline"


def test_the_same_private_address_in_two_accounts_is_two_databases(client, catalog, private_only):
    first = signup(client)
    a1 = _agent(client, first)
    second = signup(client, email="eve@other.io", company="Other Ltd")
    a2 = _agent(client, second)
    g1 = FakeAgent(client, a1["key"], [PRIVATE], FakeOdoo(db_uuid="uuid-one"))
    g2 = FakeAgent(client, a2["key"], [PRIVATE], FakeOdoo(db_uuid="uuid-two"))
    try:
        assert _connect(client, first, a1["agent"]["id"]).status_code == 201
        assert _connect(client, second, a2["agent"]["id"]).status_code == 201
    finally:
        g1.close()
        g2.close()


def test_one_database_through_an_agent_and_directly_is_still_one(client, catalog, private_only, monkeypatch):
    first = signup(client)
    a1 = _agent(client, first)
    g1 = FakeAgent(client, a1["key"], [PRIVATE], FakeOdoo(db_uuid="uuid-shared"))
    try:
        assert _connect(client, first, a1["agent"]["id"]).status_code == 201
    finally:
        g1.close()
    # Someone else reaches the same database by its public address.
    public = FakeOdoo(db_uuid="uuid-shared")
    monkeypatch.setattr(odoo_probe, "check_target", lambda url: None)
    monkeypatch.setattr(odoo_probe, "client_factory", lambda: httpx.Client(
        transport=httpx.MockTransport(public.handler), follow_redirects=False))
    second = signup(client, email="eve@other.io", company="Other Ltd")
    r = client.post("/api/v1/portal/odoo", headers=second, json={
        "url": "https://erp.acme.example", "db": "acme", "login": "bot@acme.io", "api_key": GOOD_KEY})
    assert r.status_code == 400 and "another OdooBridger account" in r.json()["detail"]


def test_the_agent_download_is_the_agent(client):
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    canonical = (root / "agent" / "odoobridger_agent.py").read_text()
    served = client.get("/downloads/odoobridger_agent.py")
    assert served.status_code == 200 and served.text == canonical, (
        "core/obcore/static/downloads/odoobridger_agent.py is out of date: copy agent/odoobridger_agent.py there")

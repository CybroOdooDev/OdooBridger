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

    def __init__(self, client, key: str, allowed: list[str], odoo: FakeOdoo, features: list[str] | None = None):
        self.ws_cm = client.websocket_connect("/relay/v1/agent", headers={"Authorization": f"Bearer {key}"})
        self.ws = self.ws_cm.__enter__()
        self.allowed, self.odoo = allowed, odoo
        self.seen: list[str] = []
        self.configs: list[list[str]] = []
        hello = {"type": "hello", "version": "1.0.0", "hostname": "office-pc", "allowed": allowed}
        if features is not None:
            hello.update(version="1.1.0", features=features)
        self.ws.send_json(hello)
        self.welcome = self.ws.receive_json()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            while True:
                msg = self.ws.receive_json()
                if msg.get("type") == "config":
                    self.configs.append(msg["allowed"])
                    continue
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
        # An older agent (no "config"): its own OB_ALLOW decides, and the portal says to update it.
        assert listed["online"] is True and listed["limited_to"] == [PRIVATE] and listed["hostname"] == "office-pc"
        assert listed["outdated"] is True

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
        assert r.status_code == 400 and "not set up to reach 192.168.1.20:8069" in r.json()["detail"]
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


# --- a device on the customer's network: the service names the agent, not an Odoo connection ----
DEVICE = "http://192.168.1.50:80"


def _forward_agent(client, account_id, agent_id, url=f"{DEVICE}/ISAPI/System/deviceInfo", secret=SERVICE_SECRET,
                   slug="biometric"):
    auth = base64.b64encode(f"{slug}:{secret}".encode()).decode()
    return client.post("/api/v1/relay/forward", headers={"Authorization": f"Basic {auth}"}, json={
        "account_id": account_id, "agent_id": agent_id, "method": "GET", "url": url, "headers": {}, "body": ""})


def test_a_service_reaches_a_device_in_the_agents_range_and_nothing_else(client, catalog, private_only):
    headers = signup(client)
    created = _agent(client, headers)
    agent_id = created["agent"]["id"]
    acc_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
    agent = FakeAgent(client, created["key"], ["192.168.1.0/24@80+443"], FakeOdoo())
    try:
        r = _forward_agent(client, acc_id, agent_id)
        assert r.status_code == 200, r.text
        assert agent.seen == [f"{DEVICE}/ISAPI/System/deviceInfo"]
        # Outside the range, on a port that was not allowed, or by name: refused before the agent sees it.
        for url in ("http://192.168.2.9/x", "http://192.168.1.50:8069/x", "http://printer.lan/x",
                    "ftp://192.168.1.50/x"):
            r = _forward_agent(client, acc_id, agent_id, url=url)
            assert r.status_code == 503 and r.json()["detail"]["code"] == "not_allowed", url
        assert len(agent.seen) == 1
        # Listed for the service, with what the agent says it may reach.
        auth = base64.b64encode(f"biometric:{SERVICE_SECRET}".encode()).decode()
        listed = client.get(f"/api/v1/relay/agents?account_id={acc_id}", headers={"Authorization": f"Basic {auth}"})
        assert listed.status_code == 200
        assert listed.json()["agents"] == [{"id": agent_id, "name": "Office server", "online": True, "allowed": []}]
    finally:
        agent.close()


def test_an_agent_is_only_usable_by_its_own_accounts_services(client, catalog, private_only):
    headers = signup(client)
    created = _agent(client, headers)
    acc_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
    other = signup(client, email="eve@other.io", company="Other")
    other_id = client.get("/api/v1/portal/account", headers=other).json()["account"]["id"]
    agent = FakeAgent(client, created["key"], ["192.168.1.0/24"], FakeOdoo())
    try:
        # Someone else's account naming this agent, a service that is not on the account, bad credentials.
        assert _forward_agent(client, other_id, created["agent"]["id"]).status_code == 403
        assert _forward_agent(client, acc_id, created["agent"]["id"], slug="shipping",
                              secret="ship-secret").status_code == 403
        assert _forward_agent(client, acc_id, created["agent"]["id"], secret="wrong").status_code == 401
        auth = base64.b64encode(f"biometric:{SERVICE_SECRET}".encode()).decode()
        assert client.get(f"/api/v1/relay/agents?account_id={other_id}",
                          headers={"Authorization": f"Basic {auth}"}).json()["agents"] == []
        # Exactly one of connection_id / agent_id.
        both = client.post("/api/v1/relay/forward", headers={"Authorization": f"Basic {auth}"}, json={
            "account_id": acc_id, "method": "GET", "url": f"{DEVICE}/x"})
        assert both.status_code == 400
    finally:
        agent.close()
    client.delete(f"/api/v1/portal/agents/{created['agent']['id']}", headers=headers)
    assert _forward_agent(client, acc_id, created["agent"]["id"]).status_code == 403


def test_the_agent_and_the_core_read_allow_rules_the_same_way():
    import importlib.util
    from pathlib import Path

    from obcore.services.relay import allows

    path = Path(__file__).resolve().parents[2] / "agent" / "odoobridger_agent.py"
    spec = importlib.util.spec_from_file_location("ob_agent_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rules = ["http://192.168.1.20:8069", "192.168.1.0/24@80+4370", "10.0.5.0/28"]
    config = mod.Config({"OB_AGENT_KEY": "obag_x", "OB_RELAY_URL": "ws://x", "OB_ALLOW": ",".join(rules)})
    assert config.problems() == []
    urls = ["http://192.168.1.20:8069/web", "http://192.168.1.50/a", "https://192.168.1.50/a",
            "http://192.168.1.50:4370/a", "http://192.168.1.50:8069/a", "http://192.168.2.1/a",
            "http://10.0.5.3/a", "http://10.0.5.30/a", "http://example.com/a", "http://[::1]/a", "ftp://192.168.1.50/a"]
    for url in urls:
        assert config.allows(url) == allows(rules, url), url
    assert [u for u in urls if config.allows(u)] == [
        "http://192.168.1.20:8069/web", "http://192.168.1.50/a", "http://192.168.1.50:4370/a", "http://10.0.5.3/a"]
    # host:port entries (what OdooBridger manages), for web requests and raw TCP alike.
    managed = ["192.168.1.20:8069", "10.0.11.43:4370", "10.0.12.0/24@80+4370", "http://odoo.lan:8069"]
    from obcore.services.relay import allows_tcp
    for url in urls + ["http://odoo.lan:8069/web", "http://10.0.11.43:4370/x", "https://192.168.1.20:8069/"]:
        assert mod.any_rule(managed, url=url) == allows(managed, url), url
    for host, port in (("10.0.11.43", 4370), ("10.0.11.43", 80), ("10.0.12.9", 4370), ("odoo.lan", 8069),
                       ("192.168.1.20", 8069), ("10.0.13.1", 4370)):
        assert mod.any_rule(managed, host=host, port=port) == allows_tcp(managed, host, port), (host, port)
    assert allows_tcp(managed, "10.0.11.43", 4370) and not allows_tcp(managed, "10.0.11.43", 80)
    bad = mod.Config({"OB_AGENT_KEY": "obag_x", "OB_RELAY_URL": "ws://x", "OB_ALLOW": "192.168.1.0/99,foo@80"})
    assert len(bad.problems()) == 2


# --- raw TCP: a device that speaks its own protocol (ZKTeco, port 4370) ----------------
class DeviceAgent(FakeAgent):
    """An agent that, besides HTTP, holds TCP connections: the 'device' upper-cases what it is sent."""

    def __init__(self, *a, **kw):
        self.opened: list[tuple[str, int]] = []
        self.closed: list[str] = []
        super().__init__(*a, **kw)

    def _serve(self):
        try:
            while True:
                msg = self.ws.receive_json()
                kind = msg.get("type")
                if kind == "open":
                    self.opened.append((msg["host"], msg["port"]))
                    self.ws.send_json({"type": "opened", "id": msg["id"]})
                elif kind == "data":
                    data = base64.b64decode(msg["data"]).upper()
                    self.ws.send_json({"type": "data", "id": msg["id"], "data": base64.b64encode(data).decode()})
                elif kind == "close":
                    self.closed.append(msg["id"])
        except Exception:  # noqa: BLE001 — the socket closed
            pass


def _stream(client, account_id, agent_id, host="10.0.11.43", port=4370, secret=SERVICE_SECRET, slug="biometric"):
    auth = base64.b64encode(f"{slug}:{secret}".encode()).decode()
    return client.websocket_connect(
        f"/api/v1/relay/stream?account_id={account_id}&agent_id={agent_id}&host={host}&port={port}",
        headers={"Authorization": f"Basic {auth}"})


def test_a_service_holds_a_tcp_connection_to_a_device_through_the_agent(client, catalog, private_only):
    import time

    headers = signup(client)
    created = _agent(client, headers)
    acc_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
    agent = DeviceAgent(client, created["key"], ["10.0.11.0/24@4370"], FakeOdoo())
    try:
        with _stream(client, acc_id, created["agent"]["id"]) as ws:
            assert ws.receive_json() == {"type": "opened"}
            ws.send_bytes(b"zk hello")
            assert ws.receive_bytes() == b"ZK HELLO"
            ws.send_bytes(b"again")
            assert ws.receive_bytes() == b"AGAIN"
        for _ in range(30):
            if agent.closed:
                break
            time.sleep(0.1)
        assert agent.opened == [("10.0.11.43", 4370)]
        assert len(agent.closed) == 1            # the agent is told to drop the device connection
        assert hub.stream_count(acc_id) == 0
    finally:
        agent.close()


def test_a_tcp_connection_outside_what_the_agent_allows_is_refused(client, catalog, private_only):
    headers = signup(client)
    created = _agent(client, headers)
    acc_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
    agent = DeviceAgent(client, created["key"], ["10.0.11.0/24@4370"], FakeOdoo())
    try:
        for host, port in (("10.0.12.5", 4370), ("10.0.11.43", 22)):
            with _stream(client, acc_id, created["agent"]["id"], host=host, port=port) as ws:
                answer = ws.receive_json()
                assert answer["type"] == "error" and answer["code"] == "not_allowed"
        assert agent.opened == []
    finally:
        agent.close()


def test_a_tcp_connection_needs_the_right_service_account_and_agent(client, catalog, private_only):
    from starlette.websockets import WebSocketDisconnect

    headers = signup(client)
    created = _agent(client, headers)
    acc_id = client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]
    other = signup(client, email="eve@other.io", company="Other")
    other_id = client.get("/api/v1/portal/account", headers=other).json()["account"]["id"]
    agent = DeviceAgent(client, created["key"], ["10.0.11.0/24@4370"], FakeOdoo())
    try:
        for kwargs in ({"account_id": other_id}, {"secret": "wrong"}, {"slug": "shipping", "secret": "ship-secret"}):
            args = {"account_id": acc_id, **kwargs}
            with pytest.raises(WebSocketDisconnect):
                with _stream(client, args.pop("account_id"), created["agent"]["id"], **args) as ws:
                    ws.receive_json()
        assert agent.opened == []
    finally:
        agent.close()
    with _stream(client, acc_id, created["agent"]["id"]) as ws:          # agent gone
        assert ws.receive_json()["code"] == "offline"

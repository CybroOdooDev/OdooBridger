"""Platform mode: a standalone terminal on the customer's own network, reached
through their OdooBridger Agent via the core's relay.

The core's relay is simulated; behind it sits the same fake Hikvision terminal
the provider's own tests use (real HTTP Digest, so the challenge round trip goes
through the relay too).
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.core.config import settings
from app.integrations import relay as relay_mod
from app.integrations.relay import list_agents
from app.models import DeviceSource, Tenant
from app.services import connections as conn_mod
from tests.test_hikvision import PASSWORD, SERIAL, USER, FakeTerminal
from tests.test_platform_mode import client  # noqa: F401 — the platform-mode app fixture

DEVICE = "http://192.168.1.50"
AGENT = "g" * 32
ACCOUNT = "a" * 32


@pytest.fixture
def core(monkeypatch):
    seen: list[dict] = []
    added: list[dict] = []
    terminal = FakeTerminal()
    state = {"offline": False}

    def handler(request: httpx.Request) -> httpx.Response:
        auth = base64.b64decode(request.headers["authorization"].split()[1]).decode()
        assert auth == "biometric:s3cret"
        if request.method == "GET":
            assert request.url.path == "/api/v1/relay/agents"
            return httpx.Response(200, json={"agents": [{"id": AGENT, "name": "Office", "online": True,
                                                         "allowed": ["192.168.1.0/24@80"]}]})
        payload = json.loads(request.content)
        if request.url.path.endswith("/addresses"):
            assert request.url.path == f"/api/v1/relay/agents/{AGENT}/addresses"
            added.append(payload)
            return httpx.Response(200, json={"address": payload["address"], "allowed": [payload["address"]]})
        seen.append(payload)
        if state["offline"]:
            return httpx.Response(503, json={"detail": {"code": "offline", "message":
                                  "The OdooBridger Agent on your network is offline."}})
        inner = httpx.Request(payload["method"], payload["url"], headers=payload["headers"],
                              content=base64.b64decode(payload["body"]))
        answer = terminal(inner)
        return httpx.Response(200, json={"status": answer.status_code, "headers": dict(answer.headers),
                                         "body": base64.b64encode(answer.content).decode()})

    monkeypatch.setattr(settings, "platform_mode", True)
    monkeypatch.setattr(settings, "platform_core_url", "https://core.test")
    monkeypatch.setattr(settings, "platform_client_secret", "s3cret")
    monkeypatch.setattr(settings, "allow_private_network_targets", False)
    monkeypatch.setattr(relay_mod, "client_factory", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler)))
    return {"seen": seen, "state": state, "terminal": terminal, "added": added}


def _source(db, provider="hik_isapi", agent=AGENT):
    from app.core.crypto import encrypt

    tenant = Tenant(name="Acme", slug="acme", status="active", platform_account_id=ACCOUNT)
    db.add(tenant)
    db.flush()
    source = DeviceSource(tenant_id=tenant.id, name="Front door", provider=provider, connection_kind="device",
                          base_url=DEVICE, username=USER, password_enc=encrypt(PASSWORD, tenant.crypto_key),
                          server_timezone="Asia/Kolkata", verify_ssl=False,
                          config={"relay_agent_id": agent} if agent else {})
    db.add(source)
    db.commit()
    return tenant, source


def test_a_private_device_is_reached_through_the_agent(db, core):
    tenant, source = _source(db)
    assert source.relay_agent_id == AGENT
    provider = conn_mod.build_source_provider(tenant, source)
    info = provider.test_connection()
    assert info.ok and SERIAL in info.message
    # Digest's challenge and the answer both went through the relay, by agent, never by connection.
    assert len(core["seen"]) >= 2
    assert {p["agent_id"] for p in core["seen"]} == {AGENT}
    assert all("connection_id" not in p and p["account_id"] == ACCOUNT for p in core["seen"])
    assert all(p["url"].startswith(DEVICE) for p in core["seen"])


def test_the_same_address_without_an_agent_is_refused(db, core):
    tenant, source = _source(db, agent=None)
    with pytest.raises(conn_mod.UnsafeTargetError):
        conn_mod.build_source_provider(tenant, source)
    assert core["seen"] == []


def test_a_push_device_is_never_dialled_so_an_agent_is_ignored(db, core):
    tenant, source = _source(db, provider="zk_adms")
    source.base_url = "adms://CKJG201760123"
    conn_mod.build_source_provider(tenant, source)      # nothing to reach, nothing relayed
    assert core["seen"] == []


def test_an_offline_agent_is_explained(db, core):
    from app.integrations.base import ProviderError

    tenant, source = _source(db)
    core["state"]["offline"] = True
    with pytest.raises(ProviderError) as caught:
        conn_mod.build_source_provider(tenant, source).test_connection()
    assert "offline" in relay_mod.explain(caught.value)


def test_the_customers_agents_are_listed_from_the_core(core):
    assert list_agents(ACCOUNT) == [{"id": AGENT, "name": "Office", "online": True,
                                     "allowed": ["192.168.1.0/24@80"]}]


def test_the_dashboard_picks_an_agent_for_a_device(client, core, monkeypatch):
    from sqlalchemy import select

    from tests.test_platform_mode import put, state
    from tests.test_platform_odoo import ODOO, _sign_in

    put(client, state(odoo=ODOO))
    headers = _sign_in(client, monkeypatch)
    monkeypatch.setattr(settings, "platform_mode", True)
    assert client.get("/api/v1/relay-agents", headers=headers).json()[0]["id"] == AGENT

    form = {"name": "Front door", "provider": "hik_isapi", "connection_kind": "device", "base_url": DEVICE,
            "username": USER, "password": PASSWORD, "server_timezone": "Asia/Kolkata", "verify_ssl": False}
    # Tested before saving, through the agent; without one the private address is refused.
    r = client.post("/api/v1/sources/test", json={**form, "relay_agent_id": AGENT}, headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    # The device's address was handed to the agent first; nothing to set on the customer's computer.
    assert core["added"] == [{"account_id": ACCOUNT, "address": DEVICE}]
    r = client.post("/api/v1/sources/test", json=form, headers=headers)
    assert r.json()["ok"] is False and "private address" in r.json()["message"]

    r = client.post("/api/v1/sources", json={**form, "relay_agent_id": AGENT}, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["relay_agent_id"] == AGENT
    source_id = r.json()["id"]
    # Switching back to a direct route removes it.
    r = client.patch(f"/api/v1/sources/{source_id}", json={"relay_agent_id": ""}, headers=headers)
    assert r.status_code == 200 and r.json()["relay_agent_id"] is None
    db = client.Session()
    assert "relay_agent_id" not in (db.scalar(select(DeviceSource)).config or {})
    db.close()

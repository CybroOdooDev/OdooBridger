"""An agent installed with only its key: its addresses are managed here and
reach it live, and it coming online re-checks what was waiting on it."""

from __future__ import annotations

import base64
import time

from tests.conftest import SERVICE_SECRET, signup
from tests.test_odoo_connection import GOOD_KEY, FakeOdoo
from tests.test_relay import PRIVATE, FakeAgent, _agent, _connect, private_only  # noqa: F401

NEW = ["config", "tcp"]


def _account_id(client, headers):
    return client.get("/api/v1/portal/account", headers=headers).json()["account"]["id"]


def _listed(client, headers):
    return client.get("/api/v1/portal/agents", headers=headers).json()["agents"][0]


def _wait(condition, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def _forward(client, account_id, agent_id, url):
    auth = base64.b64encode(f"biometric:{SERVICE_SECRET}".encode()).decode()
    return client.post("/api/v1/relay/forward", headers={"Authorization": f"Basic {auth}"}, json={
        "account_id": account_id, "agent_id": agent_id, "method": "GET", "url": url, "headers": {}, "body": ""})


def test_connecting_odoo_adds_its_address_and_the_running_agent_gets_it(client, catalog, private_only):
    headers = signup(client)
    created = _agent(client, headers)
    agent = FakeAgent(client, created["key"], [], FakeOdoo(), features=NEW)
    try:
        assert agent.welcome["allowed"] == []                # nothing yet, and nothing set on the computer
        listed = _listed(client, headers)
        assert listed["outdated"] is False and listed["limited_to"] == []
        r = _connect(client, headers, created["agent"]["id"])
        assert r.status_code == 201, r.text
        assert agent.configs[0] == ["192.168.1.20:8069"]    # sent before the first request
        assert all(u.startswith(PRIVATE) for u in agent.seen)
        assert _listed(client, headers)["allowed"] == ["192.168.1.20:8069"]
    finally:
        agent.close()


def test_the_customer_adds_and_removes_addresses_in_the_portal(client, catalog, private_only):
    headers = signup(client)
    created = _agent(client, headers)
    agent_id = created["agent"]["id"]
    acc_id = _account_id(client, headers)
    agent = FakeAgent(client, created["key"], [], FakeOdoo(), features=NEW)
    try:
        assert _connect(client, headers, agent_id).status_code == 201
        # Not an address of the agent: refused, saying where to add it.
        r = _forward(client, acc_id, agent_id, "http://192.168.1.50/ISAPI/System/deviceInfo")
        assert r.status_code == 503 and "Add Address" in r.json()["detail"]["message"]
        for pasted, stored in (("http://192.168.1.50/ISAPI", "192.168.1.50:80"), ("zk://10.0.11.43", "10.0.11.43:4370"),
                               ("10.0.12.0/24@4370+80", "10.0.12.0/24@80+4370")):
            r = client.post(f"/api/v1/portal/agents/{agent_id}/addresses", headers=headers, json={"address": pasted})
            assert r.status_code == 200, r.text
            assert stored in r.json()["agent"]["allowed"]
        assert agent.configs[-1] == ["192.168.1.20:8069", "192.168.1.50:80", "10.0.11.43:4370", "10.0.12.0/24@80+4370"]
        r = _forward(client, acc_id, agent_id, "http://192.168.1.50/ISAPI/System/deviceInfo")
        assert r.status_code == 200 and agent.seen[-1] == "http://192.168.1.50/ISAPI/System/deviceInfo"
        # Odoo's own address can't be taken away from under it.
        r = client.delete(f"/api/v1/portal/agents/{agent_id}/addresses", headers=headers,
                          params={"address": "192.168.1.20:8069"})
        assert r.status_code == 409 and "Change that connection first" in r.json()["detail"]
        r = client.delete(f"/api/v1/portal/agents/{agent_id}/addresses", headers=headers,
                          params={"address": "192.168.1.50:80"})
        assert r.status_code == 200 and "192.168.1.50:80" not in r.json()["agent"]["allowed"]
        assert "192.168.1.50:80" not in agent.configs[-1]
        # Things people get wrong are explained.
        for bad, words in (("192.168.1.50", "Add the port"), ("ftp://10.0.0.1:21", "not an address"),
                           ("10.0.0.0/99@80", "not a valid range")):
            r = client.post(f"/api/v1/portal/agents/{agent_id}/addresses", headers=headers, json={"address": bad})
            assert r.status_code == 400 and words in r.json()["detail"], (bad, r.json())
    finally:
        agent.close()
    # Reconnecting, it gets the current list straight away.
    again = FakeAgent(client, created["key"], [], FakeOdoo(), features=NEW)
    try:
        assert again.welcome["allowed"] == ["192.168.1.20:8069", "10.0.11.43:4370", "10.0.12.0/24@80+4370"]
    finally:
        again.close()


def test_a_service_adds_a_device_address_on_its_own_accounts_agent(client, catalog, private_only):
    headers = signup(client)
    created = _agent(client, headers)
    acc_id = _account_id(client, headers)
    other = signup(client, email="eve@other.io", company="Other")
    auth = {"Authorization": "Basic " + base64.b64encode(f"biometric:{SERVICE_SECRET}".encode()).decode()}
    url = f"/api/v1/relay/agents/{created['agent']['id']}/addresses"
    agent = FakeAgent(client, created["key"], [], FakeOdoo(), features=NEW)
    try:
        r = client.post(url, headers=auth, json={"account_id": acc_id, "address": "10.0.11.43:4370"})
        assert r.status_code == 200 and r.json()["address"] == "10.0.11.43:4370"
        assert agent.configs[-1] == ["10.0.11.43:4370"]
        assert client.post(url, headers=auth, json={"account_id": _account_id(client, other),
                                                    "address": "10.0.11.44:4370"}).status_code == 403
        bad = {"Authorization": "Basic " + base64.b64encode(b"biometric:wrong").decode()}
        assert client.post(url, headers=bad, json={"account_id": acc_id, "address": "10.0.11.44:4370"}).status_code == 401
    finally:
        agent.close()


def test_an_agent_coming_online_rechecks_its_odoo_and_the_services(client, catalog, private_only, service_app):
    headers = signup(client)
    created = _agent(client, headers)
    agent = FakeAgent(client, created["key"], [], FakeOdoo(), features=NEW)
    try:
        conn_id = _connect(client, headers, created["agent"]["id"]).json()["connections"][0]["id"]
    finally:
        agent.close()
    # The agent goes away; a test now fails.
    assert _wait(lambda: not _listed(client, headers)["online"])
    client.post(f"/api/v1/portal/odoo/{conn_id}/test", headers=headers)
    status = lambda: client.get("/api/v1/portal/odoo", headers=headers).json()["connections"][0]["status"]  # noqa: E731
    assert status() == "failed"
    service_app.usage_calls.clear()
    # It comes back: nothing to re-save, the connection recovers and services are asked to check live.
    again = FakeAgent(client, created["key"], [], FakeOdoo(), features=NEW)
    try:
        assert _wait(lambda: status() == "connected"), status()
        assert _wait(lambda: any(c.get("check_odoo") for c in service_app.usage_calls)), service_app.usage_calls
    finally:
        again.close()


def test_the_agent_itself_keeps_to_your_network_and_your_own_limit():
    import asyncio
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "agent" / "odoobridger_agent.py"
    spec = importlib.util.spec_from_file_location("ob_agent_limits", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    async def run():
        agent = mod.Agent(mod.Config({"OB_AGENT_KEY": "obag_x", "OB_RELAY_URL": "ws://x"}))
        assert agent.config.problems() == []                     # only the key and the address needed
        agent.managed = ["10.0.11.43:4370", "192.168.1.20:8069", "8.8.8.8:53", "127.0.0.1:8020"]
        assert await agent.permitted("10.0.11.43", 4370) is None
        assert await agent.permitted("127.0.0.1", 8020, "http://127.0.0.1:8020/web") is None
        assert "not one of this agent's addresses" in await agent.permitted("10.0.11.43", 22)
        # Even if OdooBridger listed it, nothing outside the customer's network.
        assert "not on your private network" in await agent.permitted("8.8.8.8", 53)
        limited = mod.Agent(mod.Config({"OB_AGENT_KEY": "obag_x", "OB_RELAY_URL": "ws://x",
                                        "OB_ALLOW": "10.0.11.0/24@4370"}))
        limited.managed = agent.managed
        assert await limited.permitted("10.0.11.43", 4370) is None
        assert "OB_ALLOW" in await limited.permitted("192.168.1.20", 8069, "http://192.168.1.20:8069/")
        # An older OdooBridger that sends no list: OB_ALLOW alone decides.
        legacy = mod.Agent(mod.Config({"OB_AGENT_KEY": "obag_x", "OB_RELAY_URL": "ws://x"}))
        assert "set OB_ALLOW" in await legacy.permitted("10.0.11.43", 4370)

    asyncio.run(run())

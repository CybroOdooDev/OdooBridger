"""Platform mode: a private Odoo reached through OdooBridger's relay.

The core's relay endpoint is simulated: it decodes the forwarded request and
answers from a fake Odoo, as the customer's agent would.
"""

from __future__ import annotations

import base64
import json
import xmlrpc.client

import httpx
import pytest

from app.core.config import settings
from app.core.crypto import encrypt
from app.integrations import relay as relay_mod
from app.integrations.odoo import OdooError
from app.models import OdooConnection, Tenant
from app.services import connections as conn_mod
from tests.test_platform_mode import client  # noqa: F401 — the platform-mode app fixture

PRIVATE = "http://192.168.1.20:8069"


class FakeOdoo17:
    """XML-RPC only, like Odoo 17 behind a firewall."""

    def handle(self, method: str, url: str, body: bytes) -> tuple[int, dict, bytes]:
        if url.endswith("/web/version"):
            return 404, {}, b""
        params, name = xmlrpc.client.loads(body)
        if name == "version":
            value = {"server_version": "17.0", "server_version_info": [17, 0, 0, "final", 0, ""]}
        elif name == "authenticate":
            value = 7 if params[2] == "the-key" else False
        elif name == "execute_kw":
            value = [{"id": 1, "name": "Acme HQ"}]
        else:
            value = False
        return 200, {"Content-Type": "text/xml"}, xmlrpc.client.dumps((value,), methodresponse=True).encode()


@pytest.fixture
def core(monkeypatch):
    """A stand-in for the core's /api/v1/relay/forward."""
    seen: list[dict] = []
    state = {"offline": False}
    odoo = FakeOdoo17()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/relay/forward"
        auth = base64.b64decode(request.headers["authorization"].split()[1]).decode()
        assert auth == "biometric:s3cret"
        payload = json.loads(request.content)
        seen.append(payload)
        if state["offline"]:
            return httpx.Response(503, json={"detail": {"code": "offline",
                                                        "message": "The OdooBridger Agent on your network is offline."}})
        status, headers, body = odoo.handle(payload["method"], payload["url"], base64.b64decode(payload["body"]))
        return httpx.Response(200, json={"status": status, "headers": headers,
                                         "body": base64.b64encode(body).decode()})

    monkeypatch.setattr(settings, "platform_mode", True)
    monkeypatch.setattr(settings, "platform_core_url", "https://core.test")
    monkeypatch.setattr(settings, "platform_client_secret", "s3cret")
    monkeypatch.setattr(settings, "allow_private_network_targets", False)
    monkeypatch.setattr(relay_mod, "client_factory",
                        lambda timeout: httpx.Client(transport=httpx.MockTransport(handler)))
    return {"seen": seen, "state": state}


def _setup(db):
    tenant = Tenant(name="Acme", slug="acme", status="active", platform_account_id="a" * 32)
    db.add(tenant)
    db.flush()
    conn = OdooConnection(tenant_id=tenant.id, name="OdooBridger", url=PRIVATE, db_name="acme",
                          username="bot@acme.io", api_key_enc=encrypt("the-key", tenant.crypto_key),
                          platform_connection_id="c" * 32, relay_agent_id="g" * 32)
    db.add(conn)
    db.commit()
    return tenant, conn


def test_a_relayed_connection_skips_the_private_address_guard_and_goes_through_the_core(db, core):
    tenant, conn = _setup(db)
    client = conn_mod.build_odoo_client(tenant, conn)
    assert client.authenticate() == 7          # XML-RPC, through the relay
    assert client.version()["server_version"] == "17.0"
    assert client.list_companies() == [{"id": 1, "name": "Acme HQ"}]
    assert {p["connection_id"] for p in core["seen"]} == {"c" * 32}
    assert {p["account_id"] for p in core["seen"]} == {"a" * 32}
    assert all(p["url"].startswith(PRIVATE) for p in core["seen"])


def test_the_same_address_without_the_agent_is_refused(db, core):
    tenant, conn = _setup(db)
    conn.relay_agent_id = None
    with pytest.raises(conn_mod.UnsafeTargetError):
        conn_mod.build_odoo_client(tenant, conn)


def test_an_offline_agent_reads_as_unreachable(db, core):
    tenant, conn = _setup(db)
    core["state"]["offline"] = True
    client = conn_mod.build_odoo_client(tenant, conn)
    with pytest.raises(OdooError, match="offline"):
        client.authenticate()


def test_provisioning_sets_and_clears_the_route(client):
    from sqlalchemy import select

    from tests.test_platform_mode import put, state
    from tests.test_platform_odoo import ODOO

    put(client, state(odoo={**ODOO, "url": PRIVATE, "relay": {"agent_id": "g" * 32}}))
    db = client.Session()
    conn = db.scalar(select(OdooConnection))
    assert conn.relay_agent_id == "g" * 32 and conn.url == PRIVATE
    db.close()
    put(client, state(odoo={**ODOO, "relay": None}, revision=2))
    db = client.Session()
    conn = db.scalar(select(OdooConnection))
    assert conn.relay_agent_id is None and conn.status == "unverified"
    db.close()

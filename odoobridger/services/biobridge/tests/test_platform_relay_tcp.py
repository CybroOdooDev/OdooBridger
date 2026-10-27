"""Platform mode: a ZKTeco terminal (raw TCP, port 4370) on the customer's network,
reached through their OdooBridger Agent.

The core's stream endpoint is simulated by a real local WebSocket server that
bridges each connection to a fake ZKTeco terminal over real TCP: what is under
test is BioBridge's socket-over-relay and the ZKTeco protocol running on it.
"""

from __future__ import annotations

import json
import socket
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.sync.server import serve

from app.core.config import settings
from app.integrations.base import ProviderError, SourceConfig
from app.integrations.providers.zkteco import ZKDeviceProvider
from app.integrations.relay import PlatformRelayTcp, explain
from tests.test_zkteco_protocol import FakeZKDevice

ACCOUNT, AGENT = "a" * 32, "g" * 32


class FakeCoreStream:
    """``/api/v1/relay/stream``: checks the service credentials, opens the device, pipes bytes."""

    def __init__(self, refuse: str | None = None) -> None:
        self.calls: list[dict] = []
        self.refuse = refuse
        self.server = serve(self._handle, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handle(self, ws) -> None:
        import base64

        parts = urlsplit(ws.request.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        auth = base64.b64decode(ws.request.headers["Authorization"].split()[1]).decode()
        self.calls.append({"path": parts.path, "auth": auth, **query})
        if self.refuse:
            ws.send(json.dumps({"type": "error", "code": "offline", "message": self.refuse}))
            return
        device = socket.create_connection((query["host"], int(query["port"])), timeout=5)
        ws.send(json.dumps({"type": "opened"}))

        def to_service() -> None:
            try:
                while chunk := device.recv(65536):
                    ws.send(chunk)
            except Exception:  # noqa: BLE001
                pass
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=to_service, daemon=True).start()
        try:
            for message in ws:
                if isinstance(message, bytes):
                    device.sendall(message)
        except Exception:  # noqa: BLE001
            pass
        finally:
            device.close()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def stream_core(monkeypatch):
    core = FakeCoreStream()
    monkeypatch.setattr(settings, "platform_core_url", f"http://127.0.0.1:{core.port}")
    monkeypatch.setattr(settings, "platform_client_secret", "s3cret")
    yield core
    core.close()


def _provider(device, tcp_connect) -> ZKDeviceProvider:
    return ZKDeviceProvider(SourceConfig(base_url=f"zk://{device.host}:{device.port}", timezone="UTC",
                                         tcp_connect=tcp_connect))


def test_a_zkteco_terminal_is_read_through_the_relay(stream_core):
    with FakeZKDevice(params={"~SerialNumber": "ABC123"}, attlog_count=7) as device:
        result = _provider(device, PlatformRelayTcp(ACCOUNT, AGENT)).test_connection()
    assert result.ok and "ABC123" in result.message and "7" in result.message
    (call,) = stream_core.calls
    assert call["path"] == "/api/v1/relay/stream" and call["auth"] == "biometric:s3cret"
    assert (call["account_id"], call["agent_id"], call["host"]) == (ACCOUNT, AGENT, device.host)
    assert int(call["port"]) == device.port


def test_a_terminal_that_demands_its_comm_key_still_connects_through_the_relay(stream_core):
    with FakeZKDevice(params={"~SerialNumber": "KEYED1"}, require_auth=True, comm_key=1234) as device:
        provider = ZKDeviceProvider(SourceConfig(base_url=f"zk://{device.host}:{device.port}", password="1234",
                                                 timezone="UTC", tcp_connect=PlatformRelayTcp(ACCOUNT, AGENT)))
        assert provider.test_connection().ok


def test_an_offline_or_refusing_agent_says_so_not_connection_refused(monkeypatch):
    core = FakeCoreStream(refuse="The OdooBridger Agent on your network is offline. Check that it is running.")
    monkeypatch.setattr(settings, "platform_core_url", f"http://127.0.0.1:{core.port}")
    monkeypatch.setattr(settings, "platform_client_secret", "s3cret")
    try:
        with FakeZKDevice() as device:
            result = _provider(device, PlatformRelayTcp(ACCOUNT, AGENT)).test_connection()
        assert not result.ok and "Agent on your network is offline" in result.message
        assert "Timed out" not in result.message and "refused by" not in result.message
    finally:
        core.close()


def test_the_core_not_answering_is_explained(monkeypatch):
    monkeypatch.setattr(settings, "platform_core_url", "http://127.0.0.1:9")     # nothing listens
    monkeypatch.setattr(settings, "platform_client_secret", "s3cret")
    with FakeZKDevice() as device:
        result = _provider(device, PlatformRelayTcp(ACCOUNT, AGENT)).test_connection()
    assert not result.ok and "OdooBridger's relay" in result.message


def test_building_a_zk_source_in_platform_mode_goes_through_the_agent(db, stream_core, monkeypatch):
    from app.core.crypto import encrypt
    from app.models import DeviceSource, Tenant
    from app.services import connections as conn_mod

    monkeypatch.setattr(settings, "platform_mode", True)
    monkeypatch.setattr(settings, "allow_private_network_targets", False)
    with FakeZKDevice(params={"~SerialNumber": "VIAAGENT"}) as device:
        tenant = Tenant(name="Acme", slug="acme", status="active", platform_account_id=ACCOUNT)
        db.add(tenant)
        db.flush()
        source = DeviceSource(tenant_id=tenant.id, name="uFace", provider="zk_device", connection_kind="device",
                              base_url=f"zk://{device.host}:{device.port}", username="",
                              password_enc=encrypt("", tenant.crypto_key), server_timezone="Asia/Kolkata",
                              config={"relay_agent_id": AGENT})
        db.add(source)
        db.commit()
        assert source.relay_agent_id == AGENT
        info = conn_mod.build_source_provider(tenant, source).test_connection()
        assert info.ok and "VIAAGENT" in info.message
        # Without the agent the same private address is still refused.
        source.config = {}
        with pytest.raises(conn_mod.UnsafeTargetError):
            conn_mod.build_source_provider(tenant, source)

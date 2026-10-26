"""Platform mode: reaching a customer's private Odoo through OdooBridger.

When the customer's Odoo is on a private network, OdooBridger reaches it
through the OdooBridger Agent the customer runs there. BioBridge never talks
to the agent itself: it hands each HTTP request to the core's relay
(``POST {core}/api/v1/relay/forward``, authenticated with this service's
client id and secret), which checks that this service is bound to that Odoo
connection and carries the request to the agent.
"""

from __future__ import annotations

import base64
import json
import socket
from urllib.parse import quote

import httpx

from app.core.config import settings

#: Tests swap this for a client that routes into the core in-process.
client_factory = lambda timeout: httpx.Client(timeout=timeout)  # noqa: E731


class RelayUnavailable(httpx.ConnectError):
    """The relay itself could not carry the request (agent offline, address not
    allowed, core unreachable). Its message says what to do; providers only know
    "unreachable", so ``explain`` digs it back out."""


def explain(exc: BaseException) -> str:
    """The text to show for a provider failure: the relay's own words when the
    relay was the problem, otherwise the provider's."""
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, (RelayUnavailable, RelayTcpError)):
            return str(cur)
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return str(exc)


class PlatformRelayTransport(httpx.BaseTransport):
    def __init__(self, account_id: str, connection_id: str | None = None, timeout: float | None = None,
                 *, agent_id: str | None = None) -> None:
        self.account_id = account_id
        self.connection_id = connection_id
        self.agent_id = agent_id
        self.timeout = float(timeout or settings.http_timeout_seconds)
        #: Distinguishes cached Odoo API detection per route (see OdooClient._detect_api).
        self.cache_key = f"relay:{connection_id or agent_id}"

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        payload = {
            "account_id": self.account_id,
            **({"agent_id": self.agent_id} if self.agent_id else {"connection_id": self.connection_id}),
            "method": request.method, "url": str(request.url),
            "headers": {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
            "body": base64.b64encode(request.read()).decode(), "timeout": self.timeout,
        }
        auth = base64.b64encode(f"{settings.platform_service_slug}:{settings.platform_client_secret}".encode()).decode()
        try:
            with client_factory(self.timeout + 15) as client:
                resp = client.post(f"{settings.platform_core_api}/api/v1/relay/forward", json=payload,
                                   headers={"Authorization": f"Basic {auth}"})
        except httpx.HTTPError as exc:
            raise RelayUnavailable(f"Could not reach OdooBridger's relay: {exc}", request=request) from exc
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text[:200]
            message = detail.get("message") if isinstance(detail, dict) else detail
            raise RelayUnavailable(str(message or f"OdooBridger's relay answered {resp.status_code}."),
                                   request=request)
        data = resp.json()
        headers = {k: v for k, v in (data.get("headers") or {}).items()
                   if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")}
        return httpx.Response(int(data.get("status") or 502), headers=headers,
                              content=base64.b64decode(data.get("body") or ""), request=request)


def allow_on_agent(account_id: str, agent_id: str, address: str) -> str | None:
    """Ask the core to let the agent reach this device; it is sent to the running
    agent at once. Returns why not, or None. Best effort: a failure here shows
    up again, explained, when the device is tested."""
    auth = base64.b64encode(f"{settings.platform_service_slug}:{settings.platform_client_secret}".encode()).decode()
    try:
        with client_factory(15) as client:
            resp = client.post(f"{settings.platform_core_api}/api/v1/relay/agents/{agent_id}/addresses",
                               json={"account_id": account_id, "address": address},
                               headers={"Authorization": f"Basic {auth}"})
    except httpx.HTTPError as exc:
        return f"Could not reach OdooBridger's relay: {exc}"
    if resp.status_code == 200:
        return None
    try:
        return str(resp.json().get("detail") or resp.text[:200])
    except ValueError:
        return resp.text[:200] or f"OdooBridger answered {resp.status_code}."


def list_agents(account_id: str) -> list[dict]:
    """The customer's agents, from the core (empty when the core can't say)."""
    auth = base64.b64encode(f"{settings.platform_service_slug}:{settings.platform_client_secret}".encode()).decode()
    try:
        with client_factory(15) as client:
            resp = client.get(f"{settings.platform_core_api}/api/v1/relay/agents",
                              params={"account_id": account_id}, headers={"Authorization": f"Basic {auth}"})
        if resp.status_code != 200:
            return []
        return list(resp.json().get("agents") or [])
    except (httpx.HTTPError, ValueError):
        return []


# --- raw TCP: a device that speaks its own protocol (ZKTeco, port 4370) -------------
class RelayTcpError(ConnectionError):
    """The relay could not open or keep the device connection. The message says what to do."""


class RelayTcpSocket:
    """Just enough of a blocking socket (sendall, recv, settimeout, close) over
    the core's ``/api/v1/relay/stream`` WebSocket, which the customer's agent
    ends on their own network. The device's bytes travel as binary frames."""

    def __init__(self, account_id: str, agent_id: str, host: str, port: int, timeout: float) -> None:
        from websockets.exceptions import InvalidStatus, WebSocketException
        from websockets.sync.client import connect

        base = settings.platform_core_api
        ws_base = ("wss://" + base[len("https://"):]) if base.startswith("https://") else "ws://" + base.split("://", 1)[1]
        url = (f"{ws_base}/api/v1/relay/stream?account_id={quote(account_id)}&agent_id={quote(agent_id)}"
               f"&host={quote(host)}&port={int(port)}")
        auth = base64.b64encode(f"{settings.platform_service_slug}:{settings.platform_client_secret}".encode()).decode()
        self._timeout = float(timeout)
        self._buffer = b""
        try:
            self._ws = connect(url, additional_headers={"Authorization": f"Basic {auth}"},
                               open_timeout=self._timeout, max_size=2**22, ping_interval=20, ping_timeout=20)
        except InvalidStatus as exc:
            raise RelayTcpError(f"OdooBridger refused the device connection (HTTP {exc.response.status_code}).") from exc
        except (OSError, WebSocketException) as exc:
            raise RelayTcpError(f"Could not reach OdooBridger's relay: {exc}") from exc
        try:
            first = self._ws.recv(timeout=self._timeout + 10)
        except Exception as exc:  # noqa: BLE001 — refused before it opened (bad credentials, wrong account)
            self._ws.close()
            raise RelayTcpError("OdooBridger's relay refused the device connection. Check that this device's "
                                "agent belongs to your account.") from exc
        message = json.loads(first) if isinstance(first, str) else {}
        if message.get("type") != "opened":
            self._ws.close()
            raise RelayTcpError(str(message.get("message") or "The agent could not reach the device."))

    def settimeout(self, value: float | None) -> None:
        self._timeout = float(value) if value else self._timeout

    def sendall(self, data: bytes) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            self._ws.send(data)
        except ConnectionClosed as exc:
            raise ConnectionResetError("The relay connection to the device closed.") from exc

    def recv(self, size: int) -> bytes:
        from websockets.exceptions import ConnectionClosed

        while not self._buffer:
            try:
                message = self._ws.recv(timeout=self._timeout)
            except TimeoutError:
                raise socket.timeout("timed out") from None
            except ConnectionClosed:
                return b""
            if isinstance(message, bytes):
                self._buffer += message
            else:
                info = json.loads(message)
                if info.get("type") == "error":
                    raise RelayTcpError(str(info.get("message") or "The device connection failed."))
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass


class PlatformRelayTcp:
    """Opens device connections through one customer agent: a stand-in for socket.create_connection."""

    def __init__(self, account_id: str, agent_id: str) -> None:
        self.account_id, self.agent_id = account_id, agent_id

    def __call__(self, host: str, port: int, timeout: float) -> RelayTcpSocket:
        return RelayTcpSocket(self.account_id, self.agent_id, host, port, timeout)

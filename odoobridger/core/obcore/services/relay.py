"""The relay hub: reaching customers' private networks through OdooBridger Agents.

An agent runs inside the customer's network and keeps one WebSocket open to
the core (``/relay/v1/agent``). Anything that needs to call a private Odoo —
the core's own connection test, a service's sync — hands the hub an HTTP
request; the hub sends it down that agent's socket, the agent makes the call
on the local network and sends the answer back. Many requests share one
socket, matched by id.

Wire protocol (JSON text frames):

    agent -> hub  {"type": "hello", "version", "hostname", "allowed": ["http://192.168.1.20:8069"]}
    hub -> agent  {"type": "welcome", "name"}
    hub -> agent  {"type": "request", "id", "method", "url", "headers", "body", "timeout"}
    agent -> hub  {"type": "response", "id", "status", "headers", "body"}
                | {"type": "error", "id", "code", "message"}

``body`` is base64. The agent forwards only to URLs whose origin (scheme,
host, port) is in its own ``allowed`` list — the hub cannot point it anywhere
else on the customer's network.

The hub keeps the sockets in this process, so the core must run as a single
process (it does: one uvicorn worker). Scaling out needs a shared router.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from obcore.config import settings

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
#: Not forwarded either way: they describe one hop, not the request.
HOP_HEADERS = {"host", "connection", "keep-alive", "transfer-encoding", "content-length", "upgrade",
               "proxy-connection", "te", "trailer", "proxy-authorization"}


class RelayError(Exception):
    """The request could not be carried. Safe to show the customer."""

    def __init__(self, message: str, code: str = "relay") -> None:
        super().__init__(message)
        self.code = code


def origin(url: str) -> str:
    """scheme://host:port, lower case, with the default port written out."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{(parts.hostname or '').lower()}:{port}"


DEFAULT_RANGE_PORTS = frozenset({80, 443})


# --- address rules (the same reading as the agent's own, agent/odoobridger_agent.py) ---
def parse_rule(text: str):
    """("hostport", host, port) for ``192.168.1.20:8069`` (that host and port, any
    protocol), ("origin", "http://h:p") for a web address, ("net", network, ports)
    for ``192.168.1.0/24@80+443``. Raises ValueError."""
    text = text.strip().rstrip("/")
    if "://" in text:
        if not text.lower().startswith(("http://", "https://")) or not urlsplit(text).hostname:
            raise ValueError("not a web address")
        return ("origin", origin(text))
    if "@" in text or "/" in text:
        net, _, ports = text.partition("@")
        network = ipaddress.ip_network(net.strip(), strict=False)
        wanted = {int(p) for p in ports.split("+") if p.strip()} if ports.strip() else set(DEFAULT_RANGE_PORTS)
        if not wanted or any(not 0 < p < 65536 for p in wanted):
            raise ValueError("bad port list")
        return ("net", network, frozenset(wanted))
    parts = urlsplit("//" + text)
    if not parts.hostname or not parts.port:
        raise ValueError("needs host:port")
    return ("hostport", parts.hostname.lower(), parts.port)


def _url_host_port(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    return scheme, (parts.hostname or "").lower(), parts.port or (443 if scheme == "https" else 80)


def _rule_allows(rule, url: str) -> bool:
    scheme, host, port = _url_host_port(url)
    if scheme not in ("http", "https"):
        return False
    if rule[0] == "origin":
        return origin(url) == rule[1]
    if rule[0] == "hostport":
        return host == rule[1] and port == rule[2]
    try:
        return ipaddress.ip_address(host) in rule[1] and port in rule[2]
    except ValueError:
        return False


def _rule_allows_tcp(rule, host: str, port: int) -> bool:
    host = host.strip().lower()
    if rule[0] == "origin":
        parts = urlsplit(rule[1])
        return (parts.hostname or "") == host and parts.port == port
    if rule[0] == "hostport":
        return host == rule[1] and port == rule[2]
    try:
        return ipaddress.ip_address(host) in rule[1] and port in rule[2]
    except ValueError:
        return False


def _any(rules: list[str] | None, check) -> bool:
    for text in rules or []:
        try:
            if check(parse_rule(text)):
                return True
        except ValueError:
            continue
    return False


def allows(allowed: list[str] | None, url: str) -> bool:
    return _any(allowed, lambda rule: _rule_allows(rule, url))


def allows_tcp(allowed: list[str] | None, host: str, port: int) -> bool:
    return _any(allowed, lambda rule: _rule_allows_tcp(rule, host, port))


def address_of(url: str) -> str:
    """The managed address a web URL needs: ``host:port``."""
    _scheme, host, port = _url_host_port(url)
    return f"{host}:{port}"


def _refusal(link: "Link", where: str, ok_managed: bool, ok_local: bool) -> str | None:
    """Why the agent would refuse, or None. Mirrors the agent's own check (it also
    keeps to private addresses when no local limit is set; only it can resolve names)."""
    if link.managed is None:          # an older agent: its OB_ALLOW alone decides
        if ok_local:
            return None
        return (f"The OdooBridger Agent is not set up to reach {where}. Add it to OB_ALLOW in the agent's "
                "settings and restart it, or update the agent so its addresses are managed here.")
    if not ok_managed:
        return f"{where} is not one of the agent's addresses. Add it on the Odoo page (Agents → Add Address)."
    if link.allowed and not ok_local:
        return (f"{where} is outside the OB_ALLOW limit the agent was started with on its computer "
                f"({', '.join(link.allowed)}). Start the agent again without OB_ALLOW, using the command "
                "under OdooBridger Agents → New Key; its addresses are then managed here.")
    return None


def refusal_for_url(link: "Link", url: str) -> str | None:
    return _refusal(link, address_of(url), allows(link.managed, url), allows(link.allowed, url))


def refusal_for_tcp(link: "Link", host: str, port: int) -> str | None:
    return _refusal(link, f"{host}:{port}", allows_tcp(link.managed, host, port), allows_tcp(link.allowed, host, port))


def clean_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_HEADERS}


@dataclass
class Link:
    """One connected agent."""

    agent_id: str
    account_id: str
    socket: Any                      # starlette WebSocket
    loop: asyncio.AbstractEventLoop
    #: The limit set on the agent's own computer (OB_ALLOW); empty = none.
    allowed: list[str] = field(default_factory=list)
    #: The addresses managed in OdooBridger, kept in step with the agent.
    #: None: an older agent that doesn't take them (OB_ALLOW alone decides).
    managed: list[str] | None = None
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    streams: dict[str, "Stream"] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.socket.send_text(json.dumps(message))


class Stream:
    """One raw TCP connection the agent holds open to a device, for one service."""

    def __init__(self, link: Link, stream_id: str) -> None:
        self.link, self.id = link, stream_id
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def send(self, data: bytes) -> None:
        if self.closed:
            raise RelayError("The device connection is closed.", "closed")
        await self.link.send({"type": "data", "id": self.id, "data": base64.b64encode(data).decode()})

    async def recv(self) -> bytes | None:
        """The next bytes from the device, or None when it is closed."""
        message = await self.queue.get()
        if message.get("type") == "data":
            return base64.b64decode(message.get("data") or "")
        self.closed = True
        if message.get("type") == "stream_error":
            raise RelayError(str(message.get("message") or "The device connection failed."),
                             str(message.get("code") or "agent"))
        return None

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                await self.link.send({"type": "close", "id": self.id})
            except Exception:  # noqa: BLE001 — the socket is already gone
                pass
        self.link.streams.pop(self.id, None)


class Hub:
    def __init__(self) -> None:
        self.links: dict[str, Link] = {}

    # --- agents --------------------------------------------------------------
    def online(self, agent_id: str | None) -> bool:
        return bool(agent_id) and agent_id in self.links

    def allowed(self, agent_id: str) -> list[str] | None:
        link = self.links.get(agent_id)
        return list(link.allowed) if link else None

    def takes_config(self, agent_id: str) -> bool | None:
        """Whether the connected agent takes managed addresses (None: offline)."""
        link = self.links.get(agent_id)
        return None if link is None else link.managed is not None

    def set_managed(self, agent_id: str, addresses: list[str]) -> None:
        """New managed addresses: in force for the next request at once, and sent
        to the agent (before anything sent after this call). From any thread."""
        link = self.links.get(agent_id)
        if link is None or link.managed is None:
            return
        link.managed = list(addresses)
        message = {"type": "config", "allowed": list(addresses)}
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is link.loop:
            running.create_task(_send_quietly(link, message))
        else:
            asyncio.run_coroutine_threadsafe(_send_quietly(link, message), link.loop)

    async def attach(self, link: Link) -> None:
        old = self.links.get(link.agent_id)
        self.links[link.agent_id] = link
        if old is not None and old is not link:
            # The same agent reconnected (or a second copy runs): the newest wins.
            old.loop.call_soon_threadsafe(asyncio.ensure_future, _close_quietly(old.socket))
            _fail_all(old, "The agent reconnected; try again.")

    def detach(self, link: Link) -> None:
        if self.links.get(link.agent_id) is link:
            del self.links[link.agent_id]
        _fail_all(link, "The OdooBridger Agent disconnected.")

    def deliver(self, link: Link, message: dict[str, Any]) -> None:
        """An answer from the agent: complete the waiting request."""
        future = link.pending.pop(str(message.get("id") or ""), None)
        if future is None or future.done():
            return
        if message.get("type") == "response":
            future.set_result(message)
        else:
            future.set_exception(RelayError(
                str(message.get("message") or "The agent could not make the request."),
                str(message.get("code") or "agent")))

    # --- raw TCP streams (a device's own protocol) -----------------------------
    def stream_count(self, account_id: str) -> int:
        return sum(len(link.streams) for link in self.links.values() if link.account_id == account_id)

    def deliver_stream(self, link: Link, message: dict[str, Any]) -> None:
        stream = link.streams.get(str(message.get("id") or ""))
        if stream is not None:
            stream.queue.put_nowait(message)

    async def open_stream(self, agent_id: str, host: str, port: int, timeout: float = 15) -> Stream:
        link = self.links.get(agent_id)
        if link is None:
            raise RelayError("The OdooBridger Agent on your network is offline. Check that it is running "
                             "and can reach the internet.", "offline")
        refusal = refusal_for_tcp(link, host, port)
        if refusal:
            raise RelayError(refusal, "not_allowed")
        stream = Stream(link, uuid.uuid4().hex)
        link.streams[stream.id] = stream
        try:
            await link.send({"type": "open", "id": stream.id, "host": host, "port": port, "timeout": timeout})
            first = await asyncio.wait_for(stream.queue.get(), timeout + 5)
        except asyncio.TimeoutError as exc:
            link.streams.pop(stream.id, None)
            raise RelayError(f"{host}:{port} did not answer through the agent in time.", "timeout") from exc
        except Exception as exc:  # noqa: BLE001 — a broken socket mid-send
            link.streams.pop(stream.id, None)
            raise RelayError(f"The agent connection broke: {exc}", "broken") from exc
        if first.get("type") != "opened":
            link.streams.pop(stream.id, None)
            raise RelayError(str(first.get("message") or "The agent could not reach the device."),
                             str(first.get("code") or "agent"))
        return stream

    # --- requests ------------------------------------------------------------
    async def forward(self, agent_id: str, method: str, url: str, headers: Mapping[str, str] | None = None,
                      body: bytes = b"", timeout: float | None = None) -> tuple[int, dict[str, str], bytes]:
        link = self.links.get(agent_id)
        if link is None:
            raise RelayError("The OdooBridger Agent on your network is offline. Check that it is running "
                             "and can reach the internet.", "offline")
        refusal = refusal_for_url(link, url)
        if refusal:
            raise RelayError(refusal, "not_allowed")
        if len(body) > settings.relay_max_body_bytes:
            raise RelayError("The request is too large to send through the agent.", "too_large")
        timeout = timeout or settings.relay_timeout_seconds
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        link.pending[request_id] = future
        try:
            await link.send({"type": "request", "id": request_id, "method": method.upper(), "url": url,
                             "headers": clean_headers(headers or {}),
                             "body": base64.b64encode(body).decode(), "timeout": timeout})
            answer = await asyncio.wait_for(future, timeout + 5)
        except asyncio.TimeoutError as exc:
            raise RelayError(f"{origin(url)} did not answer through the agent in time.", "timeout") from exc
        except RelayError:
            raise
        except Exception as exc:  # noqa: BLE001 — a broken socket mid-send
            raise RelayError(f"The agent connection broke: {exc}", "broken") from exc
        finally:
            link.pending.pop(request_id, None)
        content = base64.b64decode(answer.get("body") or "")
        return int(answer.get("status") or 502), dict(answer.get("headers") or {}), content

    def forward_sync(self, agent_id: str, method: str, url: str, headers: Mapping[str, str] | None = None,
                     body: bytes = b"", timeout: float | None = None) -> tuple[int, dict[str, str], bytes]:
        """``forward`` from a worker thread (sync endpoints, background jobs)."""
        link = self.links.get(agent_id)
        if link is None:
            raise RelayError("The OdooBridger Agent on your network is offline. Check that it is running "
                             "and can reach the internet.", "offline")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is link.loop:
            raise RuntimeError("forward_sync called on the hub's own event loop; await forward() instead")
        future = asyncio.run_coroutine_threadsafe(
            self.forward(agent_id, method, url, headers, body, timeout), link.loop)
        return future.result((timeout or settings.relay_timeout_seconds) + 10)


async def _send_quietly(link: Link, message: dict[str, Any]) -> None:
    try:
        await link.send(message)
    except Exception:  # noqa: BLE001 — the agent went away; it gets the list on reconnect
        pass


async def _close_quietly(socket: Any) -> None:
    try:
        await socket.close(code=4000)
    except Exception:  # noqa: BLE001
        pass


def _fail_all(link: Link, message: str) -> None:
    for stream in list(link.streams.values()):
        link.loop.call_soon_threadsafe(stream.queue.put_nowait,
                                       {"type": "stream_error", "code": "broken", "message": message})
    link.streams.clear()
    for future in list(link.pending.values()):
        if not future.done():
            link.loop.call_soon_threadsafe(_set_error, future, message)
    link.pending.clear()


def _set_error(future: asyncio.Future, message: str) -> None:
    if not future.done():
        future.set_exception(RelayError(message, "broken"))


hub = Hub()


class RelayTransport(httpx.BaseTransport):
    """An httpx transport that sends every request through one agent."""

    def __init__(self, agent_id: str, timeout: float | None = None) -> None:
        self.agent_id = agent_id
        self.timeout = timeout

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            status, headers, content = hub.forward_sync(
                self.agent_id, request.method, str(request.url), dict(request.headers), request.read(), self.timeout)
        except RelayError as exc:
            raise httpx.ConnectError(str(exc), request=request) from exc
        # The agent hands over the body already decoded.
        headers = {k: v for k, v in clean_headers(headers).items() if k.lower() != "content-encoding"}
        return httpx.Response(status, headers=headers, content=content, request=request)

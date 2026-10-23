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


def allows(allowed: list[str] | None, url: str) -> bool:
    return origin(url) in {origin(a) for a in (allowed or [])}


def clean_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_HEADERS}


@dataclass
class Link:
    """One connected agent."""

    agent_id: str
    account_id: str
    socket: Any                      # starlette WebSocket
    loop: asyncio.AbstractEventLoop
    allowed: list[str] = field(default_factory=list)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.socket.send_text(json.dumps(message))


class Hub:
    def __init__(self) -> None:
        self.links: dict[str, Link] = {}

    # --- agents --------------------------------------------------------------
    def online(self, agent_id: str | None) -> bool:
        return bool(agent_id) and agent_id in self.links

    def allowed(self, agent_id: str) -> list[str] | None:
        link = self.links.get(agent_id)
        return list(link.allowed) if link else None

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

    # --- requests ------------------------------------------------------------
    async def forward(self, agent_id: str, method: str, url: str, headers: Mapping[str, str] | None = None,
                      body: bytes = b"", timeout: float | None = None) -> tuple[int, dict[str, str], bytes]:
        link = self.links.get(agent_id)
        if link is None:
            raise RelayError("The OdooBridger Agent on your network is offline. Check that it is running "
                             "and can reach the internet.", "offline")
        if not allows(link.allowed, url):
            raise RelayError(f"The OdooBridger Agent is not set up to reach {origin(url)}. Add it to "
                             "OB_ALLOW in the agent's settings and restart it.", "not_allowed")
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


async def _close_quietly(socket: Any) -> None:
    try:
        await socket.close(code=4000)
    except Exception:  # noqa: BLE001
        pass


def _fail_all(link: Link, message: str) -> None:
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

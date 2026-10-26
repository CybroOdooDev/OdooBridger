"""OdooBridger Agents: the socket agents dial into, the portal's agent list,
and the endpoint services use to reach a private Odoo through an agent."""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.api.deps import Customer, get_customer, require_manager
from obcore.api.out import iso
from obcore.config import settings
from obcore.db import SessionLocal, get_db
from obcore.models import AccountService, OdooConnection, RelayAgent
from obcore.security import hash_token, new_token, utcnow
from obcore.services import accounts as acc
from obcore.services.relay import PROTOCOL_VERSION, Link, RelayError, hub

log = logging.getLogger(__name__)
router = APIRouter(tags=["relay"])

KEY_PREFIX = "obag_"


# =============================================================================
# The agent's socket
# =============================================================================
def _agent_for_key(raw: str) -> RelayAgent | None:
    if not raw or not raw.startswith(KEY_PREFIX):
        return None
    db = SessionLocal()
    try:
        agent = db.scalar(select(RelayAgent).where(RelayAgent.key_hash == hash_token(raw)))
        if agent is None or agent.revoked_at is not None:
            return None
        db.expunge(agent)
        return agent
    finally:
        db.close()


def _record(agent_id: str, **values) -> None:
    db = SessionLocal()
    try:
        agent = db.get(RelayAgent, agent_id)
        if agent is not None:
            for key, value in values.items():
                setattr(agent, key, value)
            db.commit()
    finally:
        db.close()


@router.websocket("/relay/v1/agent")
async def agent_socket(socket: WebSocket) -> None:
    header = socket.headers.get("authorization") or ""
    raw = header.split(None, 1)[1].strip() if header.lower().startswith("bearer ") else ""
    agent = await asyncio.to_thread(_agent_for_key, raw)
    if agent is None:
        # Close before accepting: the agent sees a refused handshake (HTTP 403).
        await socket.close(code=4401)
        return
    await socket.accept()
    try:
        hello = json.loads(await asyncio.wait_for(socket.receive_text(), 15))
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
        await socket.close(code=4400)
        return
    if hello.get("type") != "hello":
        await socket.close(code=4400)
        return
    # "allowed": the limit set on the agent's computer (OB_ALLOW), usually empty.
    # An agent that takes "config" gets its addresses from here, live.
    allowed = [str(a) for a in (hello.get("allowed") or [])][:20]
    takes_config = "config" in (hello.get("features") or [])
    managed = list(agent.managed_allowed or []) if takes_config else None
    link = Link(agent_id=agent.id, account_id=agent.account_id, socket=socket,
                loop=asyncio.get_running_loop(), allowed=allowed, managed=managed)
    await hub.attach(link)
    ip = socket.client.host if socket.client else None
    now = utcnow()
    await asyncio.to_thread(_record, agent.id, version=str(hello.get("version") or "")[:20] or None,
                            hostname=str(hello.get("hostname") or "")[:120] or None, allowed=allowed,
                            last_ip=ip, connected_at=now, last_seen_at=now)
    log.info("Agent %s (%s) connected from %s, allowed %s", agent.id, agent.name, ip, allowed)
    welcome = {"type": "welcome", "name": agent.name, "protocol": PROTOCOL_VERSION}
    if managed is not None:
        welcome["allowed"] = managed
    await link.send(welcome)
    # Whatever was waiting on this agent (an Odoo connection, the services
    # using it) is checked again now, so nothing needs saving again.
    asyncio.get_running_loop().create_task(_recheck(agent.id))
    try:
        while True:
            message = json.loads(await socket.receive_text())
            if message.get("type") in ("response", "error"):
                hub.deliver(link, message)
            elif message.get("type") in ("opened", "data", "close", "stream_error"):
                hub.deliver_stream(link, message)
            elif message.get("type") == "ping":
                await link.send({"type": "pong"})
    except (WebSocketDisconnect, ValueError, RuntimeError):
        pass
    finally:
        hub.detach(link)
        await asyncio.to_thread(_record, agent.id, last_seen_at=utcnow())
        log.info("Agent %s disconnected", agent.id)


async def _recheck(agent_id: str) -> None:
    from obcore.services.odoo_connections import recheck_through_agent

    await asyncio.sleep(0.2)
    if hub.online(agent_id):
        await asyncio.to_thread(recheck_through_agent, agent_id)


# =============================================================================
# Services reaching a private Odoo through the account's agent
# =============================================================================
class ForwardIn(BaseModel):
    account_id: str
    #: Either the Odoo connection the service is bound to, or one of the
    #: account's agents (a service reaching a device on the customer's network).
    connection_id: str | None = None
    agent_id: str | None = None
    method: str = Field(max_length=10)
    url: str = Field(max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""          # base64
    timeout: float | None = None


def _service_from_basic(request: Request, db: Session):
    from obcore.services.sso import service_by_slug

    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("basic "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Service credentials required.")
    try:
        slug, secret = base64.b64decode(header.split(None, 1)[1]).decode().split(":", 1)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad service credentials.") from exc
    service = service_by_slug(db, slug)
    if service is None or not service.client_secret_hash or hash_token(secret) != service.client_secret_hash:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown service or wrong secret.")
    return service


def _route(db: Session, request: Request, payload: ForwardIn) -> str:
    """The agent a service may use for this request, after every check."""
    service = _service_from_basic(request, db)
    row = db.scalar(select(AccountService).where(AccountService.account_id == payload.account_id,
                                                 AccountService.service_id == service.id))
    if row is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This service is not set up on that account.")
    if bool(payload.connection_id) == bool(payload.agent_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Give either connection_id or agent_id.")
    if payload.agent_id:
        agent = db.get(RelayAgent, payload.agent_id)
        if agent is None or agent.account_id != payload.account_id or agent.revoked_at is not None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "That agent does not belong to this account.")
        return agent.id
    if row.odoo_connection_id != payload.connection_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This service is not bound to that Odoo connection.")
    conn = db.get(OdooConnection, payload.connection_id)
    if conn is None or not conn.relay_agent_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "That Odoo connection does not use an agent.")
    return conn.relay_agent_id


# A device connection (raw TCP) is held for one short sync, then released.
MAX_ACCOUNT_STREAMS = 4
STREAM_IDLE_SECONDS = 120
STREAM_MAX_SECONDS = 900


@router.websocket("/api/v1/relay/stream")
async def service_stream(socket: WebSocket, account_id: str, agent_id: str, host: str, port: int) -> None:
    """A service's raw TCP connection to a device on the customer's network, through their agent.

    Binary frames carry the device's bytes both ways. Same authorisation as
    ``/api/v1/relay/forward``: the service's credentials, the service on the
    account, the agent the account's own.
    """
    payload = ForwardIn(account_id=account_id, agent_id=agent_id, method="TCP", url=f"tcp://{host}:{port}")

    def authorize() -> str:
        db = SessionLocal()
        try:
            return _route(db, socket, payload)
        finally:
            db.close()

    try:
        await asyncio.to_thread(authorize)
    except HTTPException as exc:
        await socket.close(code=4403, reason=str(exc.detail)[:100])
        return
    await socket.accept()
    if hub.stream_count(account_id) >= MAX_ACCOUNT_STREAMS:
        await socket.send_json({"type": "error", "code": "busy",
                                "message": "Too many device connections are open for this account. Try again shortly."})
        await socket.close()
        return
    try:
        stream = await hub.open_stream(agent_id, host, int(port))
    except RelayError as exc:
        await socket.send_json({"type": "error", "code": exc.code, "message": str(exc)})
        await socket.close()
        return
    await socket.send_json({"type": "opened"})

    async def upstream() -> None:      # service -> device
        while True:
            message = await asyncio.wait_for(socket.receive(), STREAM_IDLE_SECONDS)
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes"):
                await stream.send(message["bytes"])

    async def downstream() -> None:    # device -> service
        while True:
            data = await stream.recv()
            if data is None:
                return
            await socket.send_bytes(data)

    tasks = [asyncio.ensure_future(upstream()), asyncio.ensure_future(downstream())]

    async def release() -> None:
        # Its own task: if this handler is being cancelled (the service dropped),
        # the clean-up must still run to the end, and tell the agent to let go.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await stream.close()
        try:
            await socket.close()
        except RuntimeError:
            pass

    try:
        await asyncio.wait(tasks, timeout=STREAM_MAX_SECONDS, return_when=asyncio.FIRST_COMPLETED)
    finally:
        asyncio.ensure_future(release())


class ServiceAddressIn(BaseModel):
    account_id: str
    address: str = Field(min_length=1, max_length=200)


@router.post("/api/v1/relay/agents/{agent_id}/addresses")
async def service_add_address(agent_id: str, payload: ServiceAddressIn, request: Request) -> dict:
    """A service connecting a device through the account's agent: let the agent reach it.

    Same checks as forwarding (the service on the account, the agent the
    account's own). The address is sent to the running agent at once."""
    from obcore.services import agent_addresses

    def add() -> dict:
        db = SessionLocal()
        try:
            check = ForwardIn(account_id=payload.account_id, agent_id=agent_id, method="GET", url="http://x/")
            _route(db, request, check)
            agent = db.get(RelayAgent, agent_id)
            try:
                entry = agent_addresses.add(db, agent, payload.address)
            except agent_addresses.AddressError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
            db.commit()
            return {"address": entry, "allowed": list(agent.managed_allowed or [])}
        finally:
            db.close()

    return await asyncio.to_thread(add)


@router.get("/api/v1/relay/agents")
async def service_agents(account_id: str, request: Request) -> dict:
    """The account's agents, for a service that lets its customer pick one."""
    def load() -> list[dict]:
        db = SessionLocal()
        try:
            service = _service_from_basic(request, db)
            row = db.scalar(select(AccountService).where(AccountService.account_id == account_id,
                                                         AccountService.service_id == service.id))
            if row is None:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "This service is not set up on that account.")
            agents = db.scalars(select(RelayAgent).where(RelayAgent.account_id == account_id,
                                                         RelayAgent.revoked_at.is_(None))).all()
            return [{"id": a.id, "name": a.name, "online": hub.online(a.id),
                     "allowed": list(a.managed_allowed or [])} for a in agents]
        finally:
            db.close()

    return {"agents": await asyncio.to_thread(load)}


@router.post("/api/v1/relay/forward")
async def forward(payload: ForwardIn, request: Request) -> dict:
    def route() -> str:
        db = SessionLocal()
        try:
            return _route(db, request, payload)
        finally:
            db.close()

    agent_id = await asyncio.to_thread(route)
    try:
        body = base64.b64decode(payload.body or "")
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The body is not base64.") from exc
    try:
        code, headers, content = await hub.forward(agent_id, payload.method, payload.url, payload.headers,
                                                   body, payload.timeout)
    except RelayError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"code": exc.code, "message": str(exc)}) from exc
    return {"status": code, "headers": headers, "body": base64.b64encode(content).decode()}


# =============================================================================
# The portal: an account's agents
# =============================================================================
def agent_out(agent: RelayAgent) -> dict:
    used_by = [c.name for c in agent_connections(agent)]
    live_limit = hub.allowed(agent.id)
    takes = hub.takes_config(agent.id)
    return {
        "id": agent.id, "name": agent.name, "key_hint": agent.key_hint, "online": hub.online(agent.id),
        "version": agent.version, "hostname": agent.hostname,
        # What it may reach, managed here; and any limit set on its computer.
        "allowed": list(agent.managed_allowed or []),
        "limited_to": live_limit if live_limit is not None else (agent.allowed or []),
        # An older agent that ignores the addresses here (needs updating).
        "outdated": takes is False,
        "last_ip": agent.last_ip, "connected_at": iso(agent.connected_at), "last_seen_at": iso(agent.last_seen_at),
        "created_at": iso(agent.created_at), "used_by": used_by,
    }


def agent_connections(agent: RelayAgent) -> list[OdooConnection]:
    return [c for c in agent.account.odoo_connections if c.relay_agent_id == agent.id]


def _agents(customer: Customer) -> list[RelayAgent]:
    return [a for a in customer.account.relay_agents if a.revoked_at is None]


def relay_url() -> str:
    base = settings.base.rstrip("/")
    return ("wss://" + base[len("https://"):] if base.startswith("https://")
            else "ws://" + base[len("http://"):]) + "/relay/v1/agent"


@router.get("/api/v1/portal/agents")
def list_agents(customer: Customer = Depends(get_customer)) -> dict:
    return {"agents": [agent_out(a) for a in _agents(customer)], "relay_url": relay_url()}


class AgentIn(BaseModel):
    name: str = Field(default="Office agent", min_length=1, max_length=120)


@router.post("/api/v1/portal/agents", status_code=status.HTTP_201_CREATED)
def create_agent(payload: AgentIn, request: Request, customer: Customer = Depends(require_manager),
                 db: Session = Depends(get_db)) -> dict:
    if len(_agents(customer)) >= 5:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An account can have up to 5 agents.")
    raw = KEY_PREFIX + new_token(32)
    agent = RelayAgent(account_id=customer.account.id, name=payload.name.strip(), key_hash=hash_token(raw),
                       key_hint=raw[-4:])
    customer.account.relay_agents.append(agent)
    db.add(agent)
    acc.audit(db, customer.actor, "agent.create", account_id=customer.account.id, detail=agent.name, request=request)
    db.commit()
    return {"agent": agent_out(agent), "key": raw, "relay_url": relay_url()}


@router.post("/api/v1/portal/agents/{agent_id}/key")
def rotate_agent_key(agent_id: str, request: Request, customer: Customer = Depends(require_manager),
                     db: Session = Depends(get_db)) -> dict:
    agent = next((a for a in _agents(customer) if a.id == agent_id), None)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found.")
    raw = KEY_PREFIX + new_token(32)
    agent.key_hash, agent.key_hint = hash_token(raw), raw[-4:]
    acc.audit(db, customer.actor, "agent.new_key", account_id=customer.account.id, detail=agent.name, request=request)
    db.commit()
    _disconnect(agent.id)
    return {"agent": agent_out(agent), "key": raw, "relay_url": relay_url()}


@router.delete("/api/v1/portal/agents/{agent_id}")
def delete_agent(agent_id: str, request: Request, customer: Customer = Depends(require_manager),
                 db: Session = Depends(get_db)) -> dict:
    agent = next((a for a in _agents(customer) if a.id == agent_id), None)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found.")
    in_use = [c.name for c in customer.account.odoo_connections if c.relay_agent_id == agent.id]
    if in_use:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{', '.join(in_use)} reaches Odoo through this agent. Change that connection first.")
    agent.revoked_at = utcnow()
    acc.audit(db, customer.actor, "agent.remove", account_id=customer.account.id, detail=agent.name, request=request)
    db.commit()
    _disconnect(agent.id)
    return {"agents": [agent_out(a) for a in _agents(customer)]}


class AddressIn(BaseModel):
    address: str = Field(min_length=1, max_length=200)


@router.post("/api/v1/portal/agents/{agent_id}/addresses")
def add_agent_address(agent_id: str, payload: AddressIn, request: Request,
                      customer: Customer = Depends(require_manager), db: Session = Depends(get_db)) -> dict:
    from obcore.services import agent_addresses

    agent = next((a for a in _agents(customer) if a.id == agent_id), None)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found.")
    try:
        entry = agent_addresses.add(db, agent, payload.address)
    except agent_addresses.AddressError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    acc.audit(db, customer.actor, "agent.address_add", account_id=customer.account.id,
              detail=f"{agent.name}: {entry}", request=request)
    db.commit()
    return {"agent": agent_out(agent)}


@router.delete("/api/v1/portal/agents/{agent_id}/addresses")
def remove_agent_address(agent_id: str, address: str, request: Request,
                         customer: Customer = Depends(require_manager), db: Session = Depends(get_db)) -> dict:
    from obcore.services import agent_addresses

    agent = next((a for a in _agents(customer) if a.id == agent_id), None)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found.")
    try:
        agent_addresses.remove(db, agent, address)
    except agent_addresses.AddressError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    acc.audit(db, customer.actor, "agent.address_remove", account_id=customer.account.id,
              detail=f"{agent.name}: {address}", request=request)
    db.commit()
    return {"agent": agent_out(agent)}


def _disconnect(agent_id: str) -> None:
    link = hub.links.get(agent_id)
    if link is not None:
        from obcore.services.relay import _close_quietly

        hub.detach(link)
        asyncio.run_coroutine_threadsafe(_close_quietly(link.socket), link.loop)

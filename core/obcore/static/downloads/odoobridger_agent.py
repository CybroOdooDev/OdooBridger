#!/usr/bin/env python3
"""OdooBridger Agent: lets OdooBridger reach an Odoo on your private network.

Run it on any machine in the same network as your Odoo (the Odoo server itself
is fine). It opens one outgoing, encrypted connection to OdooBridger and keeps
it open. OdooBridger sends Odoo requests down that connection; the agent makes
them on your network and sends the answers back. Nothing needs to be opened
inbound: no port forwarding, no VPN.

It only ever talks to the addresses you list in OB_ALLOW. A request for any
other address is refused here, on your side, whatever OdooBridger asks.

Settings (environment variables):

    OB_AGENT_KEY    the key shown once in OdooBridger (Odoo page → Agents)
    OB_RELAY_URL    wss://<your OdooBridger address>/relay/v1/agent
    OB_ALLOW        the Odoo address(es) it may reach, comma separated,
                    e.g. http://192.168.1.20:8069
    OB_TLS_VERIFY   "false" to accept a self-signed certificate on your Odoo
                    (default "true")
    OB_CONCURRENCY  requests handled at once (default 8)
    OB_LOG_LEVEL    INFO (default) or DEBUG

    python odoobridger_agent.py          run
    python odoobridger_agent.py --check  check it can reach each OB_ALLOW address, then exit

Needs Python 3.10+ with ``websockets`` (13 or newer) and ``httpx``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import random
import signal
import socket
import sys
from urllib.parse import urlsplit

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

VERSION = "1.0.0"
MAX_BODY = 10_000_000
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
#: Describe one hop, not the request: never passed on.
HOP_HEADERS = {"host", "connection", "keep-alive", "transfer-encoding", "content-length", "upgrade",
               "proxy-connection", "te", "trailer", "proxy-authorization", "content-encoding"}

log = logging.getLogger("odoobridger-agent")


def origin(url: str) -> str:
    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{(parts.hostname or '').lower()}:{port}"


class Config:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self.key = env.get("OB_AGENT_KEY", "").strip()
        self.relay_url = env.get("OB_RELAY_URL", "").strip()
        self.allowed = [a.strip().rstrip("/") for a in env.get("OB_ALLOW", "").split(",") if a.strip()]
        self.tls_verify = env.get("OB_TLS_VERIFY", "true").strip().lower() not in ("0", "false", "no")
        self.concurrency = max(1, int(env.get("OB_CONCURRENCY", "8") or 8))

    def problems(self) -> list[str]:
        out = []
        if not self.key.startswith("obag_"):
            out.append("OB_AGENT_KEY is missing or not an agent key (it starts with obag_).")
        if not self.relay_url.startswith(("wss://", "ws://")):
            out.append("OB_RELAY_URL must start with wss:// (or ws:// for a local test).")
        if not self.allowed:
            out.append("OB_ALLOW is empty: list your Odoo address, e.g. http://192.168.1.20:8069.")
        for a in self.allowed:
            if not a.startswith(("http://", "https://")) or not urlsplit(a).hostname:
                out.append(f"OB_ALLOW entry {a!r} must be an address like http://192.168.1.20:8069.")
        return out

    def allows(self, url: str) -> bool:
        return origin(url) in {origin(a) for a in self.allowed}


class Agent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.send_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(config.concurrency)

    # --- one request ------------------------------------------------------------
    async def handle(self, ws, client: httpx.AsyncClient, message: dict) -> None:
        request_id = message.get("id")
        async with self.slots:
            answer = await self.perform(client, message)
        answer["id"] = request_id
        try:
            async with self.send_lock:
                await ws.send(json.dumps(answer))
        except ConnectionClosed:
            pass

    async def perform(self, client: httpx.AsyncClient, message: dict) -> dict:
        url = str(message.get("url") or "")
        method = str(message.get("method") or "GET").upper()
        if not self.config.allows(url):
            log.warning("Refused %s %s: not in OB_ALLOW", method, origin(url))
            return {"type": "error", "code": "not_allowed",
                    "message": f"The agent is not set up to reach {origin(url)} (add it to OB_ALLOW)."}
        if method not in METHODS:
            return {"type": "error", "code": "bad_request", "message": f"Method {method} is not carried."}
        try:
            body = base64.b64decode(message.get("body") or "")
        except ValueError:
            return {"type": "error", "code": "bad_request", "message": "The request body is not base64."}
        headers = {k: v for k, v in (message.get("headers") or {}).items() if k.lower() not in HOP_HEADERS}
        timeout = min(float(message.get("timeout") or 60), 300)
        try:
            response = await client.request(method, url, headers=headers, content=body, timeout=timeout)
        except httpx.TimeoutException:
            return {"type": "error", "code": "timeout", "message": f"{origin(url)} did not answer in time."}
        except httpx.HTTPError as exc:
            return {"type": "error", "code": "unreachable",
                    "message": f"Could not reach {origin(url)} from the agent's network: {exc}"}
        content = response.content
        if len(content) > MAX_BODY:
            return {"type": "error", "code": "too_large", "message": "Odoo's answer is too large to send back."}
        log.debug("%s %s -> %s (%d bytes)", method, url, response.status_code, len(content))
        return {"type": "response", "status": response.status_code,
                "headers": {k: v for k, v in response.headers.items() if k.lower() not in HOP_HEADERS},
                "body": base64.b64encode(content).decode()}

    # --- the connection ---------------------------------------------------------
    async def session(self) -> None:
        headers = {"Authorization": f"Bearer {self.config.key}", "User-Agent": f"OdooBridger-Agent/{VERSION}"}
        async with websockets.connect(self.config.relay_url, additional_headers=headers,
                                      max_size=2 * MAX_BODY, ping_interval=20, ping_timeout=20,
                                      open_timeout=20) as ws:
            await ws.send(json.dumps({"type": "hello", "version": VERSION, "hostname": socket.gethostname(),
                                      "platform": platform.platform(terse=True), "allowed": self.config.allowed}))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if welcome.get("type") != "welcome":
                raise ConnectionError(f"Unexpected first message: {welcome!r}")
            log.info("Connected to OdooBridger as %r. Reaching: %s", welcome.get("name"), ", ".join(self.config.allowed))
            tasks: set[asyncio.Task] = set()
            async with httpx.AsyncClient(verify=self.config.tls_verify, follow_redirects=False) as client:
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get("type") == "request":
                        task = asyncio.create_task(self.handle(ws, client, message))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                await self.session()
                delay = 1.0
                log.warning("Connection closed by OdooBridger; reconnecting")
            except InvalidStatus as exc:
                code = exc.response.status_code
                if code in (401, 403):
                    log.error("OdooBridger refused the agent key (HTTP %s). Create a new key on the Odoo page "
                              "and set OB_AGENT_KEY. Retrying in 5 minutes.", code)
                    await asyncio.sleep(300)
                    continue
                log.warning("OdooBridger answered HTTP %s; retrying in %.0fs", code, delay)
            except ConnectionClosed as exc:
                if exc.rcvd and exc.rcvd.code == 4401:
                    log.error("OdooBridger refused the agent key. Retrying in 5 minutes.")
                    await asyncio.sleep(300)
                    continue
                log.warning("Connection lost (%s); reconnecting in %.0fs", exc, delay)
            except (OSError, asyncio.TimeoutError, ConnectionError) as exc:
                log.warning("Can't reach OdooBridger (%s); retrying in %.0fs", exc, delay)
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 60)


async def check(config: Config) -> bool:
    """Can each allowed address be reached from here, and is it Odoo?"""
    ok = True
    async with httpx.AsyncClient(verify=config.tls_verify, follow_redirects=False, timeout=10) as client:
        for address in config.allowed:
            try:
                response = await client.get(f"{address}/web/version")
                data = response.json() if response.status_code == 200 else {}
                version = data.get("version") if isinstance(data, dict) else None
                print(f"  {address}: {'Odoo ' + version if version else f'answers (HTTP {response.status_code})'}")
            except (httpx.HTTPError, ValueError) as exc:
                ok = False
                print(f"  {address}: NOT reachable ({exc})")
    return ok


def main(argv: list[str]) -> int:
    logging.basicConfig(level=os.environ.get("OB_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    if os.environ.get("OB_LOG_LEVEL", "INFO").upper() != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per Odoo call is noise
    config = Config()
    problems = config.problems()
    if "--check" in argv:
        if not config.allowed:
            print("\n".join(problems))
            return 2
        print("Checking the addresses in OB_ALLOW:")
        return 0 if asyncio.run(check(config)) else 1
    if problems:
        for p in problems:
            log.error(p)
        return 2
    log.info("OdooBridger Agent %s starting", VERSION)
    if not config.tls_verify:
        log.warning("OB_TLS_VERIFY=false: certificates on your Odoo are not checked")
    loop = asyncio.new_event_loop()
    task = loop.create_task(Agent(config).run())
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except (NotImplementedError, RuntimeError):   # Windows
            pass
    try:
        loop.run_until_complete(task)
    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("Stopped")
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

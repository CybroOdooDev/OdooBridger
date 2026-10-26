#!/usr/bin/env python3
"""OdooBridger Agent: lets OdooBridger reach an Odoo on your private network.

Run it on any machine in the same network as your Odoo (the Odoo server itself
is fine). It opens one outgoing, encrypted connection to OdooBridger and keeps
it open. OdooBridger sends Odoo requests down that connection; the agent makes
them on your network and sends the answers back. Nothing needs to be opened
inbound: no port forwarding, no VPN.

What it may reach is managed in OdooBridger: connecting your Odoo or a
biometric device there adds its address, and the change reaches the running
agent at once (no restart). Two limits hold here, on your side, whatever
OdooBridger asks:

  * only addresses inside your own network (private ranges such as 10.x,
    172.16-31.x, 192.168.x, and this computer itself), and
  * if you set OB_ALLOW, only what it lists as well.

Settings (environment variables):

    OB_AGENT_KEY    the key shown once in OdooBridger (Odoo page → Agents)
    OB_RELAY_URL    wss://<your OdooBridger address>/relay/v1/agent
    OB_ALLOW        optional, a stricter limit set on this computer. Comma
                    separated: one address and port (192.168.1.20:8069),
                    a web address (http://192.168.1.20:8069), or a network
                    range with ports (192.168.1.0/24@80+443+4370; no @ means
                    ports 80 and 443).
    OB_TLS_VERIFY   "false" to accept a self-signed certificate on your Odoo
                    (default "true")
    OB_CONCURRENCY  requests handled at once (default 8)
    OB_LOG_LEVEL    INFO (default) or DEBUG

    python odoobridger_agent.py          run
    python odoobridger_agent.py --check  check it can reach each web address in OB_ALLOW, then exit

Needs Python 3.10+ with ``websockets`` (13 or newer) and ``httpx``.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
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

VERSION = "1.1.0"
MAX_BODY = 10_000_000
MAX_STREAMS = 16
STREAM_CHUNK = 65536
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
#: Describe one hop, not the request: never passed on.
HOP_HEADERS = {"host", "connection", "keep-alive", "transfer-encoding", "content-length", "upgrade",
               "proxy-connection", "te", "trailer", "proxy-authorization", "content-encoding"}

log = logging.getLogger("odoobridger-agent")


DEFAULT_RANGE_PORTS = frozenset({80, 443})
#: Carrier-grade NAT: private in practice, though Python doesn't call it that.
SHARED_NET = ipaddress.ip_network("100.64.0.0/10")


def parse_rule(text: str):
    """One address rule, as OdooBridger sends them or as OB_ALLOW lists them:

    ("hostport", host, port)    192.168.1.20:8069       that host and port, any protocol
    ("origin", "http://h:p")    http://192.168.1.20:8069 that web address
    ("net", network, ports)     192.168.1.0/24@80+443   addresses in the range, on those ports
    """
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


def rule_allows(rule, url: str) -> bool:
    scheme, host, port = _url_host_port(url)
    if scheme not in ("http", "https"):
        return False
    if rule[0] == "origin":
        return origin(url) == rule[1]
    if rule[0] == "hostport":
        return host == rule[1] and port == rule[2]
    try:
        ip = ipaddress.ip_address(host)   # an address, never a name: nothing to re-resolve
    except ValueError:
        return False
    return ip in rule[1] and port in rule[2]


def rule_allows_tcp(rule, host: str, port: int) -> bool:
    """May a raw TCP connection to host:port be opened?"""
    host = host.lower()
    if rule[0] == "origin":
        parts = urlsplit(rule[1])
        return (parts.hostname or "") == host and parts.port == port
    if rule[0] == "hostport":
        return host == rule[1] and port == rule[2]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip in rule[1] and port in rule[2]


def any_rule(rules: list[str], *, url: str | None = None, host: str = "", port: int = 0) -> bool:
    for text in rules:
        try:
            rule = parse_rule(text)
        except ValueError:
            continue
        if rule_allows(rule, url) if url is not None else rule_allows_tcp(rule, host, port):
            return True
    return False


def private_ip(ip: ipaddress._BaseAddress) -> bool:
    return ip.is_private or ip.is_loopback or ip.is_link_local or (ip.version == 4 and ip in SHARED_NET)


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
        #: Optional limit set on this computer, on top of what OdooBridger manages.
        self.allowed = [a.strip().rstrip("/") for a in env.get("OB_ALLOW", "").split(",") if a.strip()]
        self.tls_verify = env.get("OB_TLS_VERIFY", "true").strip().lower() not in ("0", "false", "no")
        self.concurrency = max(1, int(env.get("OB_CONCURRENCY", "8") or 8))

    def problems(self) -> list[str]:
        out = []
        if not self.key.startswith("obag_"):
            out.append("OB_AGENT_KEY is missing or not an agent key (it starts with obag_).")
        if not self.relay_url.startswith(("wss://", "ws://")):
            out.append("OB_RELAY_URL must start with wss:// (or ws:// for a local test).")
        for a in self.allowed:
            try:
                parse_rule(a)
            except ValueError:
                out.append(f"OB_ALLOW entry {a!r} must be like 192.168.1.20:8069, http://192.168.1.20:8069 "
                           "or 192.168.1.0/24@80+443.")
        return out

    # Only this computer's own limit (OB_ALLOW); see Agent.permitted for the whole check.
    def allows_tcp(self, host: str, port: int) -> bool:
        return any_rule(self.allowed, host=host, port=port)

    def allows(self, url: str) -> bool:
        return any_rule(self.allowed, url=url)


class Agent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.send_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(config.concurrency)
        #: Open raw TCP connections the relay asked for (a biometric terminal's own protocol).
        self.streams: dict[str, asyncio.StreamWriter] = {}
        #: The addresses OdooBridger manages for this agent (sent on connect and
        #: whenever they change). None: an older OdooBridger that doesn't, so
        #: OB_ALLOW alone decides.
        self.managed: list[str] | None = None

    async def permitted(self, host: str, port: int, url: str | None = None) -> str | None:
        """None when the agent may reach host:port; otherwise why not (shown in OdooBridger)."""
        where = f"{host}:{port}"
        if self.managed is not None:
            if not any_rule(self.managed, url=url, host=host, port=port):
                return (f"{where} is not one of this agent's addresses yet. Add it on the Odoo page in "
                        "OdooBridger (Agents → Add Address); it applies at once.")
        elif not self.config.allowed:
            return f"{where} is not allowed: this OdooBridger doesn't manage agent addresses, so set OB_ALLOW."
        if self.config.allowed:
            if not any_rule(self.config.allowed, url=url, host=host, port=port):
                return (f"{where} is outside the OB_ALLOW limit this agent was started with. Start it again "
                        "without OB_ALLOW to let OdooBridger manage its addresses, or add it to OB_ALLOW.")
            return None
        # No local limit set: inside this network only.
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            return f"{host} is not a name this computer can resolve. Check the address."
        addresses = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos}
        if not addresses or not all(private_ip(ip) for ip in addresses):
            return (f"{host} is not on your private network. The agent only reaches addresses inside your own "
                    "network unless OB_ALLOW on this computer says otherwise.")
        return None

    async def send(self, ws, message: dict) -> None:
        try:
            async with self.send_lock:
                await ws.send(json.dumps(message))
        except ConnectionClosed:
            pass

    # --- raw TCP streams --------------------------------------------------------
    async def open_stream(self, ws, message: dict) -> None:
        sid = str(message.get("id") or "")
        host = str(message.get("host") or "").strip().lower()
        try:
            port = int(message.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        refusal = await self.permitted(host, port)
        if refusal:
            log.warning("Refused TCP %s:%s: %s", host, port, refusal)
            await self.send(ws, {"type": "stream_error", "id": sid, "code": "not_allowed", "message": refusal})
            return
        if len(self.streams) >= MAX_STREAMS:
            await self.send(ws, {"type": "stream_error", "id": sid, "code": "busy",
                                 "message": "The agent has too many device connections open. Try again shortly."})
            return
        timeout = min(float(message.get("timeout") or 15), 60)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except asyncio.TimeoutError:
            await self.send(ws, {"type": "stream_error", "id": sid, "code": "timeout",
                                 "message": f"{host}:{port} did not answer from the agent's network in time."})
            return
        except OSError as exc:
            await self.send(ws, {"type": "stream_error", "id": sid, "code": "unreachable",
                                 "message": f"Could not reach {host}:{port} from the agent's network: {exc}"})
            return
        self.streams[sid] = writer
        log.debug("TCP %s:%s open (%s)", host, port, sid)
        await self.send(ws, {"type": "opened", "id": sid})
        try:
            while True:
                chunk = await reader.read(STREAM_CHUNK)
                if not chunk:
                    break
                await self.send(ws, {"type": "data", "id": sid, "data": base64.b64encode(chunk).decode()})
        except OSError:
            pass
        finally:
            if self.streams.pop(sid, None) is not None:
                writer.close()
                await self.send(ws, {"type": "close", "id": sid})

    async def stream_data(self, message: dict) -> None:
        writer = self.streams.get(str(message.get("id") or ""))
        if writer is None:
            return
        try:
            writer.write(base64.b64decode(message.get("data") or ""))
            await writer.drain()
        except (OSError, ValueError):
            writer.close()

    def stream_close(self, message: dict) -> None:
        writer = self.streams.pop(str(message.get("id") or ""), None)
        if writer is not None:
            writer.close()

    def close_streams(self) -> None:
        for writer in list(self.streams.values()):
            writer.close()
        self.streams.clear()

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
        scheme, host, port = _url_host_port(url)
        refusal = (f"The agent only carries web requests, not {scheme or 'that'}." if scheme not in ("http", "https")
                   else await self.permitted(host, port, url))
        if refusal:
            log.warning("Refused %s %s: %s", method, origin(url), refusal)
            return {"type": "error", "code": "not_allowed", "message": refusal}
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
            # "allowed" is this computer's own limit (OB_ALLOW); "config" says
            # OdooBridger may send the managed address list.
            await ws.send(json.dumps({"type": "hello", "version": VERSION, "hostname": socket.gethostname(),
                                      "platform": platform.platform(terse=True), "allowed": self.config.allowed,
                                      "features": ["config", "tcp"]}))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if welcome.get("type") != "welcome":
                raise ConnectionError(f"Unexpected first message: {welcome!r}")
            self.managed = [str(a) for a in welcome["allowed"]] if "allowed" in welcome else None
            log.info("Connected to OdooBridger as %r.", welcome.get("name"))
            self.log_addresses()
            tasks: set[asyncio.Task] = set()
            async with httpx.AsyncClient(verify=self.config.tls_verify, follow_redirects=False) as client:
                try:
                    async for raw in ws:
                        message = json.loads(raw)
                        kind = message.get("type")
                        if kind == "request":
                            task = asyncio.create_task(self.handle(ws, client, message))
                        elif kind == "open":
                            task = asyncio.create_task(self.open_stream(ws, message))
                        elif kind == "data":
                            await self.stream_data(message)
                            continue
                        elif kind == "close":
                            self.stream_close(message)
                            continue
                        elif kind == "config":
                            self.managed = [str(a) for a in (message.get("allowed") or [])]
                            log.info("OdooBridger updated this agent's addresses.")
                            self.log_addresses()
                            continue
                        else:
                            continue
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                finally:
                    self.close_streams()

    def log_addresses(self) -> None:
        if self.managed is not None:
            log.info("Addresses (set in OdooBridger): %s", ", ".join(self.managed) or "none yet")
        if self.config.allowed:
            log.info("Limited on this computer (OB_ALLOW) to: %s", ", ".join(self.config.allowed))
        else:
            log.info("Limited to addresses inside this network.")

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
            if not address.lower().startswith(("http://", "https://")):
                print(f"  {address}: not a web address, checked when it is used")
                continue
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
            print("No OB_ALLOW set: the addresses are managed in OdooBridger (Odoo page → Agents).")
            return 0
        print("Checking the web addresses in OB_ALLOW:")
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

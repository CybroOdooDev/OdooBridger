"""Test a customer's Odoo: can we reach it, sign in, and which companies are there.

This is the core's half of the connection test: reachability, login, version,
API flavour and the company list, plus Odoo's own ``database.uuid`` when the
user may read it (the reliable identity of a database, whatever URL reaches
it). What a service needs on top — its add-on, rights on its own models — is
that service's readiness check, reported back through its usage endpoint.

Odoo 19+ is spoken to over the JSON-2 API (``/json/2/<model>/<method>`` with
the API key as a bearer token); older Odoo over XML-RPC.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import xmlrpc.client
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from obcore.config import settings

JSON2_MIN_MAJOR = 19


class OdooProbeError(Exception):
    """The connection does not work. The message is safe to show the customer."""

    def __init__(self, message: str, *, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


@dataclass
class ProbeResult:
    version: str | None
    api: str
    uid: int
    companies: list[dict[str, Any]] = field(default_factory=list)
    db_uuid: str | None = None


# --- normalising -----------------------------------------------------------------
def clean_url(url: str) -> str:
    """What we store and call: scheme + host + path, no trailing slash, no /odoo or /web."""
    url = (url or "").strip()
    if not url:
        raise OdooProbeError("Enter your Odoo address, for example https://yourcompany.odoo.com.")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise OdooProbeError("That doesn't look like a web address. Use https://yourcompany.odoo.com.")
    path = re.sub(r"/(odoo|web)(/.*)?$", "", parsed.path.rstrip("/"))
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname.lower()}{port}{path}"


def url_key(url: str) -> str:
    """The same Odoo with or without https, www. or a trailing slash compares equal."""
    parsed = urlparse(clean_url(url))
    host = (parsed.hostname or "").lower().removeprefix("www.")
    port = f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else ""
    return f"{host}{port}{parsed.path.rstrip('/')}".lower()


def db_key(db: str) -> str:
    return (db or "").strip().lower()


def guess_db(url: str) -> str | None:
    """On Odoo Online the database is the subdomain of <name>.odoo.com."""
    host = urlparse(clean_url(url)).hostname or ""
    match = re.fullmatch(r"([a-z0-9-]+)\.odoo\.com", host)
    return match.group(1) if match else None


def _major(version: str | None) -> int:
    match = re.search(r"\d+", version or "")
    return int(match.group()) if match else 0


# --- where we are willing to connect ----------------------------------------------
def private_allowed() -> bool:
    if settings.odoo_allow_private_targets is not None:
        return settings.odoo_allow_private_targets
    return not settings.is_production


def check_target(url: str) -> None:
    """Refuse an address that resolves into a private network, unless allowed.

    The connection test runs from the core's own network; without this a
    customer could make the core call its internal services.
    """
    if private_allowed():
        return
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise OdooProbeError(f"No server is known by the name in {url}. Check the address for typos.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise OdooProbeError(f"{host} is a private network address. Use your Odoo's public address.")


# --- the probe -------------------------------------------------------------------
#: Tests swap this for a fake Odoo.
client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(
    timeout=settings.http_timeout_seconds, follow_redirects=False,
    headers={"User-Agent": "OdooBridger (connection test)"})


def _unreachable(url: str, exc: Exception) -> OdooProbeError:
    text = str(exc)
    if text.startswith("The OdooBridger Agent"):
        return OdooProbeError(text)   # already says what to do
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, socket.timeout, TimeoutError)):
        return OdooProbeError(f"{url} did not answer in time. Check the address and that Odoo is running.")
    if "Name or service not known" in text or "getaddrinfo" in text or "nodename nor servname" in text:
        return OdooProbeError(f"No server is known by the name in {url}. Check the address for typos.")
    if "CERTIFICATE" in text.upper() or "SSL" in text:
        return OdooProbeError(f"{url} has a TLS certificate problem: {text[:160]}")
    if "refused" in text.lower():
        return OdooProbeError(f"{url} refused the connection. Check the address and port.")
    return OdooProbeError(f"Could not reach Odoo at {url}: {text[:200] or type(exc).__name__}")


def _version(client: httpx.Client, url: str) -> str | None:
    """Odoo's version from /web/version (Odoo 17+), else from XML-RPC, else None."""
    try:
        resp = client.get(f"{url}/web/version")
    except httpx.HTTPError as exc:
        raise _unreachable(url, exc) from exc
    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("version_info"):
            return str(data.get("version") or ".".join(str(p) for p in data["version_info"][:2]))
    if resp.status_code in (301, 302, 303, 307, 308):
        target = resp.headers.get("location", "")
        raise OdooProbeError(f"{url} redirects to {target}. Use that address instead.")
    try:
        resp = client.post(f"{url}/xmlrpc/2/common", content=xmlrpc.client.dumps((), "version"),
                           headers={"Content-Type": "text/xml"})
    except httpx.HTTPError as exc:
        raise _unreachable(url, exc) from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise OdooProbeError(f"{url} answered HTTP {resp.status_code}. Is this the address of your Odoo?")
    try:
        (info,), _ = xmlrpc.client.loads(resp.content)
    except Exception as exc:  # noqa: BLE001 — anything but Odoo's answer
        raise OdooProbeError(f"{url} answered, but not like Odoo does. Is this the address of your Odoo?") from exc
    return str(info.get("server_version") or "") or None


def _json2(client: httpx.Client, url: str, db: str, key: str, model: str, method: str,
           body: dict[str, Any]) -> Any:
    try:
        resp = client.post(f"{url}/json/2/{model}/{method}", json=body,
                           headers={"Authorization": f"bearer {key}", "X-Odoo-Database": db})
    except httpx.HTTPError as exc:
        raise _unreachable(url, exc) from exc
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError:
            raise OdooProbeError(f"{url} answered with a web page instead of Odoo's API.") from None
    try:
        data = resp.json()
    except ValueError:
        data = {}
    message = str((data or {}).get("message") or "")
    lowered = message.lower()
    if resp.status_code == 401 or "apikey" in lowered.replace(" ", ""):
        raise OdooProbeError("Odoo rejected the API key: it is wrong, revoked or expired. Create a new one in "
                             "Odoo (My Profile → Account Security → New API Key).", auth=True)
    if "database" in lowered and ("not found" in lowered or "does not exist" in lowered):
        raise OdooProbeError(f"Odoo has no database named {db!r}.", auth=True)
    if resp.status_code == 403:
        raise OdooProbeError(f"The Odoo user may not use {model}: {message[:160]}", auth=True)
    raise OdooProbeError(f"Odoo answered HTTP {resp.status_code} on {model}.{method}: {message[:200]}")


def _xmlrpc(client: httpx.Client, url: str, endpoint: str, method: str, params: tuple) -> Any:
    try:
        resp = client.post(f"{url}/xmlrpc/2/{endpoint}", content=xmlrpc.client.dumps(params, method, allow_none=True),
                           headers={"Content-Type": "text/xml"})
    except httpx.HTTPError as exc:
        raise _unreachable(url, exc) from exc
    if resp.status_code != 200:
        raise OdooProbeError(f"Odoo's XML-RPC answered HTTP {resp.status_code}.")
    try:
        (value,), _ = xmlrpc.client.loads(resp.content, use_builtin_types=True)
    except xmlrpc.client.Fault as fault:
        detail = str(fault.faultString or "")
        if "does not exist" in detail or "KeyError" in detail:
            raise OdooProbeError("Odoo has no database by that name.", auth=True) from fault
        if "AccessDenied" in detail or "Access Denied" in detail:
            raise OdooProbeError("Odoo rejected the login or API key.", auth=True) from fault
        raise OdooProbeError(f"Odoo refused: {detail.strip().splitlines()[-1][:200] if detail.strip() else fault}") from fault
    return value


def relay_client(agent_id: str) -> httpx.Client:
    """A client whose every request goes through the account's OdooBridger Agent."""
    from obcore.services.relay import RelayTransport

    return httpx.Client(transport=RelayTransport(agent_id), follow_redirects=False,
                        headers={"User-Agent": "OdooBridger (connection test)"})


#: Tests swap this for a fake agent.
relay_client_factory: Callable[[str], httpx.Client] = relay_client


def probe(url: str, db: str, login: str, api_key: str, *, agent_id: str | None = None) -> ProbeResult:
    """Run the connection test. Raises OdooProbeError with a customer-readable message."""
    url = clean_url(url)
    if agent_id is None:
        check_target(url)   # through an agent, private addresses are the point
    login = (login or "").strip()
    if not db or not login or not api_key:
        raise OdooProbeError("Enter the database, the login and the API key.")
    with (relay_client_factory(agent_id) if agent_id else client_factory()) as client:
        version = _version(client, url)
        api = "json2" if version is None or _major(version) >= JSON2_MIN_MAJOR else "xmlrpc"
        if api == "json2":
            context = _json2(client, url, db, api_key, "res.users", "context_get", {})
            uid = int((context or {}).get("uid") or 0)
            if not uid:
                raise OdooProbeError("Odoo accepted the key but would not say which user it belongs to.", auth=True)
            rows = _json2(client, url, db, api_key, "res.users", "read", {"ids": [uid], "fields": ["login"]})
            actual = str((rows or [{}])[0].get("login") or "")
            if actual and actual.lower() != login.lower():
                raise OdooProbeError(f"This API key belongs to the Odoo user {actual!r}, not {login!r}.", auth=True)
            companies = _json2(client, url, db, api_key, "res.company", "search_read",
                               {"domain": [], "fields": ["id", "name"]}) or []
            try:
                db_uuid = _json2(client, url, db, api_key, "ir.config_parameter", "get_param",
                                 {"key": "database.uuid"}) or None
            except OdooProbeError:
                db_uuid = None   # only administrators may read it; the URL check still applies
        else:
            uid = _xmlrpc(client, url, "common", "authenticate", (db, login, api_key, {}))
            if not uid:
                raise OdooProbeError("Odoo rejected the login or API key. The address is right; check the "
                                     "database, login and key.", auth=True)
            uid = int(uid)

            def call(model: str, method: str, args: list, kwargs: dict | None = None) -> Any:
                return _xmlrpc(client, url, "object", "execute_kw", (db, uid, api_key, model, method, args, kwargs or {}))

            companies = call("res.company", "search_read", [[]], {"fields": ["id", "name"]}) or []
            try:
                db_uuid = call("ir.config_parameter", "get_param", ["database.uuid"]) or None
            except OdooProbeError:
                db_uuid = None
    return ProbeResult(version=version, api=api, uid=uid,
                       companies=[{"id": c["id"], "name": c["name"]} for c in companies],
                       db_uuid=str(db_uuid) if db_uuid else None)

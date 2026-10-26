"""Turning stored connection rows into live clients.

This is the only module in the system that calls ``decrypt``. Credentials exist
in plaintext inside a client object and nowhere else — not in a schema, not in a
log line, not on the dashboard.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.core.config import settings
from app.core.crypto import decrypt
from app.integrations import providers as _providers  # noqa: F401 — registers the catalogue
from app.integrations.base import AttendanceProvider, SourceConfig, build_provider, get_provider_class
from app.integrations.odoo import OdooClient, OdooCredentials
from app.models import DeviceSource, OdooConnection, Tenant


class UnsafeTargetError(ValueError):
    """The customer-supplied URL points somewhere we refuse to call."""


def assert_safe_url(url: str) -> None:
    """SSRF guard for customer-supplied endpoints.

    Customers legitimately run BioTime on a LAN, so this is a policy switch
    rather than a hard block: cloud deployments set
    ``allow_private_network_targets=false`` and require a public hostname or a
    tunnel, while a self-hosted install leaves it on.
    """
    if settings.allow_private_network_targets:
        return

    host = urlparse(url).hostname
    if not host:
        raise UnsafeTargetError("That URL has no host")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeTargetError(f"Cannot resolve {host}") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise UnsafeTargetError(
                f"{host} resolves to the private address {ip}. Expose it through "
                "a public hostname or a tunnel."
            )


def build_odoo_client(tenant: Tenant, conn: OdooConnection) -> OdooClient:
    transport = None
    if conn.relay_agent_id and settings.platform_mode and conn.platform_connection_id:
        # A private Odoo, reached through the customer's agent: its address is
        # private on purpose and is only ever dialled on their network.
        from app.integrations.relay import PlatformRelayTransport

        transport = PlatformRelayTransport(tenant.platform_account_id, conn.platform_connection_id)
    else:
        assert_safe_url(conn.url)
    return OdooClient(
        OdooCredentials(
            url=conn.url,
            db=conn.db_name,
            username=conn.username,
            api_key=decrypt(conn.api_key_enc, tenant.crypto_key) or "",
            uid=conn.uid_cache,
            company_id=conn.company_id,
            disabled_company_ids=list(conn.disabled_company_ids or []),
            transport=transport,
        )
    )


def build_source_provider(tenant: Tenant, source: DeviceSource, db=None) -> AttendanceProvider:
    """Construct the integration a source is configured to use.

    This is the single place the vendor is decided. Callers above hold an
    ``AttendanceProvider`` and never learn which one, which is what lets a tenant
    run BioTime at one site and something else at another.
    """
    options = dict(source.config or {})
    transport = None
    tcp_connect = None
    if (source.provider or "") == "zk_adms":
        # A push device is never dialled — nothing to guard — but reads its
        # traffic from the database, so it gets this source's session.
        from sqlalchemy.orm import object_session

        options["_db"] = db or object_session(source) or object_session(tenant)
        options["_source_id"] = source.id
        options["_tenant_id"] = tenant.id
    elif options.get("relay_agent_id") and settings.platform_mode and tenant.platform_account_id:
        # A device on the customer's own network, reached through their agent:
        # its address is private on purpose and is only ever dialled there.
        from app.integrations.relay import PlatformRelayTcp, PlatformRelayTransport

        provider_cls = get_provider_class(source.provider or "biotime")
        if not provider_cls.relay_capable:
            raise UnsafeTargetError(
                f"{provider_cls.label} can't be reached through the OdooBridger Agent yet. "
                "Use a device that offers a web (HTTP) interface, or connect it another way.")
        if provider_cls.relay_kind == "tcp":
            tcp_connect = PlatformRelayTcp(tenant.platform_account_id, options["relay_agent_id"])
        else:
            transport = PlatformRelayTransport(tenant.platform_account_id, agent_id=options["relay_agent_id"])
    else:
        assert_safe_url(source.base_url)

    # Columns win over the JSON bag: they are what the connection form writes,
    # and a stale copy left in config must never quietly override them.
    options["auth_type"] = source.auth_type

    return build_provider(
        source.provider or "biotime",
        SourceConfig(
            base_url=source.base_url,
            username=source.username,
            password=decrypt(source.password_enc, tenant.crypto_key) or "",
            token=decrypt(source.token_enc, tenant.crypto_key),
            verify_ssl=source.verify_ssl,
            timezone=source.server_timezone,
            options=options,
            transport=transport,
            tcp_connect=tcp_connect,
        ),
    )


_DEFAULT_PORTS = {"http": 80, "https": 443, "zk": 4370}


def address_key(base_url: str) -> str:
    """What makes two biometric connections the *same* endpoint.

    Host and port, case-insensitive, with each scheme's default port filled in
    — so ``zk://10.0.11.43`` and ``zk://10.0.11.43:4370``, or
    ``https://BioTime.acme.com`` and ``https://biotime.acme.com:443/``, are one
    address. The scheme itself is left out on purpose: one port does not serve
    both http and https, so ``http://host:8081`` and ``https://host:8081`` are
    the same server typed two ways. For a BioTime-style URL the path is kept
    (a server mounted under a prefix is a different app from one at the root).

    No DNS lookup: ``localhost`` and ``127.0.0.1`` count as different. This is
    about stopping the same address being added twice, not about proving two
    names are one machine.
    """
    value = (base_url or "").strip()
    if value.lower().startswith("adms://"):
        return "adms:" + value[7:].strip("/").upper()
    if "://" not in value:
        value = f"zk://{value}"
    parsed = urlparse(value)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:  # a malformed port: compare the raw text instead
        return value.lower().rstrip("/")
    path = "" if scheme == "zk" else parsed.path.rstrip("/")
    return f"{host}:{port}{path}"

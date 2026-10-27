"""What an OdooBridger Agent may reach, managed in OdooBridger.

The customer installs the agent once, with only its key. Its addresses live
here: connecting an Odoo or a biometric device through the agent adds the
address that needs, and the customer can add or remove addresses on the Odoo
page. Every change is sent to the running agent at once (see
``Hub.set_managed``), so nothing on the customer's computer is ever edited or
restarted.

The agent itself still refuses anything outside the customer's own network,
and anything outside OB_ALLOW when they choose to set one there.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from obcore.models import OdooConnection, RelayAgent
from obcore.services.relay import address_of, hub, parse_rule

MAX_ADDRESSES = 50


class AddressError(ValueError):
    """The address can't be used. The message is safe to show the customer."""


def normalize(text: str) -> str:
    """The stored form: ``host:port``, or ``network@port+port`` for a range.

    Accepts what people paste: ``192.168.1.20:8069``, ``http://192.168.1.20:8069/odoo``,
    ``zk://10.0.11.43`` (port 4370), ``192.168.1.0/24@80+443``.
    """
    text = (text or "").strip().rstrip("/")
    if not text:
        raise AddressError("Enter an address, for example 192.168.1.20:8069.")
    lowered = text.lower()
    if lowered.startswith("zk://"):
        parts = urlsplit(text)
        if not parts.hostname:
            raise AddressError(f"{text} has no address in it.")
        return f"{parts.hostname.lower()}:{parts.port or 4370}"
    if "://" in text:
        if not lowered.startswith(("http://", "https://")) or not urlsplit(text).hostname:
            raise AddressError(f"{text} is not an address this agent can use.")
        return address_of(text)
    if "@" in text or "/" in text:
        try:
            rule = parse_rule(text)
        except ValueError:
            raise AddressError(f"{text} is not a valid range. Use the form 192.168.1.0/24@80+443.") from None
        _kind, network, ports = rule
        return f"{network}@{'+'.join(str(p) for p in sorted(ports))}"
    try:
        _kind, host, port = parse_rule(text)
    except ValueError:
        raise AddressError(f"Add the port to {text}, for example {text}:80 for a web page, "
                           f"{text}:4370 for a ZKTeco terminal.") from None
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        pass
    return f"{host}:{port}"


def listed(agent: RelayAgent) -> list[str]:
    return list(agent.managed_allowed or [])


def _store(agent: RelayAgent, addresses: list[str]) -> None:
    agent.managed_allowed = addresses
    hub.set_managed(agent.id, addresses)


def add(db: Session, agent: RelayAgent, text: str) -> str:
    """Add an address (no-op if it is already there). Returns its stored form."""
    entry = normalize(text)
    current = listed(agent)
    if entry not in current:
        if len(current) >= MAX_ADDRESSES:
            raise AddressError(f"An agent can have up to {MAX_ADDRESSES} addresses. Remove some, or use a range.")
        _store(agent, current + [entry])
        db.flush()
    return entry


def in_use_by(agent: RelayAgent, entry: str) -> list[str]:
    """The Odoo connections that need this address."""
    return [c.name for c in agent.account.odoo_connections
            if c.relay_agent_id == agent.id and address_of(c.url) == entry]


def remove(db: Session, agent: RelayAgent, text: str) -> None:
    entry = normalize(text)
    users = in_use_by(agent, entry)
    if users:
        raise AddressError(f"{', '.join(users)} reaches Odoo at {entry}. Change that connection first.")
    current = listed(agent)
    if entry in current:
        _store(agent, [a for a in current if a != entry])
        db.flush()


def add_for_connection(db: Session, conn: OdooConnection) -> None:
    if conn.relay_agent is not None:
        add(db, conn.relay_agent, conn.url)

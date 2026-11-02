"""Is this URL one only the machine it was typed on can reach?

A service registered at ``http://localhost:8001`` works on a developer's laptop and nowhere else: in
production the core, the customer's browser and the service all sit on different machines, so such an
address is always a leftover from development.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def is_local(url: str | None) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified

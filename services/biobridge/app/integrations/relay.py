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

import httpx

from app.core.config import settings

#: Tests swap this for a client that routes into the core in-process.
client_factory = lambda timeout: httpx.Client(timeout=timeout)  # noqa: E731


class PlatformRelayTransport(httpx.BaseTransport):
    def __init__(self, account_id: str, connection_id: str, timeout: float | None = None) -> None:
        self.account_id = account_id
        self.connection_id = connection_id
        self.timeout = float(timeout or settings.http_timeout_seconds)
        #: Distinguishes cached Odoo API detection per route (see OdooClient._detect_api).
        self.cache_key = f"relay:{connection_id}"

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        payload = {
            "account_id": self.account_id, "connection_id": self.connection_id,
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
            raise httpx.ConnectError(f"Could not reach OdooBridger's relay: {exc}", request=request) from exc
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text[:200]
            message = detail.get("message") if isinstance(detail, dict) else detail
            raise httpx.ConnectError(str(message or f"OdooBridger's relay answered {resp.status_code}."),
                                     request=request)
        data = resp.json()
        headers = {k: v for k, v in (data.get("headers") or {}).items()
                   if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")}
        return httpx.Response(int(data.get("status") or 502), headers=headers,
                              content=base64.b64decode(data.get("body") or ""), request=request)

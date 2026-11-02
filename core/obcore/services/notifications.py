"""The customer's bell: which of their services have alerts.

Every service answers the same signed question,
``GET /platform/v1/accounts/{id}/alerts``, with ``{"count": n, "severity": "bad"|"warn"|null}``.
The core shows one line per service with something to report, "You have alerts in
your <service>" and the count as a badge, and opens the service when it is clicked. The alerts themselves are
the service's to show (its own pages already do), so nothing is repeated here and the
core knows nothing about what an alert means. A new service appears in the bell by
answering the question.

Nothing is stored. The answer is read live (cached for a few seconds so several tabs
and users don't each fan out to every service). A service that cannot be reached adds
nothing: its health is the staff console's business, not a notification.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import Session

from obcore.models import Account
from obcore.services import provisioning

log = logging.getLogger(__name__)

#: Seconds an answer is reused, per account.
CACHE_SECONDS = 20
#: Seconds the bell waits for the slowest service; a late answer is left out this time.
DEADLINE_SECONDS = 6
SEVERITIES = ("bad", "warn")
MAX_COUNT = 999

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def forget() -> None:
    _cache.clear()


def _item(service, raw: Any) -> dict[str, Any] | None:
    """One service's line, or None when it has nothing to say. The answer is
    data from another system: only a count and a severity are taken from it."""
    count = raw.get("count") if isinstance(raw, dict) else None
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return None
    count = min(count, MAX_COUNT)
    severity = raw.get("severity")
    return {
        "service": service.name,
        "slug": service.slug,
        "severity": severity if severity in SEVERITIES else "warn",
        "count": count,
        "text": f"You have alerts in your {service.name}",
    }


def _fetch(job) -> dict[str, Any] | None:
    service, url, headers = job
    try:
        return _item(service, provisioning._call("GET", url, headers).json())
    except (provisioning.ProvisionError, ValueError) as exc:
        log.debug("No alerts from %s: %s", service.slug, exc)
        return None


def collect(db: Session, account: Account, *, now: float | None = None) -> dict[str, Any]:
    now = time.monotonic() if now is None else now
    hit = _cache.get(account.id)
    if hit and now - hit[0] < CACHE_SECONDS:
        items = hit[1]
    else:
        # Signed here, on this database session; only the HTTP calls run in threads.
        jobs = [(row.service, provisioning._url(row.service, account.id, "/alerts"),
                 provisioning._auth(row.service, account.id, b"", db))
                for row in account.services if row.usable and row.service.is_active]
        items = []
        if jobs:
            pool = ThreadPoolExecutor(max_workers=min(len(jobs), 8))
            try:
                futures = [pool.submit(_fetch, job) for job in jobs]
                deadline = time.monotonic() + DEADLINE_SECONDS
                for future in futures:
                    try:
                        item = future.result(timeout=max(0.1, deadline - time.monotonic()))
                    except Exception:  # noqa: BLE001 — a slow or broken service must not break the bell
                        continue
                    if item:
                        items.append(item)
            finally:
                pool.shutdown(wait=False)
        items.sort(key=lambda i: (0 if i["severity"] == "bad" else 1, i["service"]))
        _cache[account.id] = (now, items)
    return {"count": sum(i["count"] for i in items),
            "worst": items[0]["severity"] if items else None, "items": items}

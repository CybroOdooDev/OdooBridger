"""How the services are doing, for the staff overview.

Every service is checked the same way: its ``/health`` endpoint, then its own
``GET /platform/v1/metrics`` (a core-signed token, ``typ=metrics``) for the
numbers only it knows: integrations, failures, its job queue, its workers.
A service without the metrics endpoint still shows as up or down.

Results are kept for ``CACHE_SECONDS`` so the overview never waits on a slow
service twice in a row. When a service first stops answering is remembered
in this process only, so that start time resets when the core restarts.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore import keys
from obcore.models import Account, AccountService, AccountStatus, MetricSnapshot, Service, ServiceStatus
from obcore.security import utcnow

log = logging.getLogger(__name__)

CACHE_SECONDS = 30
TIMEOUT_SECONDS = 4
LIVE_STATUSES = (ServiceStatus.trialing.value, ServiceStatus.active.value, ServiceStatus.past_due.value)

#: Tests swap this for a client that routes into a service app in-process.
client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=TIMEOUT_SECONDS)

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "data": None}
_down_since: dict[str, datetime] = {}


def _metrics_headers(service: Service, db: Session | None) -> dict[str, str]:
    token = keys.sign_service_token(
        service.slug, "metrics", {"sub": "odoobridger-core", "body_sha256": keys.body_digest(b"")},
        ttl_seconds=60, db=db)
    return {"Authorization": f"Bearer {token}"}


def _check(service: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """One service's health and metrics. Never raises."""
    out: dict[str, Any] = {"reachable": False, "latency_ms": None, "metrics": None, "error": None}
    base = service["api_url"]
    try:
        with client_factory() as client:
            started = time.monotonic()
            health = client.get(f"{base}/health")
            out["latency_ms"] = round((time.monotonic() - started) * 1000)
            if health.status_code >= 400:
                out["error"] = f"Health check answered {health.status_code}"
                return out
            out["reachable"] = True
            response = client.get(f"{base}/platform/v1/metrics", headers=headers)
            if response.status_code == 200:
                out["metrics"] = response.json()
            elif response.status_code != 404:
                out["error"] = f"Metrics answered {response.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        out["error"] = f"Not reachable: {exc}"[:200]
    return out


def _status(service: Service, check: dict[str, Any] | None) -> str:
    if not service.is_active:
        return "disabled"
    if not service.client_secret_hash:
        return "not_deployed"
    if check is None or not check["reachable"]:
        return "unavailable"
    reported = ((check.get("metrics") or {}).get("status") or "operational").lower()
    return reported if reported in ("operational", "degraded", "maintenance") else "operational"


def collect(db: Session, *, fresh: bool = False) -> list[dict[str, Any]]:
    """Every catalog service with its status, customers and reported metrics."""
    with _lock:
        if not fresh and _cache["data"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
    services = db.scalars(select(Service).order_by(Service.sort_order, Service.name)).all()
    customers: dict[str, int] = {}
    for row in db.scalars(select(AccountService).where(AccountService.status.in_(LIVE_STATUSES))):
        customers[row.service_id] = customers.get(row.service_id, 0) + 1
    to_check = [s for s in services if s.is_active and s.client_secret_hash]
    jobs = {s.id: ({"api_url": s.api_url}, _metrics_headers(s, db)) for s in to_check}
    checks: dict[str, dict[str, Any]] = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            futures = {sid: pool.submit(_check, svc, headers) for sid, (svc, headers) in jobs.items()}
            checks = {sid: f.result() for sid, f in futures.items()}
    now = utcnow()
    out = []
    for service in services:
        check = checks.get(service.id)
        status = _status(service, check)
        if status == "unavailable":
            _down_since.setdefault(service.id, now)
        else:
            _down_since.pop(service.id, None)
        metrics = (check or {}).get("metrics") or {}
        out.append({
            "id": service.id, "slug": service.slug, "name": service.name, "status": status,
            "customers": customers.get(service.id, 0),
            "latency_ms": (check or {}).get("latency_ms"),
            "error": (check or {}).get("error"),
            "has_metrics": bool(metrics),
            "version": metrics.get("version"),
            "integrations": metrics.get("integrations") if metrics else None,
            "jobs": metrics.get("jobs") if metrics else None,
            "last_success_at": metrics.get("last_success_at"),
            "workers": metrics.get("workers") or {},
            "accounts": metrics.get("accounts") or [],
            "dashboard": metrics.get("dashboard") if metrics else None,
            "down_since": _down_since.get(service.id),
            "checked_at": now if check is not None else None,
        })
    with _lock:
        _cache.update(at=time.monotonic(), data=out)
    return out


def forget() -> None:
    """Drop the cached results (tests, and after a catalog change)."""
    with _lock:
        _cache.update(at=0.0, data=None)
    _down_since.clear()


# --- daily figures ------------------------------------------------------------
def current_figures(db: Session) -> dict[str, int]:
    from obcore.services.billing import monthly_total_cents

    accounts = db.scalars(select(Account).where(Account.status != AccountStatus.closed.value)).all()
    mrr = sum(monthly_total_cents(a) for a in accounts
              if a.stripe_subscription_id and a.status == AccountStatus.active.value)
    subs = sum(1 for a in accounts for r in a.services if r.status in LIVE_STATUSES)
    return {"mrr_cents": mrr, "customers": len(accounts), "subscriptions": subs}


def record_snapshot(db: Session, *, today: date | None = None) -> MetricSnapshot:
    """Store (or refresh) today's figures. Cheap; the sweep calls it each tick."""
    today = today or utcnow().date()
    figures = current_figures(db)
    row = db.get(MetricSnapshot, today)
    if row is None:
        row = MetricSnapshot(day=today)
        db.add(row)
    row.mrr_cents, row.customers, row.subscriptions = (
        figures["mrr_cents"], figures["customers"], figures["subscriptions"])
    db.flush()
    return row


def snapshot_on_or_before(db: Session, day: date) -> MetricSnapshot | None:
    return db.scalar(select(MetricSnapshot).where(MetricSnapshot.day <= day)
                     .order_by(MetricSnapshot.day.desc()).limit(1))

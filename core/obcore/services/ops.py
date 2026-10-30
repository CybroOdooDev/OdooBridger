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
import json
from datetime import date, datetime, timedelta
from typing import Any, Callable

import httpx
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from obcore import keys
from obcore.config import settings
from obcore.models import (
    Account, AccountService, AccountStatus, BillingEvent, Lead, MetricSnapshot, OdooConnection, OdooConnectionStatus,
    OPEN_STAGES, Service, ServiceCheck, ServiceStatus, Setting,
)
from obcore.security import aware, utcnow

log = logging.getLogger(__name__)

CACHE_SECONDS = 30
#: Health checks are kept this long (availability and response time).
CHECK_RETENTION = timedelta(days=30)
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


def _reason(status: str, check: dict[str, Any] | None, metrics: dict[str, Any]) -> str | None:
    """Why a service is not simply operational, in words an operator can act on."""
    if status == "unavailable":
        return ((check or {}).get("error") or "The health check got no answer.")
    if status in ("degraded", "maintenance"):
        return metrics.get("status_reason") or (
            "The service reports itself as degraded." if status == "degraded" else "The service is in maintenance.")
    return None


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
    _record_checks(db, services, checks, now)
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
            "status_reason": _reason(status, check, metrics),
            "customers": customers.get(service.id, 0),
            "latency_ms": (check or {}).get("latency_ms"),
            "error": (check or {}).get("error"),
            "has_metrics": bool(metrics),
            "version": metrics.get("version"),
            "integrations": metrics.get("integrations") if metrics else None,
            "jobs": metrics.get("jobs") if metrics else None,
            "last_success_at": metrics.get("last_success_at"),
            "workers": metrics.get("workers") or {},
            "requests": metrics.get("requests") if metrics else None,
            "database": metrics.get("database") if metrics else None,
            "accounts": metrics.get("accounts") or [],
            "dashboard": metrics.get("dashboard") if metrics else None,
            "down_since": _down_since.get(service.id),
            "checked_at": now if check is not None else None,
        })
    with _lock:
        _cache.update(at=time.monotonic(), data=out)
    return out


def _record_checks(db: Session, services: list[Service], checks: dict[str, dict[str, Any]], now: datetime) -> None:
    """Keep each check for availability and response time; drop old ones."""
    try:
        for service in services:
            check = checks.get(service.id)
            if check is None:
                continue
            db.add(ServiceCheck(service_slug=service.slug, ok=bool(check["reachable"]),
                                latency_ms=check.get("latency_ms") if check["reachable"] else None))
        db.execute(delete(ServiceCheck).where(ServiceCheck.created_at < now - CHECK_RETENTION))
        db.flush()
    except Exception:  # noqa: BLE001 — history is a nicety; the check itself must still answer
        log.exception("Could not record service checks")


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95 + 0.9999) - 1)]


def check_history(db: Session, *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Per service, from the core's own checks.

    * availability over the kept checks (30 days) with how many days they cover
      and how many checks that is, so "100%" can say "100% on 1 day of data";
    * response over the last 24 hours, the average and the 95th percentile of
      the checks that got an answer, and how many checks got none (a service
      timing out half the time must not look fast).
    """
    now = now or utcnow()
    out: dict[str, dict[str, Any]] = {}
    rows = db.execute(select(ServiceCheck.service_slug, ServiceCheck.ok, func.count(ServiceCheck.id),
                             func.min(ServiceCheck.created_at))
                      .group_by(ServiceCheck.service_slug, ServiceCheck.ok)).all()
    for slug, ok, count, first in rows:
        entry = out.setdefault(slug, {"checks": 0, "ok": 0, "since": None})
        entry["checks"] += count
        entry["ok"] += count if ok else 0
        first = aware(first)
        entry["since"] = min(filter(None, [entry["since"], first]), default=None)
    recent = db.execute(select(ServiceCheck.service_slug, ServiceCheck.ok, ServiceCheck.latency_ms)
                        .where(ServiceCheck.created_at >= now - timedelta(hours=24))).all()
    answered: dict[str, list[int]] = {}
    for slug, ok, latency in recent:
        entry = out.setdefault(slug, {"checks": 0, "ok": 0, "since": None})
        entry["checks_24h"] = entry.get("checks_24h", 0) + 1
        if ok:
            if latency is not None:
                answered.setdefault(slug, []).append(latency)
        else:
            entry["failed_checks_24h"] = entry.get("failed_checks_24h", 0) + 1
    for slug, values in answered.items():
        out[slug]["avg_latency_ms"] = round(sum(values) / len(values))
        out[slug]["p95_latency_ms"] = _p95(values)
    for entry in out.values():
        entry["availability_pct"] = round(entry["ok"] * 100 / entry["checks"], 2) if entry["checks"] else None
        entry["days_of_data"] = round((now - entry["since"]).total_seconds() / 86400, 1) if entry["since"] else 0
        entry["failed_checks_24h"] = entry.get("failed_checks_24h", 0)
        entry["checks_24h"] = entry.get("checks_24h", 0)
        entry["since"] = entry["since"].isoformat() if entry["since"] else None
    return out


def history_detail(db: Session, slug: str, *, now: datetime | None = None) -> dict[str, Any]:
    """One service's last 24 hours: hourly availability and response, and the
    outages (runs of checks that got no answer)."""
    now = now or utcnow()
    since = now - timedelta(hours=24)
    rows = db.execute(select(ServiceCheck.created_at, ServiceCheck.ok, ServiceCheck.latency_ms)
                      .where(ServiceCheck.service_slug == slug, ServiceCheck.created_at >= since)
                      .order_by(ServiceCheck.created_at)).all()
    hours: dict[int, dict[str, Any]] = {}
    outages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for created, ok, latency in rows:
        created = aware(created)
        slot = int((created - since).total_seconds() // 3600)
        hour = hours.setdefault(slot, {"start": (since + timedelta(hours=slot)).isoformat(),
                                       "checks": 0, "ok": 0, "latencies": []})
        hour["checks"] += 1
        if ok:
            hour["ok"] += 1
            if latency is not None:
                hour["latencies"].append(latency)
            if current is not None:
                current["until"] = created.isoformat()
                outages.append(current)
                current = None
        elif current is None:
            current = {"from": created.isoformat(), "until": None, "checks": 1}
        else:
            current["checks"] += 1
    if current is not None:
        outages.append(current)       # still failing at the last check
    series = []
    for slot in range(24):
        hour = hours.get(slot)
        series.append({
            "start": (since + timedelta(hours=slot)).isoformat(),
            "checks": hour["checks"] if hour else 0,
            "availability_pct": round(hour["ok"] * 100 / hour["checks"], 1) if hour else None,
            "avg_latency_ms": round(sum(hour["latencies"]) / len(hour["latencies"])) if hour and hour["latencies"] else None,
        })
    return {"slug": slug, "hours": series, "outages": outages[-10:][::-1], "checks": len(rows)}


# --- the core itself ----------------------------------------------------------
SWEEP_KEY = "sweep_last_run"


def note_sweep(db: Session) -> None:
    row = db.get(Setting, SWEEP_KEY)
    if row is None:
        row = Setting(key=SWEEP_KEY)
        db.add(row)
    row.value = json.dumps({"at": utcnow().isoformat()})
    db.flush()


def core_health(db: Session) -> dict[str, Any]:
    """The core's own row in Service Health: its database, its sweep, its requests."""
    from obcore import request_meter

    started = time.monotonic()
    try:
        db.execute(text("SELECT 1"))
        database = {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001
        database = {"ok": False, "latency_ms": None, "error": str(exc)[:120]}
    row = db.get(Setting, SWEEP_KEY)
    last = None
    if row is not None:
        try:
            last = datetime.fromisoformat(json.loads(row.value).get("at"))
        except (ValueError, TypeError, AttributeError):
            last = None
    every = timedelta(seconds=settings.sweep_interval_seconds)
    sweep_ok = last is not None and utcnow() - aware(last) <= every * 3
    status = "operational" if database["ok"] and (sweep_ok or not settings.run_background_jobs) else "degraded"
    reason = None
    if not database["ok"]:
        reason = "The database can't be reached."
    elif status == "degraded":
        reason = ("The background sweep has not run yet." if last is None else
                  f"The background sweep has not run for more than {int(every.total_seconds() * 3 // 60)} minutes.")
    return {
        "slug": "core", "name": "OdooBridger Core", "status": status if database["ok"] else "unavailable",
        "status_reason": reason,
        "requests": request_meter.summary(), "database": database,
        "scheduler": {"ok": sweep_ok, "enabled": settings.run_background_jobs, "last_run_at": aware(last).isoformat() if last else None,
                      "next_run_at": (aware(last) + every).isoformat() if last else None},
        "jobs": None,
    }


def forget() -> None:
    """Drop the cached results (tests, and after a catalog change)."""
    with _lock:
        _cache.update(at=0.0, data=None)
    _down_since.clear()


# --- daily figures ------------------------------------------------------------
def current_figures(db: Session) -> dict[str, int]:
    from obcore.services.billing import monthly_total_cents, paid_mrr_cents

    accounts = db.scalars(select(Account).where(Account.status != AccountStatus.closed.value)).all()
    mrr = sum(monthly_total_cents(a) for a in accounts
              if a.stripe_subscription_id and a.status == AccountStatus.active.value)
    subs = sum(1 for a in accounts for r in a.services if r.status in LIVE_STATUSES)
    owed = outstanding(db, accounts)
    failed_odoo = db.scalar(select(func.count(OdooConnection.id)).where(
        OdooConnection.status == OdooConnectionStatus.failed.value)) or 0
    with _lock:
        cached = _cache["data"] or []
    failed_int = sum(int((s.get("integrations") or {}).get("failed") or 0) for s in cached)
    return {"mrr_cents": mrr, "customers": len(accounts), "subscriptions": subs,
            "paying": sum(1 for a in accounts if paid_mrr_cents(a) > 0),
            "open_leads": db.scalar(select(func.count(Lead.id)).where(Lead.stage.in_(OPEN_STAGES))) or 0,
            "failed_integrations": failed_int + failed_odoo,
            "outstanding": owed["accounts"], "outstanding_cents": owed["cents"]}


def outstanding(db: Session, accounts: list[Account] | None = None) -> dict[str, Any]:
    """Accounts with an unpaid invoice, and roughly how much they owe.

    The amount is each one's latest failed payment when no payment followed it,
    otherwise its monthly price (the invoice Stripe is retrying)."""
    from obcore.services.billing import paid_mrr_cents

    if accounts is None:
        accounts = db.scalars(select(Account).where(Account.status != AccountStatus.closed.value)).all()
    owing = [a for a in accounts if a.stripe_subscription_id and (
        a.stripe_status in ("past_due", "unpaid")
        or any(r.stripe_item_id and r.status == ServiceStatus.past_due.value for r in a.services))]
    total = 0
    rows = []
    for a in owing:
        last = db.scalar(select(BillingEvent).where(BillingEvent.account_id == a.id,
                                                    BillingEvent.kind.in_(("payment", "payment_failed")))
                         .order_by(BillingEvent.created_at.desc()).limit(1))
        cents = last.amount_cents if last is not None and last.kind == "payment_failed" and last.amount_cents else paid_mrr_cents(a)
        total += cents or 0
        rows.append({"account_id": a.id, "account": a.name, "cents": cents,
                     "since": aware(last.created_at).isoformat() if last is not None and last.kind == "payment_failed" else None})
    return {"accounts": len(owing), "cents": total, "rows": rows}


def record_snapshot(db: Session, *, today: date | None = None) -> MetricSnapshot:
    """Store (or refresh) today's figures. Cheap; the sweep calls it each tick."""
    today = today or utcnow().date()
    figures = current_figures(db)
    row = db.get(MetricSnapshot, today)
    if row is None:
        row = MetricSnapshot(day=today)
        db.add(row)
    for key in ("mrr_cents", "customers", "subscriptions", "paying", "open_leads", "failed_integrations",
                "outstanding", "outstanding_cents"):
        setattr(row, key, figures[key])
    db.flush()
    return row


def snapshot_on_or_before(db: Session, day: date) -> MetricSnapshot | None:
    return db.scalar(select(MetricSnapshot).where(MetricSnapshot.day <= day)
                     .order_by(MetricSnapshot.day.desc()).limit(1))

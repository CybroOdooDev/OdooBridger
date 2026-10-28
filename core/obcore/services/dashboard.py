"""The staff overview: business figures, service health and what needs attention.

Everything here is computed from the core's own tables plus what each service
reports (``services/ops.py``). Nothing is invented: a figure with no history
yet (monthly revenue before the first daily snapshot) comes back as ``None``.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.models import (
    OPEN_STAGES, Account, AccountService, AccountStatus, Lead, OdooConnection, OdooConnectionStatus,
    PendingSignup, ServiceStatus,
)
from obcore.security import aware, utcnow
from obcore.services import ops

PERIODS = {"7d": timedelta(days=7), "30d": timedelta(days=30), "12m": timedelta(days=365)}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2}
#: A change a service hasn't acknowledged is only a problem after this long.
SYNC_GRACE = timedelta(minutes=5)


def _iso(value: datetime | None) -> str | None:
    value = aware(value) if isinstance(value, datetime) else value
    return value.isoformat() if value else None


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _mix(account: Account) -> str:
    """Where the account stands, one bucket each."""
    if account.status == AccountStatus.suspended.value:
        return "suspended"
    rows = [r for r in account.services if r.status != ServiceStatus.cancelled.value]
    if any(r.status == ServiceStatus.past_due.value for r in rows):
        return "past_due"
    if any(r.status == ServiceStatus.active.value and r.stripe_item_id for r in rows):
        return "paying"
    if any(r.status == ServiceStatus.trialing.value for r in rows):
        return "trial"
    if any(r.status == ServiceStatus.active.value for r in rows):
        return "paying"
    return "no_service"


def _subscription_label(account: Account) -> str | None:
    return {"paying": "paying", "trial": "trial", "past_due": "past_due",
            "suspended": "suspended"}.get(_mix(account))


def _growth(accounts: list[Account], now: datetime) -> list[dict[str, Any]]:
    """Total customers at the end of each of the last 12 months, and how many joined."""
    out = []
    year, month = now.year, now.month
    months = []
    for _ in range(12):
        months.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    months.reverse()
    created = [aware(a.created_at) for a in accounts]
    for y, m in months:
        start = datetime(y, m, 1, tzinfo=now.tzinfo)
        end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=now.tzinfo)
        out.append({"month": f"{y:04d}-{m:02d}",
                    "total": sum(1 for c in created if c < end),
                    "added": sum(1 for c in created if start <= c < end)})
    return out


def _incident(severity: str, issue: str, *, detail: str | None = None, account: Account | None = None,
              service: str | None = None, since: datetime | None = None, key: str,
              link: str | None = None) -> dict[str, Any]:
    return {"key": key, "severity": severity, "issue": issue[:120], "detail": (detail or "")[:400] or None,
            "account_id": account.id if account else None, "account": account.name if account else None,
            "service": service, "since": _iso(since), "link": link}


def _incidents(db: Session, accounts: dict[str, Account], health: list[dict[str, Any]],
               now: datetime) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def add(item: dict[str, Any]) -> None:
        found.setdefault(item["key"], item)   # one row per problem, customer and service

    for svc in health:
        if svc["status"] == "unavailable":
            add(_incident("critical", f"{svc['name']} is not answering", detail=svc["error"],
                          service=svc["name"], since=svc["down_since"], key=f"svc:{svc['slug']}"))
        elif svc["status"] == "degraded":
            scheduler = (svc["workers"] or {}).get("scheduler") or {}
            why = "its scheduler has stopped" if scheduler and not scheduler.get("ok") else "many integrations are failing"
            add(_incident("high", f"{svc['name']} is degraded", detail=f"{why.capitalize()}.", service=svc["name"],
                          since=_parse(scheduler.get("last_run_at")) if scheduler and not scheduler.get("ok") else None,
                          key=f"svc:{svc['slug']}"))
        for entry in svc["accounts"]:
            account = accounts.get(entry.get("account_id"))
            if account is None:
                continue
            for failure in entry.get("failures") or []:
                add(_incident("high", failure.get("issue") or "Sync failing", detail=failure.get("detail"),
                              account=account,
                              service=svc["name"], since=_parse(failure.get("since")),
                              key=f"int:{svc['slug']}:{account.id}:{failure.get('key')}",
                              link=f"#/accounts/{account.id}"))

    for conn in db.scalars(select(OdooConnection).where(
            OdooConnection.status == OdooConnectionStatus.failed.value)):
        account = accounts.get(conn.account_id)
        if account is None:
            continue
        add(_incident("high", "Odoo connection failing", detail=conn.status_message, account=account,
                      service="Odoo", since=conn.failing_since or conn.last_checked_at,
                      key=f"odoo:{conn.id}", link=f"#/accounts/{account.id}"))

    for row in db.scalars(select(AccountService)):
        account = accounts.get(row.account_id)
        if account is None or row.status == ServiceStatus.cancelled.value:
            continue
        if row.status == ServiceStatus.past_due.value:
            issue = "Payment failed" if row.stripe_item_id else "Trial ended unpaid"
            add(_incident("medium", issue, account=account, service=row.service.name,
                          since=row.trial_ends_at if not row.stripe_item_id else row.updated_at,
                          key=f"pay:{row.id}", link=f"#/accounts/{account.id}"))
        if row.provisioned_revision < row.revision and aware(row.updated_at) < now - SYNC_GRACE:
            add(_incident("medium", "Change not picked up yet",
                          detail=row.provision_error or f"{row.service.name} has not confirmed the latest change.",
                          account=account, service=row.service.name, since=row.updated_at,
                          key=f"sync:{row.id}", link=f"#/accounts/{account.id}"))

    items = list(found.values())
    items.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], i["since"] or "9999"))
    return items


def _delta(now_value: int, before: int | None) -> int | None:
    return None if before is None else now_value - before


def overview(db: Session, period: str = "30d", *, fresh: bool = False) -> dict[str, Any]:
    span = PERIODS.get(period, PERIODS["30d"])
    now = utcnow()
    start = now - span
    all_accounts = db.scalars(select(Account)).all()
    live = [a for a in all_accounts if a.status != AccountStatus.closed.value]
    accounts = {a.id: a for a in live}
    health = ops.collect(db, fresh=fresh)
    figures = ops.current_figures(db)
    ops.record_snapshot(db)
    before = ops.snapshot_on_or_before(db, start.date())

    # --- customers & subscriptions ---------------------------------------------
    new_customers = sum(1 for a in live if aware(a.created_at) >= start)
    rows = [r for a in live for r in a.services]
    live_rows = [r for r in rows if r.status in ops.LIVE_STATUSES]
    new_subs = sum(1 for r in live_rows if aware(r.created_at) >= start)
    trials = sum(1 for r in live_rows if r.status == ServiceStatus.trialing.value)
    mix = Counter(_mix(a) for a in live)

    # --- integrations & services ----------------------------------------------
    reported = [s for s in health if s["integrations"]]
    active_int = sum(int(s["integrations"].get("active") or 0) for s in reported)
    failed_int = sum(int(s["integrations"].get("failed") or 0) for s in reported)
    pending = sum(int((s["jobs"] or {}).get("pending") or 0) for s in health)
    deployed = [s for s in health if s["status"] not in ("not_deployed", "disabled")]
    operational = [s for s in deployed if s["status"] == "operational"]
    failing_customers = {e.get("account_id") for s in health for e in s["accounts"] if e.get("failed")}

    incidents = _incidents(db, accounts, health, now)
    severity = Counter(i["severity"] for i in incidents)
    oldest = min((i["since"] for i in incidents if i["since"]), default=None)
    failing_customers |= {i["account_id"] for i in incidents if i["account_id"] and i["key"].startswith(("odoo:", "int:"))}

    pending_signups = db.scalars(select(PendingSignup)).all()
    unverified = sum(1 for p in pending_signups if aware(p.expires_at) > now)
    week = now + timedelta(days=7)
    trials_ending = sum(1 for r in live_rows if r.status == ServiceStatus.trialing.value
                        and r.trial_ends_at and now <= aware(r.trial_ends_at) <= week)
    leads = db.scalars(select(Lead).where(Lead.stage.in_(OPEN_STAGES))).all()

    recent = sorted(live, key=lambda a: aware(a.created_at), reverse=True)[:6]

    def last_active(a: Account) -> str | None:
        seen = [aware(u.last_login_at) for u in a.users if u.last_login_at]
        return _iso(max(seen)) if seen else None

    recent_rows = [{
        "id": a.id, "name": a.name, "subscription": _subscription_label(a),
        "services": [r.service.name for r in a.services if r.status != ServiceStatus.cancelled.value],
        "status": a.status, "created_at": _iso(a.created_at), "last_active_at": last_active(a),
    } for a in recent] + [{
        "id": None, "name": p.company, "subscription": None, "services": [], "status": "unverified",
        "created_at": _iso(p.created_at), "last_active_at": None,
    } for p in pending_signups if aware(p.expires_at) > now]
    recent_rows = sorted(recent_rows, key=lambda r: r["created_at"] or "", reverse=True)[:6]

    return {
        "period": period if period in PERIODS else "30d",
        "kpis": {
            "customers": {"value": len(live), "delta": new_customers},
            "subscriptions": {"value": len(live_rows), "delta": new_subs, "trials": trials},
            "mrr": {"value_cents": figures["mrr_cents"],
                    "delta_pct": (round((figures["mrr_cents"] - before.mrr_cents) * 100 / before.mrr_cents, 1)
                                  if before and before.mrr_cents else None),
                    "has_history": before is not None},
            "integrations": {"value": active_int, "failed": failed_int, "pending_jobs": pending,
                             "reported_by": len(reported)},
            "services": {"operational": len(operational), "deployed": len(deployed),
                         "not_operational": [s["name"] for s in deployed if s["status"] != "operational"]},
            "attention": {"value": len(incidents), "by_severity": dict(severity), "oldest_since": oldest},
        },
        "growth": _growth(live, now),
        "services": [{k: (_iso(v) if isinstance(v, datetime) else v) for k, v in s.items()
                      if k not in ("accounts", "dashboard")}
                     for s in health],
        "incidents": incidents,
        "recent_customers": recent_rows,
        "mix": {k: mix.get(k, 0) for k in ("paying", "trial", "past_due", "suspended", "no_service")},
        "customer_facts": {"unverified": unverified, "trials_ending_7d": trials_ending,
                           "with_failures": len({c for c in failing_customers if c}),
                           "open_leads": len(leads)},
        "checked_at": _iso(now),
    }


# =============================================================================
# One service's own dashboard
# =============================================================================
def service_dashboard(db: Session, slug: str, *, fresh: bool = False) -> dict[str, Any] | None:
    """What one service reports for its dashboard, joined with the core's own
    view of its customers (name, plan, status). Only generic figures: services
    send counts and statuses, never connection details."""
    from obcore.models import Service

    service = db.scalar(select(Service).where(Service.slug == slug))
    if service is None:
        return None
    health = next((h for h in ops.collect(db, fresh=fresh) if h["slug"] == slug), None) or {}
    board = health.get("dashboard") or {}
    per_account = board.get("accounts") or {}
    rows = [r for r in db.scalars(select(AccountService).where(AccountService.service_id == service.id))
            if r.status != ServiceStatus.cancelled.value and r.account.status != AccountStatus.closed.value]
    customers = [{
        "account_id": r.account_id, "name": r.account.name,
        "plan": r.package.name if r.package else None, "status": r.status,
        "values": per_account.get(r.account_id) or {},
    } for r in sorted(rows, key=lambda r: r.account.name.lower())]
    names = {r.account_id: r.account.name for r in rows}
    attention = [{**a, "account": names.get(a.get("account_id"))} for a in board.get("attention") or []
                 if a.get("account_id") in names or not a.get("account_id")]
    attention.sort(key=lambda a: (SEVERITY_ORDER.get(a.get("severity"), 3), a.get("since") or "9999"))
    return {
        "service": {"slug": service.slug, "name": service.name, "status": health.get("status"),
                    "version": health.get("version"), "error": health.get("error"),
                    "scheduler": (health.get("workers") or {}).get("scheduler"),
                    "checked_at": _iso(health.get("checked_at")) if isinstance(health.get("checked_at"), datetime)
                    else health.get("checked_at"),
                    "has_dashboard": bool(board)},
        "tiles": board.get("tiles") or [
            {"key": "customers", "label": "Customers", "value": len(rows), "icon": "users"}],
        "series": board.get("series"),
        "columns": board.get("columns") or [],
        "customers": customers,
        "attention": attention,
    }

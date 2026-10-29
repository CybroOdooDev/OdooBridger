"""The customer's own reports in the portal: billing history, plan and usage,
and the account's activity. Everything here is about the signed-in account only.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.models import Account, AuditLog, Service, ServiceStatus
from obcore.security import aware, utcnow
from obcore.services import billing, provisioning
from obcore.services import stripe_api as stripe


def _iso(value: datetime | None) -> str | None:
    value = aware(value)
    return value.isoformat() if value else None


# =============================================================================
# Billing history
# =============================================================================
def billing_history(account: Account) -> dict[str, Any]:
    """Every invoice Stripe holds for the account (up to 100), newest first, with totals."""
    out: dict[str, Any] = {
        "pays_online": bool(account.stripe_subscription_id),
        "interval": account.billing_interval,
        "renews_at": _iso(account.renews_at),
        "cancel_at_period_end": account.cancel_at_period_end,
        "monthly_total_cents": billing.monthly_total_cents(account),
        "invoices": [], "totals": {"paid_cents": 0, "outstanding_cents": 0, "invoices": 0, "currency": None},
        "error": None,
    }
    try:
        invoices = billing.invoices(account, limit=100)
    except stripe.BillingError as exc:
        out["error"] = str(exc)
        return out
    out["invoices"] = invoices
    totals = out["totals"]
    totals["invoices"] = len(invoices)
    totals["paid_cents"] = sum(i.get("amount_paid") or 0 for i in invoices)
    totals["outstanding_cents"] = sum((i.get("amount_due") or 0) - (i.get("amount_paid") or 0)
                                      for i in invoices if i.get("status") == "open")
    totals["currency"] = next((i.get("currency") for i in invoices if i.get("currency")), None)
    return out


# =============================================================================
# Plan and usage
# =============================================================================
#: Limits a package can set, and the usage figure each one is measured against.
LIMITS = (
    ("max_employees", "employees", "Employees"),
    ("max_devices", "devices", "Terminals"),
)


def plan_usage(db: Session, account: Account) -> list[dict[str, Any]]:
    """Each live service: its package, price, trial and limits, against what the service reports."""
    rows = []
    for row in sorted(account.services, key=lambda r: r.service.sort_order):
        if row.status == ServiceStatus.cancelled.value:
            continue
        ent = row.effective_entitlements()
        price = row.package.price_cents_for(account.billing_interval) if row.package else None
        item: dict[str, Any] = {
            "service": row.service.name, "slug": row.service.slug,
            "package": row.package.name if row.package else None,
            "status": row.status, "trial_ends_at": _iso(row.trial_ends_at), "billed": bool(row.stripe_item_id),
            "price_cents": price, "currency": row.package.currency if row.package else None,
            "interval": account.billing_interval,
            "min_sync_interval_minutes": ent.get("min_sync_interval_minutes"),
            "limits": [], "last_sync": None, "sync_interval_minutes": None, "usage_error": None,
        }
        usage: dict[str, Any] = {}
        try:
            usage = provisioning.usage(db, row) or {}
        except Exception:  # noqa: BLE001 — a service that can't answer just shows no usage
            item["usage_error"] = "Usage isn't available from this service right now."
        for key, field, label in LIMITS:
            if key in ent or field in usage:
                used = usage.get(field)
                limit = ent.get(key)
                item["limits"].append({
                    "label": label, "used": used, "limit": limit,
                    "percent": round(100 * used / limit) if isinstance(used, int) and limit else None,
                })
        item["last_sync"] = usage.get("last_sync")
        item["sync_interval_minutes"] = usage.get("sync_interval_minutes")
        rows.append(item)
    return rows


# =============================================================================
# Account activity
# =============================================================================
ACTION_LABELS = {
    "account.signup": "Account created",
    "account.create": "Account created",
    "account.update": "Company details changed",
    "account.suspend": "Account suspended",
    "account.reactivate": "Account reactivated",
    "service.add": "Service added",
    "service.remove": "Service removed",
    "service.update": "Service changed",
    "service.package": "Package changed",
    "service.package_scheduled": "Package change scheduled for renewal",
    "service.package_unscheduled": "Scheduled package change cancelled",
    "service.trial_ended": "Free trial ended",
    "user.invite": "Person invited",
    "user.update": "Person's role changed",
    "user.remove": "Person removed",
    "user.verified": "Email confirmed",
    "user.link": "Sign-in link sent",
    "odoo.connect": "Odoo connected",
    "odoo.update": "Odoo connection changed",
    "odoo.disconnect": "Odoo disconnected",
    "billing.cancel": "Subscription cancelled",
    "billing.resume": "Subscription resumed",
    "billing.free_trial": "Free trial days added",
}


def _who(actor: str) -> tuple[str, bool]:
    """Display name for an actor, and whether it is one of the account's own people."""
    if actor.startswith("user:"):
        return actor[5:], True
    if actor == "system":
        return "OdooBridger (automatic)", False
    return "OdooBridger", False     # staff: never their personal address


#: Field names in "what changed" details, as people read them.
FIELD_NAMES = {"name": "company name", "timezone": "time zone", "country": "country", "url": "URL",
               "db_name": "database", "username": "login", "api key": "API key", "key_expires_at": "key expiry"}


def _readable(action: str, detail: str | None, services: dict[str, str]) -> str | None:
    """Turn an audit detail (slugs, field names) into words."""
    if not detail:
        return None
    if action in ("account.signup", "service.add", "service.remove", "service.trial_ended"):
        return ", ".join(services.get(s.strip(), s.strip()) for s in detail.split(","))
    if action in ("service.package", "service.package_scheduled") and "->" in detail:
        slug, _, package = detail.partition("->")
        return f"{services.get(slug.strip(), slug.strip())}: {package.strip()}"
    if action in ("account.update", "odoo.update"):
        return "Changed " + ", ".join(FIELD_NAMES.get(f.strip(), f.strip().replace("_", " ")) for f in detail.split(","))
    return detail


def activity(db: Session, account: Account, days: int) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    rows = db.scalars(select(AuditLog).where(AuditLog.account_id == account.id, AuditLog.created_at >= since)
                      .order_by(AuditLog.created_at.desc()).limit(1000)).all()
    services = {sv.slug: sv.name for sv in db.scalars(select(Service))}
    out = []
    for r in rows:
        who, own = _who(r.actor or "")
        out.append({
            "at": _iso(r.created_at), "who": who,
            "action": ACTION_LABELS.get(r.action, r.action.replace(".", " ").replace("_", " ").capitalize()),
            # Staff notes (a suspension's reason, say) stay with staff.
            "detail": _readable(r.action, r.detail, services) if own or r.actor == "system" else None,
            "ip": r.ip_address if own else None,
        })
    return {"days": days, "events": out, "truncated": len(rows) == 1000}

"""JSON shapes the API returns. Plain functions, one place."""

from __future__ import annotations

from typing import Any

from obcore.models import (
    Account, AccountService, Lead, LeadEvent, OdooConnection, Service, ServicePackage, StaffUser, User,
)
from obcore.security import aware


def iso(value) -> str | None:
    value = aware(value)
    return value.isoformat() if value else None


def package_out(p: ServicePackage, *, staff: bool = False) -> dict[str, Any]:
    out = {
        "id": p.id, "service_id": p.service_id, "name": p.name, "description": p.description,
        "monthly_price_cents": p.monthly_price_cents, "yearly_price_cents": p.yearly_price_cents,
        "currency": p.currency, "entitlements": p.entitlements or {}, "features": p.features or [],
        "trial_days": p.trial_days, "is_default": p.is_default, "is_active": p.is_active,
        "buyable_month": bool(p.stripe_price_id), "buyable_year": bool(p.stripe_yearly_price_id),
        "sort_order": p.sort_order,
    }
    if staff:
        out.update(stripe_price_id=p.stripe_price_id, stripe_yearly_price_id=p.stripe_yearly_price_id)
    return out


def service_out(s: Service, *, staff: bool = False) -> dict[str, Any]:
    packages = [p for p in s.packages if staff or p.is_active]
    out = {
        "id": s.id, "slug": s.slug, "name": s.name, "tagline": s.tagline, "description": s.description,
        "uses_odoo": s.uses_odoo, "base_url": s.base_url, "is_active": s.is_active, "is_public": s.is_public,
        "packages": [package_out(p, staff=staff) for p in packages],
    }
    if staff:
        out.update(internal_url=s.internal_url, sso_callback_path=s.sso_callback_path,
                   has_client_secret=bool(s.client_secret_hash), sort_order=s.sort_order)
    return out


def subscription_out(row: AccountService, *, staff: bool = False) -> dict[str, Any]:
    out = {
        "id": row.id,
        "service": {"slug": row.service.slug, "name": row.service.name, "base_url": row.service.base_url,
                    "tagline": row.service.tagline, "uses_odoo": row.service.uses_odoo},
        "package": package_out(row.package) if row.package else None,
        "status": row.status,
        "trial_ends_at": iso(row.trial_ends_at),
        "billed": bool(row.stripe_item_id),
        "setup_complete": (row.odoo_readiness or {}).get("setup_complete"),
        "pending_change": ({"package": package_out(row.pending_package), "at": iso(row.pending_change_at)}
                           if row.pending_package_id and row.pending_package else None),
        "entitlements": row.effective_entitlements(),
        "cancelled_at": iso(row.cancelled_at),
    }
    if staff:
        out.update(
            stripe_item_id=row.stripe_item_id, entitlement_overrides=row.entitlement_overrides,
            provisioned_at=iso(row.provisioned_at), provision_error=row.provision_error,
            in_sync=row.provisioned_revision >= row.revision, revision=row.revision,
        )
    return out


def user_out(u: User) -> dict[str, Any]:
    return {"id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role,
            "is_active": u.is_active, "email_verified": u.email_verified_at is not None,
            "has_password": bool(u.hashed_password), "last_login_at": iso(u.last_login_at),
            "created_at": iso(u.created_at)}


def account_out(a: Account, *, staff: bool = False) -> dict[str, Any]:
    from obcore.services.billing import monthly_total_cents, trial_bonus_days

    out = {
        "id": a.id, "name": a.name, "slug": a.slug, "status": a.status, "country": a.country,
        "timezone": a.timezone, "billing_interval": a.billing_interval,
        "pays_online": bool(a.stripe_subscription_id), "stripe_status": a.stripe_status,
        "renews_at": iso(a.renews_at), "cancel_at_period_end": a.cancel_at_period_end,
        "free_trial_offer": {"days": days} if (days := trial_bonus_days(a)) else None,
        "free_trial_added_days": a.trial_bonus_days if a.trial_bonus_at else None,
        "monthly_total_cents": monthly_total_cents(a),
        "created_at": iso(a.created_at),
        "services": [subscription_out(r, staff=staff) for r in sorted(a.services, key=lambda r: r.service.sort_order)],
    }
    if staff:
        out.update(stripe_customer_id=a.stripe_customer_id, stripe_subscription_id=a.stripe_subscription_id,
                   suspended_at=iso(a.suspended_at), suspension_reason=a.suspension_reason,
                   users=[user_out(u) for u in a.users], lead_id=a.lead_id,
                   odoo=[odoo_out(c) for c in a.odoo_connections])
    return out


def staff_out(s: StaffUser) -> dict[str, Any]:
    return {"id": s.id, "email": s.email, "full_name": s.full_name, "is_active": s.is_active,
            "is_static": s.is_static, "last_login_at": iso(s.last_login_at)}


def lead_out(lead: Lead) -> dict[str, Any]:
    return {
        "id": lead.id, "topic": lead.topic, "name": lead.name, "email": lead.email, "company": lead.company,
        "phone": lead.phone, "country": lead.country, "services": lead.services or [],
        "details": lead.details or {}, "message": lead.message, "preferred_date": lead.preferred_date,
        "preferred_window": lead.preferred_window, "timezone": lead.timezone, "source": lead.source,
        "stage": lead.stage, "stage_changed_at": iso(lead.stage_changed_at), "demo_status": lead.demo_status,
        "demo_at": iso(lead.demo_at), "lost_reason": lead.lost_reason, "owner_staff_id": lead.owner_staff_id,
        "value_cents": lead.value_cents, "account_id": lead.account_id, "created_at": iso(lead.created_at),
    }


def lead_event_out(e: LeadEvent) -> dict[str, Any]:
    return {"id": e.id, "kind": e.kind, "from": e.from_value, "to": e.to_value, "note": e.note,
            "actor": e.actor, "created_at": iso(e.created_at)}


def odoo_out(c: "OdooConnection", *, staff: bool = False) -> dict[str, Any]:
    """An Odoo connection as the portal shows it. Never includes the key."""
    services = []
    for row in c.account.services:
        if row.odoo_connection is not c:
            continue
        readiness = row.odoo_readiness or {}
        services.append({
            "slug": row.service.slug, "name": row.service.name, "status": row.status,
            "ready": readiness.get("ready"), "message": readiness.get("message"),
            "checks": readiness.get("checks") or [], "checked_at": iso(row.odoo_readiness_at),
            "in_sync": row.provisioned_revision >= row.revision,
        })
    return {
        "id": c.id, "name": c.name, "url": c.url, "db": c.db_name, "login": c.login,
        "version": c.odoo_version, "api": c.api, "companies": c.companies or [],
        "status": c.status, "status_message": c.status_message,
        "last_checked_at": iso(c.last_checked_at), "last_ok_at": iso(c.last_ok_at),
        "key_expires_at": iso(c.key_expires_at), "has_db_uuid": bool(c.db_uuid),
        "services": services, "created_at": iso(c.created_at),
    }

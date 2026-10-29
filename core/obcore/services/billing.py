"""Subscriptions: one Stripe subscription per account, one item per service.

Lifecycle of one service on an account (``AccountService``)
-----------------------------------------------------------
* **Added** from the portal (or by staff): ``trialing`` for the package's
  trial days, no card needed — unless the account already pays online, in
  which case the service is added to the subscription straight away
  (prorated) and is ``active``.
* **Checkout** ("Start paying"): one Stripe Checkout for every service not yet
  billed, all on the account's interval (monthly or yearly — Stripe needs one
  interval per subscription). The webhook links the subscription and each item.
* **Package change** on a billed service: the item's price is changed with
  proration. On a trial: just the package.
* **Removed**: a billed service's item is deleted (prorated credit); removing
  the last one cancels the subscription at the end of the paid period instead.
* **Trial ends** unpaid: ``past_due`` — the service stops working (but keeps
  the data) until the account pays.

Stripe is the authority once an account pays: every webhook re-reads the
subscription and ``apply_subscription`` maps it onto the account's services,
so handling is idempotent and order-independent. ``suspended`` is staff's and
never changed by Stripe.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from obcore.config import settings
from obcore.models import (
    Account, AccountService, Service, ServicePackage, ServiceStatus, StripeEvent,
)
from obcore.security import aware, utcnow
from obcore.services import stripe_api as stripe
from obcore.services.provisioning import mark_changed
from obcore.services.stripe_api import BillingError

log = logging.getLogger(__name__)

STATUS_MAP = {
    "active": ServiceStatus.active.value,
    "trialing": ServiceStatus.active.value,
    "past_due": ServiceStatus.past_due.value,
    "unpaid": ServiceStatus.past_due.value,
    "canceled": ServiceStatus.cancelled.value,
    "incomplete_expired": ServiceStatus.cancelled.value,
}


class SubscriptionError(ValueError):
    """A refusal whose message is safe to show."""


# =============================================================================
# Adding, changing and removing services
# =============================================================================
def default_package(service: Service) -> ServicePackage | None:
    active = [p for p in service.packages if p.is_active]
    return next((p for p in active if p.is_default), active[0] if active else None)


def _set_status(row: AccountService, status: str) -> None:
    if row.status != status:
        row.status = status
        if status == ServiceStatus.cancelled.value:
            row.cancelled_at = utcnow()
        mark_changed(row)


def add_service(db: Session, account: Account, service: Service,
                package: ServicePackage | None = None, *, trial_days: int | None = None) -> AccountService:
    package = package or default_package(service)
    if package is None:
        raise SubscriptionError(f"{service.name} has no packages on sale yet.")
    if package.service_id != service.id:
        raise SubscriptionError("That package belongs to another service.")
    row = db.scalar(select(AccountService).where(
        AccountService.account_id == account.id, AccountService.service_id == service.id))
    if row is not None and row.status != ServiceStatus.cancelled.value:
        raise SubscriptionError(f"{service.name} is already on this account.")
    if row is None:
        row = AccountService(account_id=account.id, service_id=service.id, revision=1, provisioned_revision=0)
        db.add(row)
        account.services.append(row)
    row.package_id = package.id
    row.package = package
    row.service = service
    row.cancelled_at = None
    row.stripe_item_id = None

    if account.stripe_subscription_id and stripe.enabled():
        # Already paying online: bill it from today, prorated.
        price = package.price_id_for(account.billing_interval)
        if not price:
            raise SubscriptionError(
                f"{package.name} can't be bought {'yearly' if account.billing_interval == 'year' else 'monthly'} online.")
        item = stripe.request("POST", "subscription_items", {
            "subscription": account.stripe_subscription_id, "price": price, "quantity": 1,
            "proration_behavior": "create_prorations",
            "metadata": {"account_id": account.id, "service": service.slug},
        })
        row.stripe_item_id = item["id"]
        row.trial_ends_at = None
        row.status = ServiceStatus.active.value
    else:
        days = trial_days if trial_days is not None else (
            package.trial_days if package.trial_days is not None else settings.default_trial_days)
        row.trial_ends_at = utcnow() + timedelta(days=days)
        row.status = ServiceStatus.trialing.value if days > 0 else ServiceStatus.past_due.value
    if service.uses_odoo:
        from obcore.services.odoo_connections import bind_services

        bind_services(account)
    mark_changed(row)
    db.flush()
    return row


def change_package(db: Session, row: AccountService, package: ServicePackage, *, when: str = "now") -> None:
    """Move a service to another package.

    ``when="now"`` changes it at once (a paying account is billed the difference, prorated, on
    its next invoice). ``when="period_end"`` keeps the current package until the paid period
    ends, then switches without any proration.
    """
    if package.service_id != row.service_id:
        raise SubscriptionError("That package belongs to another service.")
    if not package.is_active:
        raise SubscriptionError("That package is no longer sold.")
    paying = bool(row.stripe_item_id and stripe.enabled())
    if when == "period_end":
        if not paying or not row.account.renews_at:
            raise SubscriptionError("There is no paid period to wait for. Change the package now.")
        if package.id == row.package_id:
            clear_pending_change(row)
        else:
            if not package.price_id_for(row.account.billing_interval):
                raise SubscriptionError(f"{package.name} can't be bought online on this billing interval.")
            row.pending_package_id = package.id
            row.pending_package = package
            row.pending_change_at = row.account.renews_at
        db.flush()
        return
    if paying:
        price = package.price_id_for(row.account.billing_interval)
        if not price:
            raise SubscriptionError(f"{package.name} can't be bought online on this billing interval.")
        stripe.request("POST", f"subscription_items/{row.stripe_item_id}", {
            "price": price, "proration_behavior": "create_prorations"})
    clear_pending_change(row)
    row.package_id = package.id
    row.package = package
    mark_changed(row)
    db.flush()


def clear_pending_change(row: AccountService) -> None:
    row.pending_package_id = None
    row.pending_package = None
    row.pending_change_at = None


def apply_pending_changes(db: Session, *, now: datetime | None = None) -> list[AccountService]:
    """Make the package changes that were waiting for a renewal, once it has come."""
    now = now or utcnow()
    done: list[AccountService] = []
    for row in db.scalars(select(AccountService).where(AccountService.pending_package_id.is_not(None))):
        if row.status == ServiceStatus.cancelled.value or not row.stripe_item_id:
            clear_pending_change(row)
            continue
        if aware(row.pending_change_at) > now:
            continue
        package = row.pending_package
        if package is None or not package.is_active or not row.account.stripe_subscription_id:
            clear_pending_change(row)
            continue
        price = package.price_id_for(row.account.billing_interval)
        try:
            if not price:
                raise stripe.BillingError("no price")
            stripe.request("POST", f"subscription_items/{row.stripe_item_id}", {
                "price": price, "proration_behavior": "none"})
        except stripe.BillingError:
            log.warning("Could not apply the scheduled package change for %s; will retry", row.id)
            continue
        row.package_id = package.id
        row.package = package
        clear_pending_change(row)
        mark_changed(row)
        done.append(row)
    db.flush()
    return done


def remove_service(db: Session, row: AccountService) -> str:
    """Stop a service. Returns a sentence for the customer."""
    account = row.account
    if row.stripe_item_id and account.stripe_subscription_id and stripe.enabled():
        billed = [r for r in account.services if r.stripe_item_id and r.status != ServiceStatus.cancelled.value]
        if len(billed) <= 1:
            sub = stripe.request("POST", f"subscriptions/{account.stripe_subscription_id}",
                                 {"cancel_at_period_end": True})
            apply_subscription(db, account, sub)
            end = account.renews_at.strftime("%d %b %Y") if account.renews_at else "the end of the period"
            return f"{row.service.name} stays on until {end}, then stops. Nothing more will be charged."
        stripe.request("DELETE", f"subscription_items/{row.stripe_item_id}",
                       {"proration_behavior": "create_prorations"})
        row.stripe_item_id = None
    clear_pending_change(row)
    _set_status(row, ServiceStatus.cancelled.value)
    db.flush()
    return f"{row.service.name} has been removed."


def set_interval(account: Account, interval: str) -> None:
    if interval not in ("month", "year"):
        raise SubscriptionError("Choose monthly or yearly.")
    if account.stripe_subscription_id:
        raise SubscriptionError("Change the billing interval from Manage payment details.")
    account.billing_interval = interval


# =============================================================================
# Checkout, portal, invoices
# =============================================================================
def _ensure_customer(account: Account, email: str | None) -> str:
    if not account.stripe_customer_id:
        customer = stripe.request("POST", "customers", {
            "name": account.name, "email": email, "metadata": {"account_id": account.id},
        }, idempotency_key=f"customer-{account.id}")
        account.stripe_customer_id = customer["id"]
    return account.stripe_customer_id


def unbilled(account: Account) -> list[AccountService]:
    return [r for r in account.services
            if r.status != ServiceStatus.cancelled.value and not r.stripe_item_id and r.package]


def start_checkout(db: Session, account: Account, email: str | None, interval: str | None = None) -> str:
    if interval:
        set_interval(account, interval)
    if account.stripe_subscription_id:
        raise SubscriptionError("This account already pays online. New services are added to it automatically.")
    rows = unbilled(account)
    if not rows:
        raise SubscriptionError("Add a service first.")
    items = []
    for row in rows:
        price = row.package.price_id_for(account.billing_interval)
        if not price:
            raise SubscriptionError(
                f"{row.service.name} – {row.package.name} can't be bought "
                f"{'yearly' if account.billing_interval == 'year' else 'monthly'} online. "
                f"Contact us, or choose another package.")
        items.append({"price": price, "quantity": 1})
    customer = _ensure_customer(account, email)
    db.flush()
    base = settings.base
    session = stripe.request("POST", "checkout/sessions", {
        "mode": "subscription",
        "customer": customer,
        "client_reference_id": account.id,
        "line_items": items,
        "subscription_data": {"metadata": {"account_id": account.id}},
        "metadata": {"account_id": account.id},
        "allow_promotion_codes": True,
        "success_url": f"{base}/app/#/billing?checkout=success",
        "cancel_url": f"{base}/app/#/billing?checkout=cancelled",
    })
    return session["url"]


#: How long after registering a customer who paid up front can still ask for
#: their free trial days.
TRIAL_BONUS_WINDOW_DAYS = 30


def trial_bonus_days(account: Account) -> int:
    """Free trial days this account can still ask for; 0 when there is nothing to offer.

    Only for an account that paid at registration, once, while its first paid
    period is running: the days are the longest trial among its paid packages.
    """
    if not (account.paid_at_signup and account.stripe_subscription_id) or account.trial_bonus_at:
        return 0
    if account.cancel_at_period_end or account.stripe_status not in ("active", None):
        return 0
    created = aware(account.created_at)
    if created is None or utcnow() > created + timedelta(days=TRIAL_BONUS_WINDOW_DAYS):
        return 0
    days = [r.package.trial_days if r.package.trial_days is not None else settings.default_trial_days
            for r in account.services if r.package and r.stripe_item_id and r.status != ServiceStatus.cancelled.value]
    return max(days, default=0)


def claim_trial_bonus(db: Session, account: Account) -> int:
    """Give the free trial days: the first renewal moves back by that many days, nothing is charged.

    The year (or month) already paid for runs in full, and then the trial days
    follow before the next charge. So the customer gets the trial days *on top
    of* the paid period, not out of it. Once only.
    """
    db.refresh(account, with_for_update=True)   # two clicks at once must not give the days twice
    days = trial_bonus_days(account)
    if days <= 0:
        raise SubscriptionError("There are no free trial days to add to this account.")
    subscription = stripe.request("GET", f"subscriptions/{account.stripe_subscription_id}")
    end = _period_end(subscription)
    if end is None:
        raise SubscriptionError("Your subscription's renewal date isn't available yet. Try again shortly.")
    trial_end = end + timedelta(days=days)
    updated = stripe.request("POST", f"subscriptions/{account.stripe_subscription_id}", {
        "trial_end": int(trial_end.timestamp()),
        "proration_behavior": "none",
    }, idempotency_key=f"trial-bonus-{account.id}")
    account.trial_bonus_days = days
    account.trial_bonus_at = utcnow()
    apply_subscription(db, account, updated)
    account.renews_at = trial_end
    db.flush()
    return days


def portal_url(account: Account, flow: str | None = None) -> str:
    if not account.stripe_customer_id:
        raise SubscriptionError("There are no payment details on this account yet.")
    data: dict[str, Any] = {"customer": account.stripe_customer_id,
                            "return_url": f"{settings.base}/app/#/billing"}
    if flow == "payment_method":
        data["flow_data"] = {"type": "payment_method_update"}
    return stripe.request("POST", "billing_portal/sessions", data)["url"]


def set_cancel_at_period_end(db: Session, account: Account, cancel: bool) -> None:
    if not account.stripe_subscription_id:
        raise SubscriptionError("This account has no online subscription.")
    sub = stripe.request("POST", f"subscriptions/{account.stripe_subscription_id}",
                         {"cancel_at_period_end": cancel})
    apply_subscription(db, account, sub)


def cancel_now(account: Account) -> bool:
    """End the subscription immediately (account deletion). True if one was ended."""
    if not account.stripe_subscription_id or not stripe.enabled():
        return False
    try:
        stripe.request("DELETE", f"subscriptions/{account.stripe_subscription_id}")
    except BillingError as exc:
        if "No such subscription" not in str(exc) and "canceled" not in str(exc).lower():
            raise
    return True


def _ts(value: Any) -> str | None:
    return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat() if value else None


def invoice_out(inv: dict[str, Any]) -> dict[str, Any]:
    status = inv.get("status")
    return {
        "id": inv.get("id"), "number": inv.get("number"), "created": _ts(inv.get("created")),
        "amount_due": inv.get("amount_due"), "amount_paid": inv.get("amount_paid"),
        "currency": inv.get("currency"), "status": status, "customer": inv.get("customer"),
        "customer_name": inv.get("customer_name"), "customer_email": inv.get("customer_email"),
        "hosted_invoice_url": inv.get("hosted_invoice_url"), "invoice_pdf": inv.get("invoice_pdf"),
        "attempt_count": inv.get("attempt_count"),
        "lines": [{"description": line.get("description"), "amount": line.get("amount")}
                  for line in ((inv.get("lines") or {}).get("data") or [])][:10],
        "payable": status == "open" and bool(inv.get("hosted_invoice_url")),
    }


def invoices(account: Account, limit: int = 12) -> list[dict[str, Any]]:
    if not account.stripe_customer_id or not stripe.enabled():
        return []
    data = stripe.request("GET", "invoices", {"customer": account.stripe_customer_id, "limit": limit})
    return [invoice_out(i) for i in data.get("data") or [] if i.get("status") != "draft"]


def payment_method(account: Account) -> dict[str, Any] | None:
    if not account.stripe_customer_id or not stripe.enabled():
        return None
    customer = stripe.request("GET", f"customers/{account.stripe_customer_id}",
                              {"expand": ["invoice_settings.default_payment_method"]})
    pm = (customer.get("invoice_settings") or {}).get("default_payment_method")
    if not isinstance(pm, dict):
        return None
    card = pm.get("card") or {}
    return {"brand": card.get("brand") or pm.get("type"), "last4": card.get("last4"),
            "exp_month": card.get("exp_month"), "exp_year": card.get("exp_year")}


def monthly_total_cents(account: Account) -> int:
    """What the account's services add up to per month (yearly prices / 12)."""
    total = 0
    for row in account.services:
        if row.status == ServiceStatus.cancelled.value or not row.package:
            continue
        cents = row.package.price_cents_for(account.billing_interval) or 0
        total += cents // 12 if account.billing_interval == "year" else cents
    return total


# =============================================================================
# Webhooks
# =============================================================================
def _items(subscription: dict[str, Any]) -> list[dict[str, Any]]:
    return ((subscription.get("items") or {}).get("data")) or []


def _period_end(subscription: dict[str, Any]) -> datetime | None:
    raw = subscription.get("current_period_end")
    if raw is None:
        ends = [i.get("current_period_end") for i in _items(subscription) if i.get("current_period_end")]
        raw = max(ends) if ends else None
    return datetime.fromtimestamp(int(raw), tz=timezone.utc) if raw else None


def apply_subscription(db: Session, account: Account, subscription: dict[str, Any]) -> None:
    """Bring an account's services in line with its Stripe subscription."""
    stripe_status = subscription.get("status") or ""
    status = STATUS_MAP.get(stripe_status)
    ended = status == ServiceStatus.cancelled.value
    account.stripe_status = stripe_status or account.stripe_status
    account.cancel_at_period_end = bool(subscription.get("cancel_at_period_end"))
    end = _period_end(subscription)
    if end and not ended:
        account.renews_at = end

    seen: set[str] = set()
    for item in _items(subscription):
        price = (item.get("price") or {})
        price_id = price.get("id")
        package = db.scalar(select(ServicePackage).where(or_(
            ServicePackage.stripe_price_id == price_id,
            ServicePackage.stripe_yearly_price_id == price_id))) if price_id else None
        row = next((r for r in account.services if r.stripe_item_id == item.get("id")), None)
        if row is None and package is not None:
            row = next((r for r in account.services if r.service_id == package.service_id), None)
        if row is None:
            if package is None:
                log.warning("Subscription %s has item %s with unknown price %s",
                            subscription.get("id"), item.get("id"), price_id)
                continue
            # Added in Stripe directly (dashboard): mirror it.
            row = AccountService(account_id=account.id, service_id=package.service_id,
                                 revision=1, provisioned_revision=0)
            row.service = db.get(Service, package.service_id)
            db.add(row)
            account.services.append(row)
            db.flush()
        seen.add(row.id)
        row.stripe_item_id = item.get("id")
        if package is not None and row.package_id != package.id:
            row.package_id = package.id
            row.package = package
            mark_changed(row)
        if package is not None and price.get("recurring"):
            account.billing_interval = (price["recurring"].get("interval") or "month")
        row.trial_ends_at = None
        if status:
            _set_status(row, status)

    for row in account.services:
        if row.stripe_item_id and row.id not in seen:
            # Its item is gone from the subscription (removed, or the whole
            # subscription ended).
            row.stripe_item_id = None
            _set_status(row, ServiceStatus.cancelled.value)

    if ended:
        account.stripe_subscription_id = None
        account.cancel_at_period_end = False
    db.flush()


def handle_event(db: Session, event: dict[str, Any]) -> str:
    event_id = event.get("id") or ""
    kind = event.get("type") or ""
    if db.get(StripeEvent, event_id):
        return "duplicate"
    db.add(StripeEvent(id=event_id, type=kind))
    db.flush()
    obj = (event.get("data") or {}).get("object") or {}

    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded") \
            and (obj.get("metadata") or {}).get("pending_signup_id"):
        # A pay-first signup: no account yet. The confirmation link goes out now.
        from obcore.services import signups
        pending = signups.mark_paid(db, obj)
        if pending is None:
            log.warning("Paid checkout %s has no matching signup (%s)", obj.get("id"),
                        (obj.get("metadata") or {}).get("pending_signup_id"))
            return "unknown signup"
        return "signup paid"

    if kind == "checkout.session.completed":
        if obj.get("mode") != "subscription" or not obj.get("subscription"):
            return "ignored"
        account = _account_for(db, obj.get("client_reference_id")
                               or (obj.get("metadata") or {}).get("account_id"), obj.get("customer"))
        if account is None:
            return "unknown account"
        account.stripe_customer_id = obj.get("customer") or account.stripe_customer_id
        account.stripe_subscription_id = obj["subscription"]
        apply_subscription(db, account, stripe.request("GET", f"subscriptions/{obj['subscription']}"))
        return "linked"

    if kind.startswith("customer.subscription.") or kind.startswith("invoice."):
        sub_id = obj.get("id") if kind.startswith("customer.subscription.") else obj.get("subscription")
        if not sub_id:
            sub_id = ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
        if not sub_id:
            return "ignored"
        account = db.scalar(select(Account).where(Account.stripe_subscription_id == sub_id)) \
            or _account_for(db, (obj.get("metadata") or {}).get("account_id"), obj.get("customer"))
        if account is None:
            return "unknown account"
        subscription = stripe.request("GET", f"subscriptions/{sub_id}")
        if account.stripe_subscription_id not in (None, sub_id) \
                and subscription.get("status") in ("canceled", "incomplete_expired"):
            return "stale subscription"
        account.stripe_subscription_id = sub_id
        apply_subscription(db, account, subscription)
        return "applied"
    return "ignored"


def _account_for(db: Session, account_id: str | None, customer_id: str | None) -> Account | None:
    account = db.get(Account, account_id) if account_id else None
    if account is None and customer_id:
        account = db.scalar(select(Account).where(Account.stripe_customer_id == customer_id))
    return account


# =============================================================================
# The sweep: trials that ended unpaid
# =============================================================================
def expire_trials(db: Session) -> list[AccountService]:
    now = utcnow()
    expired = []
    for row in db.scalars(select(AccountService).where(
            AccountService.status == ServiceStatus.trialing.value,
            AccountService.stripe_item_id.is_(None),
            AccountService.trial_ends_at.is_not(None))).all():
        if aware(row.trial_ends_at) <= now:
            _set_status(row, ServiceStatus.past_due.value)
            expired.append(row)
    db.flush()
    return expired

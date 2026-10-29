"""Website signups: nothing is created until the email address is confirmed.

``start`` keeps what the signup form said in a ``PendingSignup``.

* **Free trial:** it emails the confirmation link straight away.
* **Buy Now:** it sends the visitor to Stripe Checkout first (``checkout_url``).
  Only when that payment succeeds (``mark_paid``, from the webhook or from the
  page Stripe returns to) is the confirmation link emailed.

Only ``complete`` — the person opened that link and chose a password —
creates the account, its owner and its services, tells the services, and, for a
paid signup, hands the paid Stripe subscription to the new account. An address
nobody confirms leaves no account, no trial and nothing in any service; an
unpaid row expires and is cleared by the sweep. A paid row is never cleared:
the money is already taken, so the link can be asked for again.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from obcore.config import settings
from obcore.mailer import try_send
from obcore.models import Account, PendingSignup, Service, ServicePackage, User
from obcore.security import aware, hash_password, hash_token, new_token, utcnow
from obcore.services import accounts as acc
from obcore.services import billing
from obcore.services import stripe_api as stripe


class SignupError(ValueError):
    """Safe to show."""


def start(db: Session, *, email: str, company: str, full_name: str | None, country: str | None,
          timezone_name: str | None, services: list[dict[str, Any]], interval: str, mode: str,
          ip: str | None) -> PendingSignup:
    """Keep the signup and email the confirmation link. Replaces an earlier unconfirmed one."""
    email = acc.normalise_email(email)
    if db.scalar(select(User.id).where(User.email == email)):
        raise SignupError("An account with this email already exists. Sign in instead.")
    pending = db.scalar(select(PendingSignup).where(PendingSignup.email == email))
    if pending is not None and pending.paid_at:
        # Paid already (a second attempt, or the link was lost): keep what was
        # paid for, and send the link again rather than charging twice.
        send(pending)
        db.flush()
        return pending
    if pending is None:
        pending = PendingSignup(email=email)
        db.add(pending)
    pending.company = company.strip()
    pending.full_name = (full_name or "").strip() or None
    pending.country = country
    pending.timezone = timezone_name
    pending.services = services
    pending.interval = "year" if interval == "year" else "month"
    pending.mode = "buy" if mode == "buy" else "trial"
    pending.ip_address = ip
    if pending.mode == "buy":
        # Pay first: no link until the payment is through. A token nobody has
        # seen keeps the row valid; it expires after Checkout does.
        pending.token_hash = hash_token(new_token())
        pending.expires_at = utcnow() + timedelta(hours=UNPAID_HOURS)
        pending.sent_at = None
        pending.stripe_checkout_id = None
    else:
        send(pending)
    db.flush()
    return pending


#: Longer than a Stripe Checkout stays open (24 h), so a payment that lands late
#: still finds its signup.
UNPAID_HOURS = 48


def checkout_url(db: Session, pending: PendingSignup) -> str:
    """Stripe Checkout for what this signup chose. Raises ``SignupError`` if it can't be bought online."""
    items = []
    for choice in pending.services or []:
        service = db.scalar(select(Service).where(Service.slug == choice.get("service"),
                                                  Service.is_active.is_(True)))
        if service is None:
            continue
        package = db.get(ServicePackage, choice["package_id"]) if choice.get("package_id") else None
        if package is not None and (package.service_id != service.id or not package.is_active):
            package = None
        chosen = package or billing.default_package(service)
        price = chosen.price_id_for(pending.interval) if chosen else None
        if not price:
            raise SignupError(f"{service.name} can't be bought {'yearly' if pending.interval == 'year' else 'monthly'} "
                              "online. Start a free trial, or contact us.")
        items.append({"price": price, "quantity": 1})
    if not items:
        raise SignupError("Choose a service to buy.")
    base = settings.base
    meta = {"pending_signup_id": pending.id}
    session = stripe.request("POST", "checkout/sessions", {
        "mode": "subscription",
        "customer_email": pending.email,
        "client_reference_id": pending.id,
        "line_items": items,
        "subscription_data": {"metadata": meta},
        "metadata": meta,
        "allow_promotion_codes": True,
        "success_url": f"{base}/site/signup.html?paid=1&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/site/signup.html?mode=buy&cancelled=1",
    })
    pending.stripe_checkout_id = session.get("id")
    return session["url"]


def mark_paid(db: Session, session: dict[str, Any]) -> PendingSignup | None:
    """The Checkout is paid: remember the subscription and email the confirmation link.

    Called by the webhook and by the page Stripe sends the visitor back to, so
    whichever comes first does the work and the other finds it done.
    """
    pending_id = (session.get("metadata") or {}).get("pending_signup_id") or session.get("client_reference_id")
    pending = db.get(PendingSignup, pending_id) if pending_id else None
    if pending is None or not session.get("subscription") or session.get("payment_status") not in ("paid", "no_payment_required"):
        return None
    if pending.paid_at is None:
        pending.stripe_customer_id = session.get("customer")
        pending.stripe_subscription_id = session["subscription"]
        pending.paid_at = utcnow()
        send(pending)
        db.flush()
    return pending


def send(pending: PendingSignup) -> None:
    """A fresh link (the previous one stops working) and the email carrying it."""
    if pending.mode == "buy" and pending.paid_at is None:
        return   # pay first: the link goes out once the payment is through
    raw = new_token()
    pending.token_hash = hash_token(raw)
    pending.expires_at = utcnow() + timedelta(hours=settings.email_verification_ttl_hours)
    pending.sent_at = utcnow()
    first = (pending.full_name or pending.email).split()[0]
    paid = pending.paid_at is not None
    then = ("Your payment has been received. Confirming creates your account with the services you paid for."
            if paid else "Your free trial starts when you confirm.")
    body = (f"Hi {first},\n\nThanks for signing up {pending.company} for OdooBridger.\n\n"
            f"Confirm your email and choose your password here:\n{acc.link_url(raw, acc.PURPOSE_VERIFY)}\n\n"
            f"{then} Your account is created only once you do.\n\n"
            f"The link works once and expires in {settings.email_verification_ttl_hours} hours. "
            f"Your login is this email address and the password you choose.\n\n— OdooBridger\n")
    try_send(pending.email, "Confirm your email to create your OdooBridger account", body)


def for_link(db: Session, raw: str) -> PendingSignup | None:
    if not raw:
        return None
    pending = db.scalar(select(PendingSignup).where(PendingSignup.token_hash == hash_token(raw)))
    if pending is None or (pending.mode == "buy" and pending.paid_at is None):
        return None
    expires = aware(pending.expires_at)
    if expires is None or expires < utcnow():
        return None
    return pending


def complete(db: Session, pending: PendingSignup, password: str) -> tuple[Account, User]:
    """The address is confirmed: create the account, its owner and its services.

    Flushes, doesn't commit. The caller tells the services and starts Checkout.
    """
    if db.scalar(select(User.id).where(User.email == pending.email)):
        db.delete(pending)
        raise SignupError("An account with this email already exists. Sign in instead.")
    try:
        account, user = acc.create_account(db, company=pending.company, email=pending.email,
                                           full_name=pending.full_name, country=pending.country,
                                           timezone_name=pending.timezone)
    except acc.AccountError as exc:
        raise SignupError(str(exc)) from exc
    account.billing_interval = pending.interval
    for choice in pending.services or []:
        service = db.scalar(select(Service).where(Service.slug == choice.get("service"),
                                                  Service.is_active.is_(True)))
        if service is None:
            continue   # withdrawn since the signup; the rest still go ahead
        package = db.get(ServicePackage, choice["package_id"]) if choice.get("package_id") else None
        if package is not None and (package.service_id != service.id or not package.is_active):
            package = None   # that package is gone: the service's default instead
        try:
            billing.add_service(db, account, service, package)
        except billing.SubscriptionError:
            continue
    if pending.stripe_subscription_id:
        # Paid before confirming: the new account takes over that subscription,
        # which marks each chosen service as paid and active.
        account.paid_at_signup = True
        account.stripe_customer_id = pending.stripe_customer_id
        account.stripe_subscription_id = pending.stripe_subscription_id
        billing.apply_subscription(db, account, stripe.request("GET", f"subscriptions/{pending.stripe_subscription_id}"))
    user.hashed_password = hash_password(password)
    user.email_verified_at = utcnow()
    user.last_login_at = utcnow()
    db.delete(pending)
    db.flush()
    return account, user


def clear_expired(db: Session) -> int:
    # Never a paid one: the payment is taken, so its link can always be asked for again.
    result = db.execute(delete(PendingSignup).where(PendingSignup.expires_at < utcnow(),
                                                    PendingSignup.paid_at.is_(None)))
    return result.rowcount or 0

"""The staff console API: accounts, payments, leads, the service catalog."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from obcore.api.deps import get_staff, stripe_config
from obcore.api.out import (
    account_out, iso, lead_event_out, lead_out, package_out, service_out, staff_out,
)
from obcore.db import get_db
from obcore.models import (
    DEMO_STATUSES, LEAD_STAGES, OPEN_STAGES, Account, AccountClosure, AccountService, AccountStatus,
    AuditLog, Lead, LeadEvent, Service, ServicePackage, ServiceStatus, StaffUser, User,
)
from obcore.security import aware, hash_password, utcnow, validate_password
from obcore.services import accounts as acc
from obcore.services import billing, closing, leads, provisioning, sso
from obcore.services import stripe_api as stripe

router = APIRouter(prefix="/staff", tags=["staff"], dependencies=[Depends(stripe_config)])


def actor(staff: StaffUser) -> str:
    return f"staff:{staff.email}"


def _account(db: Session, account_id: str) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found.")
    return account


def _sync(db: Session, account: Account) -> None:
    db.commit()
    db.refresh(account)
    provisioning.push_account(db, account)
    db.commit()


# =============================================================================
# Overview
# =============================================================================
@router.get("/overview")
def overview(_: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    accounts = db.scalars(select(Account)).all()
    by_status = Counter(a.status for a in accounts)
    rows = db.scalars(select(AccountService)).all()
    per_service: dict[str, Counter] = {}
    for row in rows:
        per_service.setdefault(row.service.name, Counter())[row.status] += 1
    mrr = sum(billing.monthly_total_cents(a) for a in accounts
              if a.stripe_subscription_id and a.status == AccountStatus.active.value)
    lead_rows = db.scalars(select(Lead)).all()
    leads_by_stage = Counter(lead.stage for lead in lead_rows)
    pipeline_value = sum(lead.value_cents or 0 for lead in lead_rows if lead.stage in OPEN_STAGES)
    out_of_sync = [{"account_id": r.account_id, "account": r.account.name, "service": r.service.name,
                    "error": r.provision_error}
                   for r in rows if r.provisioned_revision < r.revision][:20]
    trials_ending = sorted(
        [{"account_id": r.account_id, "account": r.account.name, "service": r.service.name,
          "trial_ends_at": iso(r.trial_ends_at)}
         for r in rows if r.status == ServiceStatus.trialing.value and r.trial_ends_at],
        key=lambda x: x["trial_ends_at"])[:10]
    recent = sorted(accounts, key=lambda a: a.created_at, reverse=True)[:8]
    return {
        "accounts": {"total": len(accounts), **by_status},
        "services": {name: dict(c) for name, c in per_service.items()},
        "mrr_cents": mrr,
        "leads": {"open": sum(leads_by_stage[s] for s in OPEN_STAGES), **leads_by_stage,
                  "pipeline_value_cents": pipeline_value},
        "out_of_sync": out_of_sync,
        "trials_ending": trials_ending,
        "recent_accounts": [{"id": a.id, "name": a.name, "created_at": iso(a.created_at),
                             "services": [r.service.name for r in a.services]} for a in recent],
        "stripe": stripe.config_summary(db),
    }


# =============================================================================
# Accounts
# =============================================================================
@router.get("/accounts")
def list_accounts(q: str | None = None, status_filter: str | None = None, service: str | None = None,
                  _: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> list[dict]:
    stmt = select(Account).order_by(Account.created_at.desc())
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Account.name).like(like), Account.id.in_(
            select(User.account_id).where(func.lower(User.email).like(like)))))
    if status_filter:
        stmt = stmt.where(Account.status == status_filter)
    out = []
    for account in db.scalars(stmt).all():
        if service and not any(r.service.slug == service for r in account.services):
            continue
        row = account_out(account)
        owner = acc.owner_of(account)
        row["owner_email"] = owner.email if owner else None
        row["users_count"] = len(account.users)
        out.append(row)
    return out


@router.get("/accounts/{account_id}")
def get_account(account_id: str, _: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    account = _account(db, account_id)
    out = account_out(account, staff=True)
    out["audit"] = [{"actor": a.actor, "action": a.action, "detail": a.detail, "at": iso(a.created_at)}
                    for a in db.scalars(select(AuditLog).where(AuditLog.account_id == account.id)
                                        .order_by(AuditLog.created_at.desc()).limit(50))]
    out["invoices"] = []
    try:
        out["invoices"] = billing.invoices(account, limit=10)
    except stripe.BillingError as exc:
        out["invoices_error"] = str(exc)
    return out


class ChoiceIn(BaseModel):
    service: str
    package_id: str | None = None
    trial_days: int | None = Field(default=None, ge=0, le=365)


class CreateAccountIn(BaseModel):
    company: str = Field(min_length=2, max_length=160)
    owner_email: EmailStr
    owner_name: str | None = Field(default=None, max_length=120)
    country: str | None = None
    timezone: str | None = None
    services: list[ChoiceIn] = Field(default_factory=list)
    send_invite: bool = True
    lead_id: str | None = None


def _service(db: Session, slug: str) -> Service:
    service = db.scalar(select(Service).where(Service.slug == slug))
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown service: {slug}")
    return service


def _create_account(db: Session, payload: CreateAccountIn, staff: StaffUser, request: Request) -> Account:
    try:
        account, owner = acc.create_account(db, company=payload.company, email=payload.owner_email,
                                            full_name=payload.owner_name, country=payload.country,
                                            timezone_name=payload.timezone)
        for choice in payload.services:
            service = _service(db, choice.service)
            package = db.get(ServicePackage, choice.package_id) if choice.package_id else None
            billing.add_service(db, account, service, package, trial_days=choice.trial_days)
    except (acc.AccountError, billing.SubscriptionError) as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    account.lead_id = payload.lead_id
    acc.audit(db, actor(staff), "account.create", account_id=account.id, request=request)
    if payload.send_invite:
        acc.send_welcome(owner)
    return account


@router.post("/accounts", status_code=status.HTTP_201_CREATED)
def create_account(payload: CreateAccountIn, request: Request, staff: StaffUser = Depends(get_staff),
                   db: Session = Depends(get_db)) -> dict:
    account = _create_account(db, payload, staff, request)
    _sync(db, account)
    return account_out(account, staff=True)


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    timezone: str | None = None
    country: str | None = None


@router.patch("/accounts/{account_id}")
def update_account(account_id: str, payload: AccountPatch, request: Request,
                   staff: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    account = _account(db, account_id)
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(account, key, value)
    provisioning.mark_account_changed(account)
    acc.audit(db, actor(staff), "account.update", account_id=account.id, detail=", ".join(data), request=request)
    _sync(db, account)
    return account_out(account, staff=True)


class ReasonIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)
    force: bool = False


@router.post("/accounts/{account_id}/suspend")
def suspend(account_id: str, payload: ReasonIn, request: Request, staff: StaffUser = Depends(get_staff),
            db: Session = Depends(get_db)) -> dict:
    account = _account(db, account_id)
    account.status = AccountStatus.suspended.value
    account.suspended_at = utcnow()
    account.suspension_reason = payload.reason
    provisioning.mark_account_changed(account)
    acc.audit(db, actor(staff), "account.suspend", account_id=account.id, detail=payload.reason, request=request)
    _sync(db, account)
    return account_out(account, staff=True)


@router.post("/accounts/{account_id}/reactivate")
def reactivate(account_id: str, request: Request, staff: StaffUser = Depends(get_staff),
               db: Session = Depends(get_db)) -> dict:
    account = _account(db, account_id)
    account.status = AccountStatus.active.value
    account.suspended_at = None
    account.suspension_reason = None
    provisioning.mark_account_changed(account)
    acc.audit(db, actor(staff), "account.reactivate", account_id=account.id, request=request)
    _sync(db, account)
    return account_out(account, staff=True)


@router.post("/accounts/{account_id}/delete")
def delete_account(account_id: str, payload: ReasonIn, staff: StaffUser = Depends(get_staff),
                   db: Session = Depends(get_db)) -> dict:
    account = _account(db, account_id)
    name = account.name
    try:
        warnings = closing.delete_account(db, account, actor=actor(staff), reason=payload.reason,
                                          force=payload.force)
    except (closing.ClosingError, stripe.BillingError) as exc:
        db.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    db.commit()
    return {"message": f"{name} was deleted.", "warnings": warnings}


@router.post("/accounts/{account_id}/services", status_code=status.HTTP_201_CREATED)
def add_service(account_id: str, payload: ChoiceIn, request: Request, staff: StaffUser = Depends(get_staff),
                db: Session = Depends(get_db)) -> dict:
    account = _account(db, account_id)
    service = _service(db, payload.service)
    package = db.get(ServicePackage, payload.package_id) if payload.package_id else None
    try:
        billing.add_service(db, account, service, package, trial_days=payload.trial_days)
    except (billing.SubscriptionError, stripe.BillingError) as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    acc.audit(db, actor(staff), "service.add", account_id=account.id, detail=service.slug, request=request)
    _sync(db, account)
    return account_out(account, staff=True)


class SubscriptionPatch(BaseModel):
    package_id: str | None = None
    #: Staff-only: set the status by hand (e.g. "active" for an account
    #: billed offline). Stripe still wins for billed services.
    status: str | None = None
    trial_ends_at: datetime | None = None
    entitlement_overrides: dict | None = None


def _subscription(account: Account, slug: str) -> AccountService:
    row = next((r for r in account.services if r.service.slug == slug), None)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That service is not on this account.")
    return row


@router.patch("/accounts/{account_id}/services/{slug}")
def update_subscription(account_id: str, slug: str, payload: SubscriptionPatch, request: Request,
                        staff: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    account = _account(db, account_id)
    row = _subscription(account, slug)
    data = payload.model_dump(exclude_unset=True)
    try:
        if payload.package_id:
            package = db.get(ServicePackage, payload.package_id)
            if package is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown package.")
            billing.change_package(db, row, package)
        if "status" in data:
            if payload.status not in {s.value for s in ServiceStatus}:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown status.")
            row.status = payload.status
        if "trial_ends_at" in data:
            row.trial_ends_at = payload.trial_ends_at
            if payload.trial_ends_at and aware(payload.trial_ends_at) > utcnow() \
                    and row.status == ServiceStatus.past_due.value and not row.stripe_item_id:
                row.status = ServiceStatus.trialing.value   # extending an ended trial reopens it
        if "entitlement_overrides" in data:
            row.entitlement_overrides = {k: v for k, v in (payload.entitlement_overrides or {}).items()
                                         if v is not None and v != ""} or None
    except (billing.SubscriptionError, stripe.BillingError) as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    provisioning.mark_changed(row)
    acc.audit(db, actor(staff), "service.update", account_id=account.id,
              detail=f"{slug}: {', '.join(data)}", request=request)
    _sync(db, account)
    return account_out(account, staff=True)


@router.delete("/accounts/{account_id}/services/{slug}")
def remove_service(account_id: str, slug: str, request: Request, staff: StaffUser = Depends(get_staff),
                   db: Session = Depends(get_db)) -> dict:
    account = _account(db, account_id)
    row = _subscription(account, slug)
    try:
        message = billing.remove_service(db, row)
    except stripe.BillingError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    acc.audit(db, actor(staff), "service.remove", account_id=account.id, detail=slug, request=request)
    _sync(db, account)
    return {"message": message, "account": account_out(account, staff=True)}


@router.post("/accounts/{account_id}/services/{slug}/resync")
def resync(account_id: str, slug: str, staff: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    account = _account(db, account_id)
    row = _subscription(account, slug)
    provisioning.mark_changed(row)
    ok = provisioning.push(db, row)
    db.commit()
    if not ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, row.provision_error or "The service did not answer.")
    return {"message": f"{row.service.name} is up to date."}


@router.get("/accounts/{account_id}/services/{slug}/usage")
def service_usage(account_id: str, slug: str, _: StaffUser = Depends(get_staff),
                  db: Session = Depends(get_db)) -> dict:
    row = _subscription(_account(db, account_id), slug)
    try:
        return provisioning.usage(db, row)
    except provisioning.ProvisionError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/accounts/{account_id}/users/{user_id}/resend")
def resend_invite(account_id: str, user_id: str, staff: StaffUser = Depends(get_staff),
                  db: Session = Depends(get_db)) -> dict:
    user = db.get(User, user_id)
    if user is None or user.account_id != account_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    if user.hashed_password:
        acc.send_reset(user)
        message = f"A password reset link was sent to {user.email}."
    else:
        acc.send_welcome(user)
        message = f"A new set-password link was sent to {user.email}."
    acc.audit(db, actor(staff), "user.link", account_id=account_id, detail=user.email)
    db.commit()
    return {"message": message}


@router.get("/closures")
def closures(_: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": c.id, "account_id": c.account_id, "account_name": c.account_name, "owner_email": c.owner_email,
             "services": c.services, "closed_by": c.closed_by, "reason": c.reason, "closed_at": iso(c.created_at)}
            for c in db.scalars(select(AccountClosure).order_by(AccountClosure.created_at.desc()).limit(200))]


# =============================================================================
# Payments
# =============================================================================
@router.get("/payments")
def payments(_: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    subscribers = []
    for account in db.scalars(select(Account).where(Account.stripe_customer_id.is_not(None))
                              .order_by(Account.name)).all():
        subscribers.append({
            "id": account.id, "name": account.name, "stripe_status": account.stripe_status,
            "billing_interval": account.billing_interval, "renews_at": iso(account.renews_at),
            "cancel_at_period_end": account.cancel_at_period_end,
            "monthly_total_cents": billing.monthly_total_cents(account),
            "services": [f"{r.service.name} – {r.package.name if r.package else '?'}"
                         for r in account.services if r.stripe_item_id],
            "stripe_customer_id": account.stripe_customer_id,
        })
    out = {"stripe": stripe.config_summary(db), "subscribers": subscribers, "invoices": [], "error": None}
    if stripe.enabled():
        try:
            data = stripe.request("GET", "invoices", {"limit": 25})
            out["invoices"] = [billing.invoice_out(i) for i in data.get("data") or [] if i.get("status") != "draft"]
        except stripe.BillingError as exc:
            out["error"] = str(exc)
    return out


class StripeKeysIn(BaseModel):
    secret_key: str | None = None
    webhook_secret: str | None = None


@router.patch("/payments/stripe")
def set_stripe(payload: StripeKeysIn, staff: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    if payload.secret_key and not payload.secret_key.startswith(("sk_", "rk_")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That doesn't look like a Stripe secret key (sk_…).")
    if payload.webhook_secret and not payload.webhook_secret.startswith("whsec_"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That doesn't look like a webhook signing secret (whsec_…).")
    stripe.save_config(db, payload.secret_key, payload.webhook_secret, staff.email)
    acc.audit(db, actor(staff), "payments.stripe_keys")
    db.commit()
    return stripe.config_summary(db)


@router.post("/payments/stripe/test")
def test_stripe(_: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    try:
        data = stripe.request("GET", "balance")
    except stripe.BillingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"message": "Stripe answered.", "livemode": data.get("livemode")}


# =============================================================================
# Service catalog
# =============================================================================
@router.get("/services")
def list_services(_: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> list[dict]:
    services = db.scalars(select(Service).order_by(Service.sort_order, Service.name)).all()
    out = []
    for s in services:
        row = service_out(s, staff=True)
        row["accounts"] = db.scalar(select(func.count(AccountService.id)).where(
            AccountService.service_id == s.id, AccountService.status != ServiceStatus.cancelled.value)) or 0
        out.append(row)
    return out


class ServiceIn(BaseModel):
    slug: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,38}$")
    name: str | None = Field(default=None, max_length=80)
    tagline: str | None = Field(default=None, max_length=200)
    description: str | None = None
    base_url: str | None = Field(default=None, max_length=255)
    internal_url: str | None = Field(default=None, max_length=255)
    sso_callback_path: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None
    is_public: bool | None = None
    sort_order: int | None = None


@router.post("/services", status_code=status.HTTP_201_CREATED)
def create_service(payload: ServiceIn, staff: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    if not payload.slug or not payload.name or not payload.base_url:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Slug, name and URL are required.")
    if db.scalar(select(Service.id).where(Service.slug == payload.slug)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A service with that slug exists.")
    service = Service(**{k: v for k, v in payload.model_dump().items() if v is not None})
    db.add(service)
    secret = sso.rotate_client_secret(service)
    acc.audit(db, actor(staff), "catalog.service_create", detail=payload.slug)
    db.commit()
    return {**service_out(service, staff=True), "client_secret": secret}


def _catalog_service(db: Session, service_id: str) -> Service:
    service = db.get(Service, service_id)
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found.")
    return service


@router.patch("/services/{service_id}")
def update_service(service_id: str, payload: ServiceIn, staff: StaffUser = Depends(get_staff),
                   db: Session = Depends(get_db)) -> dict:
    service = _catalog_service(db, service_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == "slug" and value != service.slug:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "A service's slug can't change.")
        setattr(service, key, value)
    acc.audit(db, actor(staff), "catalog.service_update", detail=service.slug)
    db.commit()
    return service_out(service, staff=True)


@router.post("/services/{service_id}/secret")
def rotate_secret(service_id: str, staff: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    service = _catalog_service(db, service_id)
    secret = sso.rotate_client_secret(service)
    acc.audit(db, actor(staff), "catalog.service_secret", detail=service.slug)
    db.commit()
    return {"client_secret": secret,
            "message": f"Set this as {service.name}'s PLATFORM_CLIENT_SECRET. It is shown only once."}


class PackageIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=240)
    monthly_price_cents: int | None = Field(default=None, ge=0)
    yearly_price_cents: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    stripe_price_id: str | None = None
    stripe_yearly_price_id: str | None = None
    entitlements: dict | None = None
    features: list[str] | None = None
    trial_days: int | None = Field(default=None, ge=0, le=365)
    is_active: bool | None = None
    is_default: bool | None = None
    sort_order: int | None = None


def _apply_package(db: Session, package: ServicePackage, data: dict) -> None:
    for key in ("stripe_price_id", "stripe_yearly_price_id"):
        if key in data and data[key] is not None:
            data[key] = data[key].strip() or None
            if data[key] and not data[key].startswith("price_"):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Stripe price ids start with price_.")
    for key, value in data.items():
        setattr(package, key, value)
    if data.get("is_default"):
        for other in db.scalars(select(ServicePackage).where(
                ServicePackage.service_id == package.service_id, ServicePackage.id != package.id)):
            other.is_default = False


@router.post("/services/{service_id}/packages", status_code=status.HTTP_201_CREATED)
def create_package(service_id: str, payload: PackageIn, staff: StaffUser = Depends(get_staff),
                   db: Session = Depends(get_db)) -> dict:
    service = _catalog_service(db, service_id)
    if not payload.name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A package needs a name.")
    if any(p.name == payload.name for p in service.packages):
        raise HTTPException(status.HTTP_409_CONFLICT, "This service already has a package with that name.")
    package = ServicePackage(service_id=service.id, entitlements={}, features=[])
    db.add(package)
    _apply_package(db, package, payload.model_dump(exclude_unset=True))
    acc.audit(db, actor(staff), "catalog.package_create", detail=f"{service.slug}/{payload.name}")
    db.commit()
    return package_out(package, staff=True)


@router.patch("/packages/{package_id}")
def update_package(package_id: str, payload: PackageIn, staff: StaffUser = Depends(get_staff),
                   db: Session = Depends(get_db)) -> dict:
    package = db.get(ServicePackage, package_id)
    if package is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Package not found.")
    data = payload.model_dump(exclude_unset=True)
    _apply_package(db, package, data)
    if "entitlements" in data:
        # Everyone on it gets the new limits.
        for row in db.scalars(select(AccountService).where(AccountService.package_id == package.id)):
            provisioning.mark_changed(row)
    acc.audit(db, actor(staff), "catalog.package_update", detail=package.name)
    db.commit()
    provisioning.push_pending(db)
    db.commit()
    return package_out(package, staff=True)


# =============================================================================
# Leads
# =============================================================================
@router.get("/leads")
def list_leads(stage: str | None = None, q: str | None = None, service: str | None = None,
               demo: str | None = None, _: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> list[dict]:
    stmt = select(Lead).order_by(Lead.created_at.desc())
    if stage == "open":
        stmt = stmt.where(Lead.stage.in_(OPEN_STAGES))
    elif stage:
        stmt = stmt.where(Lead.stage == stage)
    if demo:
        stmt = stmt.where(Lead.demo_status == demo)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Lead.name).like(like), func.lower(Lead.email).like(like),
                              func.lower(Lead.company).like(like)))
    rows = [lead_out(lead) for lead in db.scalars(stmt.limit(500)).all()]
    if service:
        rows = [r for r in rows if service in r["services"]]
    return rows


@router.get("/leads/pipeline")
def pipeline(_: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    stages = {s: [] for s in LEAD_STAGES}
    for lead in db.scalars(select(Lead).order_by(Lead.stage_changed_at.desc())).all():
        stages.setdefault(lead.stage, []).append(lead_out(lead))
    return {"stages": stages, "demo_statuses": list(DEMO_STATUSES),
            "staff": [staff_out(s) for s in db.scalars(select(StaffUser).where(StaffUser.is_active.is_(True)))]}


def _lead(db: Session, lead_id: str) -> Lead:
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found.")
    return lead


@router.get("/leads/{lead_id}")
def get_lead(lead_id: str, _: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    lead = _lead(db, lead_id)
    events = db.scalars(select(LeadEvent).where(LeadEvent.lead_id == lead.id).order_by(LeadEvent.created_at)).all()
    return {**lead_out(lead), "events": [lead_event_out(e) for e in events]}


class LeadIn(BaseModel):
    topic: str = "Sales question"
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    company: str = Field(min_length=1, max_length=160)
    phone: str | None = None
    country: str | None = None
    services: list[str] = Field(default_factory=list)
    message: str | None = None
    value_cents: int | None = None


@router.post("/leads", status_code=status.HTTP_201_CREATED)
def create_lead(payload: LeadIn, staff: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    data = payload.model_dump()
    data["email"] = acc.normalise_email(payload.email)
    data["owner_staff_id"] = staff.id
    lead = leads.create(db, data, source="staff", actor=staff.email)
    db.commit()
    return lead_out(lead)


class LeadPatch(BaseModel):
    stage: str | None = None
    lost_reason: str | None = Field(default=None, max_length=160)
    demo_status: str | None = None
    demo_at: datetime | None = None
    owner_staff_id: str | None = None
    value_cents: int | None = Field(default=None, ge=0)
    name: str | None = None
    company: str | None = None
    phone: str | None = None
    country: str | None = None
    services: list[str] | None = None


@router.patch("/leads/{lead_id}")
def update_lead(lead_id: str, payload: LeadPatch, staff: StaffUser = Depends(get_staff),
                db: Session = Depends(get_db)) -> dict:
    lead = _lead(db, lead_id)
    data = payload.model_dump(exclude_unset=True)
    try:
        if "stage" in data:
            leads.move(db, lead, payload.stage, staff.email, lost_reason=payload.lost_reason)
        if "demo_status" in data or "demo_at" in data:
            leads.set_demo(db, lead, data.get("demo_status", lead.demo_status), staff.email,
                           demo_at=payload.demo_at)
    except leads.LeadError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    for key in ("owner_staff_id", "value_cents", "name", "company", "phone", "country", "services"):
        if key in data:
            setattr(lead, key, data[key])
    db.commit()
    return lead_out(lead)


class NoteIn(BaseModel):
    note: str = Field(min_length=1, max_length=4000)


@router.post("/leads/{lead_id}/notes", status_code=status.HTTP_201_CREATED)
def add_note(lead_id: str, payload: NoteIn, staff: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    lead = _lead(db, lead_id)
    leads.record(db, lead, "note", staff.email, note=payload.note)
    db.commit()
    return get_lead(lead_id, staff, db)


class ConvertIn(BaseModel):
    services: list[ChoiceIn] | None = None
    send_invite: bool = True


@router.post("/leads/{lead_id}/convert", status_code=status.HTTP_201_CREATED)
def convert_lead(lead_id: str, payload: ConvertIn, request: Request, staff: StaffUser = Depends(get_staff),
                 db: Session = Depends(get_db)) -> dict:
    lead = _lead(db, lead_id)
    if lead.account_id and db.get(Account, lead.account_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "This lead already has an account.")
    choices = payload.services if payload.services is not None else [ChoiceIn(service=s) for s in lead.services or []]
    account = _create_account(db, CreateAccountIn(
        company=lead.company, owner_email=lead.email, owner_name=lead.name, country=lead.country,
        timezone=lead.timezone, services=choices, send_invite=payload.send_invite, lead_id=lead.id),
        staff, request)
    lead.account_id = account.id
    leads.move(db, lead, "won", staff.email)
    leads.record(db, lead, "convert", staff.email, note=f"Account {account.name} created")
    _sync(db, account)
    return {"account": account_out(account, staff=True), "lead": lead_out(lead)}


# =============================================================================
# Staff users
# =============================================================================
@router.get("/team")
def team(_: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> list[dict]:
    return [staff_out(s) for s in db.scalars(select(StaffUser).order_by(StaffUser.email))]


class StaffIn(BaseModel):
    email: EmailStr
    full_name: str | None = None
    password: str


@router.post("/team", status_code=status.HTTP_201_CREATED)
def add_staff(payload: StaffIn, staff: StaffUser = Depends(get_staff), db: Session = Depends(get_db)) -> dict:
    email = acc.normalise_email(payload.email)
    if db.scalar(select(StaffUser.id).where(StaffUser.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "That staff login exists.")
    problem = validate_password(payload.password)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    member = StaffUser(email=email, full_name=payload.full_name, hashed_password=hash_password(payload.password))
    db.add(member)
    acc.audit(db, actor(staff), "staff.add", detail=email)
    db.commit()
    return staff_out(member)


class StaffPatch(BaseModel):
    is_active: bool


@router.patch("/team/{staff_id}")
def update_staff(staff_id: str, payload: StaffPatch, staff: StaffUser = Depends(get_staff),
                 db: Session = Depends(get_db)) -> dict:
    member = db.get(StaffUser, staff_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Staff user not found.")
    if member.id == staff.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't disable yourself.")
    member.is_active = payload.is_active
    acc.audit(db, actor(staff), "staff.update", detail=f"{member.email} active={payload.is_active}")
    db.commit()
    return staff_out(member)

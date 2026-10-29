"""The customer portal: the account, its services, people and billing."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from obcore.api.deps import Customer, get_customer, require_manager, require_owner, stripe_config
from obcore.api.out import account_out, odoo_out, user_out
from obcore.db import get_db
from obcore.models import AccountService, OdooConnection, Service, ServicePackage, ServiceStatus, User, UserRole
from obcore.services import accounts as acc
from obcore.services import billing, closing, provisioning, reports
from obcore.services import odoo_connections as odoo
from obcore.services import stripe_api as stripe
from obcore.api.public import public_services

router = APIRouter(prefix="/portal", tags=["portal"], dependencies=[Depends(stripe_config)])


def _fail(exc: Exception, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(code, str(exc))


def _row(customer: Customer, slug: str) -> AccountService:
    row = next((r for r in customer.account.services if r.service.slug == slug), None)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That service is not on this account.")
    return row


def _sync(db: Session, customer: Customer) -> None:
    """Commit, then tell the services (best effort; the sweep retries)."""
    db.commit()
    db.refresh(customer.account)
    provisioning.push_account(db, customer.account)
    db.commit()


@router.get("/account")
def get_account(customer: Customer = Depends(get_customer), db: Session = Depends(get_db)) -> dict:
    have = {r.service_id for r in customer.account.services if r.status != ServiceStatus.cancelled.value}
    return {
        "account": account_out(customer.account),
        "me": user_out(customer.user),
        "available": [{"slug": s.slug, "name": s.name, "tagline": s.tagline}
                      for s in public_services(db) if s.id not in have],
        "stripe_enabled": stripe.enabled(),
    }


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    timezone: str | None = Field(default=None, max_length=64)
    country: str | None = Field(default=None, max_length=80)


@router.patch("/account")
def update_account(payload: AccountPatch, request: Request, customer: Customer = Depends(require_manager),
                   db: Session = Depends(get_db)) -> dict:
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(customer.account, key, value)
    provisioning.mark_account_changed(customer.account)
    acc.audit(db, customer.actor, "account.update", account_id=customer.account.id,
              detail=", ".join(data), request=request)
    _sync(db, customer)
    return account_out(customer.account)


class ServiceIn(BaseModel):
    service: str
    package_id: str | None = None


@router.post("/services", status_code=status.HTTP_201_CREATED)
def add_service(payload: ServiceIn, request: Request, customer: Customer = Depends(require_manager),
                db: Session = Depends(get_db)) -> dict:
    service = db.scalar(select(Service).where(Service.slug == payload.service, Service.is_active.is_(True),
                                              Service.is_public.is_(True)))
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown service.")
    package = db.get(ServicePackage, payload.package_id) if payload.package_id else None
    try:
        billing.add_service(db, customer.account, service, package)
        db.flush()
    except IntegrityError as exc:
        # Added by another request at the same moment: that one won.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"{service.name} is already on this account.") from exc
    except (billing.SubscriptionError, stripe.BillingError) as exc:
        db.rollback()
        raise _fail(exc) from exc
    acc.audit(db, customer.actor, "service.add", account_id=customer.account.id, detail=service.slug,
              request=request)
    _sync(db, customer)
    return account_out(customer.account)


class PackageIn(BaseModel):
    package_id: str
    #: "now", or "period_end" to wait for the end of the paid period.
    when: Literal["now", "period_end"] = "now"


@router.patch("/services/{slug}")
def change_package(slug: str, payload: PackageIn, request: Request,
                   customer: Customer = Depends(require_manager), db: Session = Depends(get_db)) -> dict:
    row = _row(customer, slug)
    package = db.get(ServicePackage, payload.package_id)
    if package is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown package.")
    if row.status == ServiceStatus.cancelled.value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add the service again first.")
    try:
        billing.change_package(db, row, package, when=payload.when)
    except (billing.SubscriptionError, stripe.BillingError) as exc:
        db.rollback()
        raise _fail(exc) from exc
    scheduled = payload.when == "period_end" and row.pending_package_id is not None
    acc.audit(db, customer.actor, "service.package_scheduled" if scheduled else "service.package",
              account_id=customer.account.id, detail=f"{slug} -> {package.name}", request=request)
    _sync(db, customer)
    return account_out(customer.account)


@router.delete("/services/{slug}/pending-change")
def cancel_pending_change(slug: str, request: Request, customer: Customer = Depends(require_manager),
                          db: Session = Depends(get_db)) -> dict:
    row = _row(customer, slug)
    billing.clear_pending_change(row)
    acc.audit(db, customer.actor, "service.package_unscheduled", account_id=customer.account.id,
              detail=slug, request=request)
    _sync(db, customer)
    return account_out(customer.account)


@router.delete("/services/{slug}")
def remove_service(slug: str, request: Request, customer: Customer = Depends(require_manager),
                   db: Session = Depends(get_db)) -> dict:
    row = _row(customer, slug)
    if row.status == ServiceStatus.cancelled.value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That service is already removed.")
    try:
        message = billing.remove_service(db, row)
    except stripe.BillingError as exc:
        db.rollback()
        raise _fail(exc) from exc
    acc.audit(db, customer.actor, "service.remove", account_id=customer.account.id, detail=slug,
              request=request)
    _sync(db, customer)
    return {"message": message, "account": account_out(customer.account)}


# --- people ---------------------------------------------------------------------
@router.get("/users")
def list_users(customer: Customer = Depends(get_customer)) -> list[dict]:
    return [user_out(u) for u in customer.account.users]


class InviteIn(BaseModel):
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=120)
    role: str = UserRole.member.value


@router.post("/users", status_code=status.HTTP_201_CREATED)
def invite_user(payload: InviteIn, request: Request, customer: Customer = Depends(require_manager),
                db: Session = Depends(get_db)) -> dict:
    if payload.role == UserRole.owner.value and customer.user.role != UserRole.owner.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can add another owner.")
    try:
        user = acc.add_user(db, customer.account, email=payload.email, full_name=payload.full_name,
                            role=payload.role)
    except acc.AccountError as exc:
        raise _fail(exc, status.HTTP_409_CONFLICT) from exc
    db.refresh(customer.account)
    acc.send_welcome(user, invited_by=customer.user.full_name or customer.user.email)
    provisioning.mark_account_changed(customer.account)
    acc.audit(db, customer.actor, "user.invite", account_id=customer.account.id, detail=user.email,
              request=request)
    _sync(db, customer)
    return user_out(user)


class UserPatch(BaseModel):
    role: str | None = None
    is_active: bool | None = None


def _member(customer: Customer, db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None or user.account_id != customer.account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    return user


@router.patch("/users/{user_id}")
def update_user(user_id: str, payload: UserPatch, request: Request,
                customer: Customer = Depends(require_manager), db: Session = Depends(get_db)) -> dict:
    user = _member(customer, db, user_id)
    if user.id == customer.user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't change your own role or access.")
    if (user.role == UserRole.owner.value or payload.role == UserRole.owner.value) \
            and customer.user.role != UserRole.owner.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can change an owner.")
    if payload.role is not None:
        if payload.role not in (r.value for r in UserRole):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown role.")
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    provisioning.mark_account_changed(customer.account)
    acc.audit(db, customer.actor, "user.update", account_id=customer.account.id, detail=user.email,
              request=request)
    _sync(db, customer)
    return user_out(user)


@router.delete("/users/{user_id}")
def remove_user(user_id: str, request: Request, customer: Customer = Depends(require_manager),
                db: Session = Depends(get_db)) -> dict:
    user = _member(customer, db, user_id)
    if user.id == customer.user.id or user.role == UserRole.owner.value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The owner (and yourself) can't be removed here.")
    acc.audit(db, customer.actor, "user.remove", account_id=customer.account.id, detail=user.email,
              request=request)
    db.delete(user)
    db.flush()
    db.refresh(customer.account)
    provisioning.mark_account_changed(customer.account)
    _sync(db, customer)
    return {"message": f"{user.email} was removed."}


@router.post("/users/{user_id}/resend")
def resend_invite(user_id: str, customer: Customer = Depends(require_manager),
                  db: Session = Depends(get_db)) -> dict:
    user = _member(customer, db, user_id)
    if user.email_verified_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "They have already set their password.")
    acc.send_welcome(user, invited_by=customer.user.full_name or customer.user.email)
    db.commit()
    return {"message": f"A new link was sent to {user.email}."}


# --- billing --------------------------------------------------------------------
@router.get("/billing")
def billing_overview(customer: Customer = Depends(get_customer)) -> dict:
    account = customer.account
    out = {"account": account_out(account), "stripe_enabled": stripe.enabled(),
           "unbilled": [r.service.slug for r in billing.unbilled(account)],
           "invoices": [], "payment_method": None, "error": None}
    try:
        out["invoices"] = billing.invoices(account)
        out["payment_method"] = billing.payment_method(account)
    except stripe.BillingError as exc:
        out["error"] = str(exc)
    return out


class CheckoutIn(BaseModel):
    interval: str | None = None


@router.post("/billing/checkout")
def checkout(payload: CheckoutIn, customer: Customer = Depends(require_manager),
             db: Session = Depends(get_db)) -> dict:
    try:
        url = billing.start_checkout(db, customer.account, customer.user.email, payload.interval)
    except (billing.SubscriptionError, stripe.BillingError) as exc:
        db.rollback()
        raise _fail(exc) from exc
    db.commit()
    return {"url": url}


# --- reports ----------------------------------------------------------------------
@router.get("/reports/billing")
def report_billing(customer: Customer = Depends(get_customer)) -> dict:
    return reports.billing_history(customer.account)


@router.get("/reports/usage")
def report_usage(customer: Customer = Depends(get_customer), db: Session = Depends(get_db)) -> dict:
    return {"services": reports.plan_usage(db, customer.account)}


@router.get("/reports/activity")
def report_activity(days: int = 90, customer: Customer = Depends(require_manager),
                    db: Session = Depends(get_db)) -> dict:
    """Who did what on the account. Owners and admins only: it names people and addresses."""
    return reports.activity(db, customer.account, max(1, min(days, 730)))


@router.post("/billing/free-trial")
def free_trial(request: Request, customer: Customer = Depends(require_manager),
               db: Session = Depends(get_db)) -> dict:
    """Take the free trial days a customer who paid at registration is offered."""
    try:
        days = billing.claim_trial_bonus(db, customer.account)
    except (billing.SubscriptionError, stripe.BillingError) as exc:
        db.rollback()
        raise _fail(exc) from exc
    acc.audit(db, customer.actor, "billing.free_trial", account_id=customer.account.id, request=request,
              detail=f"{days} days")
    db.commit()
    return account_out(customer.account)


@router.post("/billing/interval")
def set_interval(payload: CheckoutIn, customer: Customer = Depends(require_manager),
                 db: Session = Depends(get_db)) -> dict:
    try:
        billing.set_interval(customer.account, payload.interval or "")
    except billing.SubscriptionError as exc:
        raise _fail(exc) from exc
    db.commit()
    return account_out(customer.account)


class PortalIn(BaseModel):
    flow: str | None = None


@router.post("/billing/portal")
def billing_portal(payload: PortalIn, customer: Customer = Depends(require_manager)) -> dict:
    try:
        return {"url": billing.portal_url(customer.account, payload.flow)}
    except (billing.SubscriptionError, stripe.BillingError) as exc:
        raise _fail(exc) from exc


@router.post("/billing/cancel")
def cancel(request: Request, customer: Customer = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    try:
        billing.set_cancel_at_period_end(db, customer.account, True)
    except (billing.SubscriptionError, stripe.BillingError) as exc:
        raise _fail(exc) from exc
    acc.audit(db, customer.actor, "billing.cancel", account_id=customer.account.id, request=request)
    _sync(db, customer)
    return account_out(customer.account)


@router.post("/billing/resume")
def resume(request: Request, customer: Customer = Depends(require_owner), db: Session = Depends(get_db)) -> dict:
    try:
        billing.set_cancel_at_period_end(db, customer.account, False)
    except (billing.SubscriptionError, stripe.BillingError) as exc:
        raise _fail(exc) from exc
    acc.audit(db, customer.actor, "billing.resume", account_id=customer.account.id, request=request)
    _sync(db, customer)
    return account_out(customer.account)


class DeleteIn(BaseModel):
    confirm_name: str
    reason: str | None = Field(default=None, max_length=1000)


@router.post("/account/delete")
def delete_account(payload: DeleteIn, customer: Customer = Depends(require_owner),
                   db: Session = Depends(get_db)) -> dict:
    account = customer.account
    if payload.confirm_name.strip() != account.name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Type the account name exactly to confirm.")
    try:
        closing.delete_account(db, account, actor=customer.actor, reason=payload.reason)
    except (closing.ClosingError, stripe.BillingError) as exc:
        db.rollback()
        raise _fail(exc, status.HTTP_502_BAD_GATEWAY) from exc
    db.commit()
    return {"message": f"{account.name} and its data were deleted."}


# --- Odoo -----------------------------------------------------------------------


def _connection(customer: Customer, conn_id: str) -> OdooConnection:
    conn = next((c for c in customer.account.odoo_connections if c.id == conn_id), None)
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That Odoo connection is not on this account.")
    return conn


def _odoo_view(customer: Customer) -> dict:
    account = customer.account
    return {
        "connections": [odoo_out(c) for c in account.odoo_connections],
        "max": odoo.MAX_PER_ACCOUNT,
        "services_without": [{"slug": r.service.slug, "name": r.service.name} for r in account.services
                             if r.service.uses_odoo and r.status != ServiceStatus.cancelled.value
                             and r.odoo_connection is None],
    }


@router.get("/odoo")
def get_odoo(customer: Customer = Depends(get_customer)) -> dict:
    return _odoo_view(customer)


class OdooIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    url: str = Field(min_length=3, max_length=500)
    db: str | None = Field(default=None, max_length=120)
    login: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=8, max_length=200)
    key_expires_at: str | None = Field(default=None, max_length=32)


@router.post("/odoo", status_code=status.HTTP_201_CREATED)
def add_odoo(payload: OdooIn, request: Request, customer: Customer = Depends(require_manager),
             db: Session = Depends(get_db)) -> dict:
    try:
        conn = odoo.create(db, customer.account, url=payload.url, db_name=payload.db or "", login=payload.login,
                           api_key_value=payload.api_key, name=payload.name, key_expires_at=payload.key_expires_at)
    except odoo.OdooConnectionError as exc:
        db.rollback()
        raise _fail(exc) from exc
    except IntegrityError as exc:
        # Two accounts connecting the same database at the same moment.
        db.rollback()
        raise _fail(odoo.OdooConnectionError(
            "This Odoo database is already connected to another OdooBridger account.")) from exc
    acc.audit(db, customer.actor, "odoo.connect", account_id=customer.account.id,
              detail=f"{conn.url} / {conn.db_name}", request=request)
    _sync(db, customer)
    odoo.refresh_readiness(db, conn, live=True)
    db.commit()
    return _odoo_view(customer)


class OdooPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    url: str | None = Field(default=None, max_length=500)
    db: str | None = Field(default=None, max_length=120)
    login: str | None = Field(default=None, max_length=255)
    #: Blank keeps the current key.
    api_key: str | None = Field(default=None, max_length=200)
    key_expires_at: str | None = Field(default=None, max_length=32)


@router.patch("/odoo/{conn_id}")
def change_odoo(conn_id: str, payload: OdooPatch, request: Request, customer: Customer = Depends(require_manager),
                db: Session = Depends(get_db)) -> dict:
    conn = _connection(customer, conn_id)
    data = payload.model_dump(exclude_unset=True)
    try:
        odoo.update(db, conn, url=data.get("url"), db_name=data.get("db"), login=data.get("login"),
                    api_key_value=data.get("api_key"), name=data.get("name"),
                    **({"key_expires_at": data["key_expires_at"]} if "key_expires_at" in data else {}))
    except (odoo.OdooConnectionError, IntegrityError) as exc:
        db.rollback()
        raise _fail(exc if isinstance(exc, odoo.OdooConnectionError) else odoo.OdooConnectionError(
            "This Odoo database is already connected to another OdooBridger account.")) from exc
    acc.audit(db, customer.actor, "odoo.update", account_id=customer.account.id,
              detail=", ".join(k for k in data if k != "api_key") + (", api key" if data.get("api_key") else ""),
              request=request)
    _sync(db, customer)
    odoo.refresh_readiness(db, conn, live=True)
    db.commit()
    return _odoo_view(customer)


@router.post("/odoo/{conn_id}/test")
def test_odoo(conn_id: str, customer: Customer = Depends(get_customer), db: Session = Depends(get_db)) -> dict:
    conn = _connection(customer, conn_id)
    odoo.test(db, conn)
    _sync(db, customer)
    odoo.refresh_readiness(db, conn, live=True)
    db.commit()
    return _odoo_view(customer)


@router.delete("/odoo/{conn_id}")
def delete_odoo(conn_id: str, request: Request, customer: Customer = Depends(require_manager),
                db: Session = Depends(get_db)) -> dict:
    conn = _connection(customer, conn_id)
    detail = f"{conn.url} / {conn.db_name}"
    odoo.delete(db, conn)
    acc.audit(db, customer.actor, "odoo.disconnect", account_id=customer.account.id, detail=detail, request=request)
    _sync(db, customer)
    return _odoo_view(customer)

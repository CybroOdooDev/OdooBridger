"""What the website calls: the catalog, signup and the contact form."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.api.deps import stripe_config
from obcore.api.out import service_out
from obcore.db import get_db
from obcore.models import Service, ServicePackage, User
from obcore.services import accounts as acc
from obcore.services import billing, leads, provisioning
from obcore.config import settings

router = APIRouter(prefix="/public", tags=["public"], dependencies=[Depends(stripe_config)])


def public_services(db: Session) -> list[Service]:
    return list(db.scalars(select(Service).where(Service.is_active.is_(True), Service.is_public.is_(True))
                           .order_by(Service.sort_order, Service.name)).all())


@router.get("/catalog")
def catalog(db: Session = Depends(get_db)) -> dict:
    return {"services": [service_out(s) for s in public_services(db)],
            "default_trial_days": settings.default_trial_days}


class Choice(BaseModel):
    service: str
    package_id: str | None = None


class SignupIn(BaseModel):
    company: str = Field(min_length=2, max_length=160)
    full_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    country: str | None = Field(default=None, max_length=80)
    timezone: str | None = Field(default=None, max_length=64)
    services: list[Choice] = Field(default_factory=list, max_length=20)
    interval: str = "month"
    #: Honeypot: a real visitor never fills it.
    website: str | None = None


@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(payload: SignupIn, request: Request, db: Session = Depends(get_db)) -> dict:
    done = {"message": f"Check {payload.email} for a link to confirm it and set your password."}
    if payload.website:
        return done
    if not acc.rate_ok("signup", acc.client_ip(request), settings.registration_rate_per_hour):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many signups from here. Try again later.")
    services = {s.slug: s for s in public_services(db)}
    picks = []
    for choice in payload.services:
        service = services.get(choice.service)
        if service is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown service: {choice.service}")
        package = db.get(ServicePackage, choice.package_id) if choice.package_id else None
        if package is not None and (package.service_id != service.id or not package.is_active):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That package is not available.")
        picks.append((service, package))
    try:
        account, user = acc.create_account(db, company=payload.company, email=payload.email,
                                           full_name=payload.full_name, country=payload.country,
                                           timezone_name=payload.timezone)
        account.billing_interval = "year" if payload.interval == "year" else "month"
        for service, package in picks:
            billing.add_service(db, account, service, package)
    except (acc.AccountError, billing.SubscriptionError) as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    acc.audit(db, f"user:{user.email}", "account.signup", account_id=account.id, request=request,
              detail=", ".join(s.slug for s, _ in picks) or None)
    acc.send_welcome(user)
    db.commit()
    db.refresh(account)
    provisioning.push_account(db, account)
    db.commit()
    return done


@router.post("/resend")
def resend(payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    email = acc.normalise_email(str(payload.get("email") or ""))
    if acc.rate_ok("resend", acc.client_ip(request), 10):
        user = db.scalar(select(User).where(User.email == email))
        if user and user.is_active and user.email_verified_at is None:
            acc.send_welcome(user)
            db.commit()
    return {"message": "If that address is waiting for confirmation, a new link is on its way."}


class ContactIn(BaseModel):
    topic: str = Field(default="Demo", max_length=40)
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    company: str = Field(min_length=1, max_length=160)
    phone: str | None = Field(default=None, max_length=40)
    country: str | None = Field(default=None, max_length=80)
    services: list[str] = Field(default_factory=list, max_length=20)
    details: dict[str, str] = Field(default_factory=dict)
    message: str | None = Field(default=None, max_length=4000)
    preferred_date: str | None = Field(default=None, max_length=10)
    preferred_window: str | None = Field(default=None, max_length=20)
    timezone: str | None = Field(default=None, max_length=64)
    website: str | None = None


@router.post("/contact", status_code=status.HTTP_201_CREATED)
def contact(payload: ContactIn, request: Request, db: Session = Depends(get_db)) -> dict:
    thanks = {"message": "Thanks — we'll be in touch within one working day."}
    if payload.website:
        return thanks
    ip = acc.client_ip(request)
    if not acc.rate_ok("contact", ip, settings.contact_rate_per_hour):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests from here. Try again later.")
    known = {s.slug for s in public_services(db)}
    data = payload.model_dump(exclude={"website"})
    data["topic"] = "Demo" if payload.topic.lower().startswith("demo") else "Sales question"
    data["email"] = acc.normalise_email(payload.email)
    data["services"] = [s for s in payload.services if s in known]
    data["details"] = {k[:40]: str(v)[:200] for k, v in list(payload.details.items())[:20]}
    data["ip"] = ip
    lead = leads.create(db, data)
    db.commit()
    leads.notify_sales(db, lead)
    leads.acknowledge(lead)
    return thanks

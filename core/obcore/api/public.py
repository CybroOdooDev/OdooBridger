"""What the website calls: the catalog, signup and the contact form."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.api.deps import stripe_config
from obcore.api.out import service_out
from obcore.db import get_db
from obcore.models import PendingSignup, Service, ServicePackage, User
from obcore.services import accounts as acc
from obcore.services import billing, leads, provisioning, signups
from obcore.services import stripe_api as stripe
from obcore.config import settings

router = APIRouter(prefix="/public", tags=["public"], dependencies=[Depends(stripe_config)])


def public_services(db: Session) -> list[Service]:
    return list(db.scalars(select(Service).where(Service.is_active.is_(True), Service.is_public.is_(True))
                           .order_by(Service.sort_order, Service.name)).all())


@router.get("/site")
def site_info() -> dict:
    """The company details the contact page shows beside the form."""
    return {"company": settings.company_name, "email": settings.contact_email or settings.sales_email,
            "phone": settings.contact_phone, "address": settings.contact_address,
            "hours": settings.contact_hours, "website": settings.company_website}


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
    #: "trial" starts the free trial; "buy" goes on to Stripe checkout.
    mode: str = "trial"
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
    buying = payload.mode == "buy"
    interval = "year" if payload.interval == "year" else "month"
    if buying:
        if not picks:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a service to buy.")
        if not stripe.enabled():
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "Online payment is not available yet. Start a free trial instead, or contact us.")
        for service, package in picks:
            chosen = package or billing.default_package(service)
            if chosen is None or not chosen.price_id_for(interval):
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    f"{service.name} can't be bought {'yearly' if interval == 'year' else 'monthly'} online. "
                                    "Start a free trial, or contact us.")
    try:
        pending = signups.start(
            db, email=payload.email, company=payload.company, full_name=payload.full_name,
            country=payload.country, timezone_name=payload.timezone,
            services=[{"service": svc.slug, "package_id": pkg.id if pkg else None} for svc, pkg in picks],
            interval=interval, mode=payload.mode, ip=acc.client_ip(request))
        if buying and pending.paid_at is None:
            # Pay first: the confirmation link is emailed once Stripe has the payment.
            done = {"checkout_url": signups.checkout_url(db, pending),
                    "message": "Taking you to payment. Your confirmation link is emailed once it goes through."}
    except signups.SignupError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except stripe.BillingError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Payment could not be started. Please try again.") from exc
    db.commit()
    if buying and pending.paid_at is not None:
        done = {"message": f"You've already paid. We've sent a new link to {payload.email} to confirm it and set your password."}
    return done


@router.post("/signup/paid")
def signup_paid(payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    """Where Stripe sends the visitor back after paying. Checks the Checkout with Stripe
    (the webhook may be a moment behind) and emails the confirmation link."""
    session_id = str(payload.get("session_id") or "")
    if not session_id.startswith("cs_") or not acc.rate_ok("signup-paid", acc.client_ip(request), 30):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This payment could not be found.")
    try:
        session = stripe.request("GET", f"checkout/sessions/{session_id}")
    except stripe.BillingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not check the payment just now. Please try again shortly.") from exc
    pending = signups.mark_paid(db, session)
    if pending is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This payment has not gone through yet.")
    db.commit()
    return {"email": pending.email}


@router.post("/resend")
def resend(payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    email = acc.normalise_email(str(payload.get("email") or ""))
    if acc.rate_ok("resend", acc.client_ip(request), 10):
        user = db.scalar(select(User).where(User.email == email))
        pending = db.scalar(select(PendingSignup).where(PendingSignup.email == email))
        if user and user.is_active and user.email_verified_at is None:
            acc.send_welcome(user)
            db.commit()
        elif pending is not None:
            signups.send(pending)
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

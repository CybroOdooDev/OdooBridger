"""Sign-in at the two doors (customers, staff), sessions, email links."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.api.deps import Customer, get_customer, get_staff
from obcore.api.out import account_out, staff_out, user_out
from obcore.config import settings
from obcore.db import get_db
from obcore.models import PendingSignup, StaffUser, User, UserSession
from obcore.security import (
    SCOPE_CUSTOMER, SCOPE_STAFF, aware, create_token, decode_token, hash_password, hash_token,
    utcnow, validate_password, verify_password,
)
from obcore.services import accounts as acc
from obcore.services import billing, provisioning, signups, stripe_api

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class RefreshIn(BaseModel):
    refresh_token: str


class EmailIn(BaseModel):
    email: EmailStr


class LinkIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class SetPasswordIn(LinkIn):
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


def issue(db: Session, scope: str, subject_id: str, request: Request | None) -> dict:
    access, _, access_exp = create_token(subject_id, scope, "access")
    refresh, jti, refresh_exp = create_token(subject_id, scope, "refresh")
    db.add(UserSession(scope=scope, subject_id=subject_id, token_hash=hash_token(jti),
                       ip_address=acc.client_ip(request), expires_at=refresh_exp))
    db.commit()
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "scope": scope,
            "expires_in": int((access_exp - utcnow()).total_seconds())}


def _check(person, password: str) -> None:
    """Shared by both doors: lockout, password, counters. Raises 401/423."""
    locked = aware(person.locked_until) if person else None
    if locked and locked > utcnow():
        raise HTTPException(status.HTTP_423_LOCKED, "Too many attempts. Try again in a few minutes.")
    if person is None or not person.is_active or not verify_password(password, person.hashed_password):
        if person is not None:
            person.failed_login_count = (person.failed_login_count or 0) + 1
            if person.failed_login_count >= settings.max_failed_logins:
                person.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)
                person.failed_login_count = 0
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password.")
    person.failed_login_count = 0
    person.locked_until = None
    person.last_login_at = utcnow()


@router.post("/login")
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.email == acc.normalise_email(payload.email)))
    waiting = db.scalar(select(PendingSignup).where(PendingSignup.email == acc.normalise_email(payload.email)))
    if user is None and waiting is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Confirm your email first — open the link we sent you. Your account is created then."
                            if waiting.sent_at else
                            "Finish paying to create your account — your confirmation link is sent once the payment goes through.")
    if user is not None and user.is_active and not user.hashed_password:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Confirm your email first — open the link we sent you to choose your password.")
    try:
        _check(user, payload.password)
    finally:
        db.commit()
    return issue(db, SCOPE_CUSTOMER, user.id, request)


@router.post("/staff/login")
def staff_login(payload: LoginIn, request: Request, db: Session = Depends(get_db)) -> dict:
    staff = db.scalar(select(StaffUser).where(StaffUser.email == acc.normalise_email(payload.email)))
    try:
        _check(staff, payload.password)
    finally:
        db.commit()
    return issue(db, SCOPE_STAFF, staff.id, request)


@router.post("/refresh")
def refresh(payload: RefreshIn, request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        claims = decode_token(payload.refresh_token, "refresh")
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    row = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(claims["jti"])))
    if row is None or row.revoked_at is not None or aware(row.expires_at) < utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session ended. Sign in again.")
    model = StaffUser if claims["scp"] == SCOPE_STAFF else User
    person = db.get(model, claims["sub"])
    if person is None or not person.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session ended. Sign in again.")
    row.revoked_at = utcnow()   # rotate: one refresh token, one use
    return issue(db, claims["scp"], person.id, request)


@router.post("/logout")
def logout(payload: RefreshIn, db: Session = Depends(get_db)) -> dict:
    try:
        claims = decode_token(payload.refresh_token, "refresh")
    except ValueError:
        return {"message": "Signed out."}
    row = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(claims["jti"])))
    if row and row.revoked_at is None:
        row.revoked_at = utcnow()
        db.commit()
    return {"message": "Signed out."}


@router.get("/me")
def me(customer: Customer = Depends(get_customer)) -> dict:
    return {"user": user_out(customer.user), "account": account_out(customer.account)}


@router.get("/staff/me")
def staff_me(staff: StaffUser = Depends(get_staff)) -> dict:
    return staff_out(staff)


@router.post("/forgot-password")
def forgot_password(payload: EmailIn, request: Request, db: Session = Depends(get_db)) -> dict:
    message = {"message": "If that email has an account, a reset link is on its way."}
    if not acc.rate_ok("forgot", acc.client_ip(request), 10):
        return message
    user = db.scalar(select(User).where(User.email == acc.normalise_email(payload.email)))
    if user and user.is_active:
        sent = aware(user.link_sent_at)
        if sent is None or (utcnow() - sent).total_seconds() > 60:
            if user.hashed_password:
                acc.send_reset(user)
            else:
                acc.send_welcome(user)   # never set a password: the set-password link again
            db.commit()
    return message


@router.post("/link/check")
def check_link(payload: LinkIn, db: Session = Depends(get_db)) -> dict:
    user = acc.user_for_link(db, payload.token)
    if user is None:
        pending = signups.for_link(db, payload.token)
        if pending is not None:
            return {"email": pending.email, "account": pending.company, "purpose": acc.PURPOSE_VERIFY,
                    "signup": True, "mode": pending.mode}
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "This link has expired or was already used. Ask for a new one.")
    return {"email": user.email, "account": user.account.name, "purpose": user.link_token_purpose}


@router.post("/link/use")
def use_link(payload: SetPasswordIn, request: Request, db: Session = Depends(get_db)) -> dict:
    user = acc.user_for_link(db, payload.token)
    if user is None:
        pending = signups.for_link(db, payload.token)
        if pending is not None:
            return _confirm_signup(db, pending, payload.password, request)
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "This link has expired or was already used. Ask for a new one.")
    problem = validate_password(payload.password)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    first_time = user.email_verified_at is None
    acc.use_link(user, payload.password)
    if not first_time:
        # A reset ends every other session.
        for row in db.scalars(select(UserSession).where(
                UserSession.subject_id == user.id, UserSession.revoked_at.is_(None))):
            row.revoked_at = utcnow()
    else:
        acc.audit(db, f"user:{user.email}", "user.verified", account_id=user.account_id, request=request)
    user.last_login_at = utcnow()
    db.commit()
    return issue(db, SCOPE_CUSTOMER, user.id, request)


@router.post("/change-password")
def change_password(payload: ChangePasswordIn, customer: Customer = Depends(get_customer),
                    db: Session = Depends(get_db)) -> dict:
    if not verify_password(payload.current_password, customer.user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The current password is not right.")
    problem = validate_password(payload.new_password)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    customer.user.hashed_password = hash_password(payload.new_password)
    db.commit()
    return {"message": "Password changed."}


def _confirm_signup(db: Session, pending, password: str, request: Request) -> dict:
    """The signup's email is confirmed: only now is the account created."""
    problem = validate_password(password)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    stripe_api.load_config(db)
    try:
        account, user = signups.complete(db, pending, password)
    except signups.SignupError as exc:
        db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except stripe_api.BillingError as exc:
        # The link isn't used up: nothing was created, so they can try again.
        db.rollback()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Could not reach payments just now. Please try this link again in a minute.") from exc
    acc.audit(db, f"user:{user.email}", "account.signup", account_id=account.id, request=request,
              detail=", ".join(r.service.slug for r in account.services) or None)
    db.commit()
    db.refresh(account)
    provisioning.push_account(db, account)
    db.commit()
    out = issue(db, SCOPE_CUSTOMER, user.id, request)
    return out

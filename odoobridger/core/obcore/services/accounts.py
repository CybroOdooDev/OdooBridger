"""Accounts and their people: signup, email links, passwords, audit."""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.config import settings
from obcore.mailer import try_send
from obcore.models import Account, AuditLog, User, UserRole
from obcore.security import aware, hash_password, hash_token, new_token, utcnow

PURPOSE_VERIFY = "verify"     # also used for invitations: confirms the address and sets a password
PURPOSE_RESET = "reset"


class AccountError(ValueError):
    """A refusal whose message is safe to show the customer."""


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "account"


def unique_slug(db: Session, name: str) -> str:
    base = slugify(name)
    slug, n = base, 1
    while db.scalar(select(Account.id).where(Account.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def normalise_email(email: str) -> str:
    return email.strip().lower()


def audit(db: Session, actor: str, action: str, *, account_id: str | None = None,
          detail: str | None = None, request: Request | None = None) -> None:
    db.add(AuditLog(account_id=account_id, actor=actor, action=action, detail=detail,
                    ip_address=client_ip(request)))


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


# --- per-IP rate limits (in-process) -----------------------------------------
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def rate_ok(bucket: str, ip: str | None, per_hour: int) -> bool:
    if per_hour <= 0:
        return True
    now = time.monotonic()
    with _lock:
        q = _hits[f"{bucket}:{ip or '?'}"]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= per_hour:
            return False
        q.append(now)
        return True


def reset_rate_limits() -> None:
    with _lock:
        _hits.clear()


# --- creating accounts ---------------------------------------------------------
def create_account(db: Session, *, company: str, email: str, full_name: str | None,
                   country: str | None = None, timezone_name: str | None = None,
                   role: str = UserRole.owner.value) -> tuple[Account, User]:
    email = normalise_email(email)
    if db.scalar(select(User.id).where(User.email == email)):
        raise AccountError("An account with this email already exists. Sign in instead.")
    account = Account(name=company.strip(), slug=unique_slug(db, company), country=country,
                      timezone=timezone_name or "UTC")
    db.add(account)
    db.flush()
    user = User(account_id=account.id, email=email, full_name=(full_name or "").strip() or None, role=role)
    db.add(user)
    db.flush()
    return account, user


def add_user(db: Session, account: Account, *, email: str, full_name: str | None, role: str) -> User:
    email = normalise_email(email)
    if role not in (UserRole.admin.value, UserRole.member.value, UserRole.owner.value):
        raise AccountError("Unknown role.")
    if db.scalar(select(User.id).where(User.email == email)):
        raise AccountError("Someone with this email already has an OdooBridger login.")
    user = User(account_id=account.id, email=email, full_name=full_name or None, role=role)
    db.add(user)
    db.flush()
    return user


# --- email links -----------------------------------------------------------------
def issue_link(user: User, purpose: str) -> str:
    raw = new_token()
    user.link_token_hash = hash_token(raw)
    user.link_token_purpose = purpose
    ttl = (timedelta(hours=settings.email_verification_ttl_hours) if purpose == PURPOSE_VERIFY
           else timedelta(minutes=settings.password_reset_ttl_minutes))
    user.link_token_expires_at = utcnow() + ttl
    user.link_sent_at = utcnow()
    return raw


def link_url(raw: str, purpose: str) -> str:
    page = "set-password" if purpose == PURPOSE_VERIFY else "reset-password"
    return f"{settings.base}/app/#/{page}?token={raw}"


def send_welcome(user: User, *, invited_by: str | None = None) -> None:
    raw = issue_link(user, PURPOSE_VERIFY)
    first = (user.full_name or user.email).split()[0]
    intro = (f"{invited_by} has invited you to {user.account.name} on OdooBridger."
             if invited_by else f"Thanks for signing up {user.account.name} for OdooBridger.")
    body = (f"Hi {first},\n\n{intro}\n\nConfirm your email and choose your password here:\n"
            f"{link_url(raw, PURPOSE_VERIFY)}\n\nThe link works once and expires in "
            f"{settings.email_verification_ttl_hours} hours. Your login is this email address "
            f"and the password you choose.\n\n— OdooBridger\n")
    try_send(user.email, "Confirm your email and set your OdooBridger password", body)


def send_reset(user: User) -> None:
    raw = issue_link(user, PURPOSE_RESET)
    body = (f"Someone asked to reset the OdooBridger password for {user.email}.\n\n"
            f"Choose a new one here: {link_url(raw, PURPOSE_RESET)}\n\n"
            f"The link expires in {settings.password_reset_ttl_minutes} minutes. "
            f"If it wasn't you, ignore this email.\n")
    try_send(user.email, "Reset your OdooBridger password", body)


def user_for_link(db: Session, raw: str) -> User | None:
    if not raw:
        return None
    user = db.scalar(select(User).where(User.link_token_hash == hash_token(raw)))
    if user is None or not user.is_active:
        return None
    expires = aware(user.link_token_expires_at)
    if expires is None or expires < utcnow():
        return None
    return user


def use_link(user: User, password: str) -> None:
    """Set the password from a verify/reset link; the link is spent."""
    user.hashed_password = hash_password(password)
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
    user.link_token_hash = None
    user.link_token_purpose = None
    user.link_token_expires_at = None
    user.failed_login_count = 0
    user.locked_until = None


def owner_of(account: Account) -> User | None:
    owners = [u for u in account.users if u.role == UserRole.owner.value and u.is_active]
    return owners[0] if owners else (account.users[0] if account.users else None)

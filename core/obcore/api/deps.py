"""Who is calling: a customer (user + account) or staff. Two doors, two tables."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from obcore.db import get_db
from obcore.models import Account, StaffUser, User, UserRole
from obcore.security import SCOPE_CUSTOMER, SCOPE_STAFF, decode_token
from obcore.services import stripe_api

bearer = HTTPBearer(auto_error=False)


@dataclass
class Customer:
    user: User
    account: Account

    @property
    def actor(self) -> str:
        return f"user:{self.user.email}"


def _payload(credentials: HTTPAuthorizationCredentials | None) -> dict:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in first.")
    try:
        return decode_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


def get_customer(credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
                 db: Session = Depends(get_db)) -> Customer:
    payload = _payload(credentials)
    if payload["scp"] != SCOPE_CUSTOMER:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "This is a staff session. Customer pages need a customer sign-in.")
    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or disabled.")
    return Customer(user=user, account=user.account)


def require_manager(customer: Customer = Depends(get_customer)) -> Customer:
    if not customer.user.can_manage:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the account's owner or admins can do this.")
    return customer


def require_owner(customer: Customer = Depends(get_customer)) -> Customer:
    if customer.user.role != UserRole.owner.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the account owner can do this.")
    return customer


def get_staff(credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
              db: Session = Depends(get_db)) -> StaffUser:
    payload = _payload(credentials)
    if payload["scp"] != SCOPE_STAFF:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The staff console needs a staff sign-in.")
    staff = db.get(StaffUser, payload["sub"])
    if staff is None or not staff.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Staff user not found or disabled.")
    return staff


def stripe_config(db: Session = Depends(get_db)) -> None:
    """Load the Stripe keys in force (console, else environment) for this request."""
    stripe_api.load_config(db)

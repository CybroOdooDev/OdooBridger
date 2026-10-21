"""Passwords, the core's own session tokens, and secret encryption.

Core sessions are HS256 and never leave the core. Tokens for services are a
different thing — RS256, signed with the key in ``obcore.keys`` — so a service
holding the public key can verify but never mint one.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from jose import JWTError, jwt
from passlib.context import CryptContext

from obcore.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SCOPE_CUSTOMER = "customer"
SCOPE_STAFF = "staff"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return pwd_context.verify(plain, hashed)
    except ValueError:
        return False


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def validate_password(password: str) -> str | None:
    """A reason the password is not acceptable, or None."""
    if len(password) < 10:
        return "Use at least 10 characters."
    if password.lower() == password or password.upper() == password or not any(c.isdigit() for c in password):
        return "Mix upper and lower case letters and at least one number."
    return None


# --- core sessions -------------------------------------------------------------
def _ttl(scope: str, kind: str) -> timedelta:
    if kind == "access":
        minutes = (settings.staff_access_token_ttl_minutes if scope == SCOPE_STAFF
                   else settings.access_token_ttl_minutes)
        return timedelta(minutes=minutes)
    days = settings.staff_refresh_token_ttl_days if scope == SCOPE_STAFF else settings.refresh_token_ttl_days
    return timedelta(days=days)


def create_token(subject: str, scope: str, kind: str, extra: dict[str, Any] | None = None) -> tuple[str, str, datetime]:
    """(token, jti, expires_at)."""
    now = utcnow()
    expires = now + _ttl(scope, kind)
    jti = uuid.uuid4().hex
    payload = {"sub": subject, "scp": scope, "typ": kind, "iat": int(now.timestamp()),
               "exp": int(expires.timestamp()), "jti": jti, **(extra or {})}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256"), jti, expires


def decode_token(token: str, kind: str = "access") -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise ValueError("Invalid or expired token") from exc
    if payload.get("typ") != kind:
        raise ValueError("Wrong token type")
    if payload.get("scp") not in (SCOPE_CUSTOMER, SCOPE_STAFF):
        raise ValueError("Unknown token scope")
    return payload


# --- secrets at rest -----------------------------------------------------------
def _fernet(purpose: str) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"odoobridger.v1",
               info=purpose.encode()).derive(settings.master_encryption_key.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str, purpose: str = "setting") -> str:
    return "v1:" + _fernet(purpose).encrypt(value.encode()).decode()


def decrypt(value: str, purpose: str = "setting") -> str:
    if not value.startswith("v1:"):
        raise ValueError("Unknown ciphertext version")
    try:
        return _fernet(purpose).decrypt(value[3:].encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Cannot decrypt (wrong MASTER_ENCRYPTION_KEY?)") from exc

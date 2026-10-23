"""The core's RS256 signing key, its JWKS, and tokens for services.

Two kinds of token go to services, told apart by ``typ``:

* ``identity`` — who just signed in through SSO: the user, their account,
  their role and the account's status and entitlements for that service.
  Audience = the service slug.
* ``provision`` — the Authorization header on the core's server-to-server
  calls (create/update/delete an account on the service). Audience = the
  service slug, ``sub`` = "odoobridger-core".

Services verify both with the public key (``GET /.well-known/jwks.json``, or
the PEM pasted into their settings) and never need a shared secret for it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import uuid
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from sqlalchemy.orm import Session

from obcore.config import settings
from obcore.security import decrypt, encrypt, utcnow

log = logging.getLogger(__name__)
_SETTING_KEY = "signing_key"
_lock = threading.Lock()
_cached: tuple[str, str] | None = None   # (private pem, kid)


def _kid(private_pem: str) -> str:
    public = public_pem_for(private_pem)
    return hashlib.sha256(public.encode()).hexdigest()[:16]


def public_pem_for(private_pem: str) -> str:
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def generate_private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def load(db: Session | None = None) -> tuple[str, str]:
    """(private pem, kid): SIGNING_KEY_PEM, else the one kept in the database
    (generated on first use)."""
    global _cached
    if _cached:
        return _cached
    with _lock:
        if _cached:
            return _cached
        pem = settings.signing_key_pem.strip().replace("\\n", "\n")
        if not pem:
            from obcore.db import session_scope
            from obcore.models import Setting

            def _from(session: Session) -> str:
                row = session.get(Setting, _SETTING_KEY)
                if row is None:
                    fresh = generate_private_pem()
                    session.add(Setting(key=_SETTING_KEY, value=encrypt(fresh, "signing-key"),
                                        updated_by="system"))
                    session.flush()
                    log.info("Generated the core's service-token signing key")
                    return fresh
                return decrypt(row.value, "signing-key")

            if db is not None:
                pem = _from(db)
            else:
                with session_scope() as session:
                    pem = _from(session)
        _cached = (pem, _kid(pem))
        return _cached


def reset_cache() -> None:
    global _cached
    _cached = None


def public_pem(db: Session | None = None) -> str:
    return public_pem_for(load(db)[0])


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwks(db: Session | None = None) -> dict[str, Any]:
    private, kid = load(db)
    numbers = serialization.load_pem_private_key(private.encode(), password=None).public_key().public_numbers()
    return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
                      "n": _b64(numbers.n), "e": _b64(numbers.e)}]}


def sign_service_token(audience: str, typ: str, claims: dict[str, Any], *,
                       ttl_seconds: int | None = None, db: Session | None = None) -> str:
    private, kid = load(db)
    now = utcnow()
    payload = {
        "iss": settings.base,
        "aud": audience,
        "typ": typ,
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + (ttl_seconds or settings.service_token_ttl_seconds),
        "jti": uuid.uuid4().hex,
        **claims,
    }
    return jwt.encode(payload, private, algorithm="RS256", headers={"kid": kid})


def body_digest(body: bytes) -> str:
    """Bound into provisioning tokens, so a captured token cannot carry another body."""
    return hashlib.sha256(body).hexdigest()


def dumps(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()

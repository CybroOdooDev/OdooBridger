"""A thin Stripe client (httpx, form-encoded) and webhook signature checks.

Keys come from the staff console (Payments → Stripe keys, stored encrypted)
when set there, otherwise from STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from obcore.config import settings
from obcore.models import Setting
from obcore.security import decrypt, encrypt

SETTING_KEY = "stripe"
SIGNATURE_TOLERANCE_SECONDS = 300
_console: tuple[str, str] | None = None


class BillingError(Exception):
    """Stripe refused or could not be reached. The message is safe to show."""


class SignatureError(BillingError):
    pass


# --- which keys --------------------------------------------------------------
def load_config(db: Session) -> None:
    global _console
    row = db.get(Setting, SETTING_KEY)
    if row is None:
        _console = None
        return
    data = json.loads(row.value or "{}")
    try:
        secret = decrypt(data["secret_key"], "stripe") if data.get("secret_key") else ""
        hook = decrypt(data["webhook_secret"], "stripe") if data.get("webhook_secret") else ""
    except ValueError:
        secret, hook = "", ""
    _console = (secret, hook) if secret else None


def save_config(db: Session, secret_key: str | None, webhook_secret: str | None, by: str) -> None:
    row = db.get(Setting, SETTING_KEY)
    data = json.loads(row.value) if row else {}
    if secret_key is not None:
        data["secret_key"] = encrypt(secret_key, "stripe") if secret_key else ""
    if webhook_secret is not None:
        data["webhook_secret"] = encrypt(webhook_secret, "stripe") if webhook_secret else ""
    if row is None:
        row = Setting(key=SETTING_KEY, value="{}")
        db.add(row)
    row.value = json.dumps(data)
    row.updated_by = by
    db.flush()
    load_config(db)


def config_summary(db: Session) -> dict[str, Any]:
    load_config(db)
    key = secret_key()
    return {
        "enabled": bool(key),
        "source": "console" if _console else ("environment" if settings.stripe_secret_key else None),
        "mode": "live" if key.startswith(("sk_live", "rk_live")) else ("test" if key else None),
        "secret_key_hint": f"…{key[-4:]}" if key else None,
        "webhook_secret_set": bool(webhook_secret()),
        "webhook_url": f"{settings.base}/api/v1/billing/webhook",
    }


def secret_key() -> str:
    return _console[0] if _console else settings.stripe_secret_key


def webhook_secret() -> str:
    return _console[1] if _console and _console[1] else settings.stripe_webhook_secret


def enabled() -> bool:
    return bool(secret_key())


# --- requests ------------------------------------------------------------------
def _flatten(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else key
        if value is None:
            continue
        if isinstance(value, dict):
            out.extend(_flatten(value, name))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out.extend(_flatten(item, f"{name}[{i}]"))
                else:
                    out.append((f"{name}[{i}]", str(item)))
        elif isinstance(value, bool):
            out.append((name, "true" if value else "false"))
        else:
            out.append((name, str(value)))
    return out


def request(method: str, path: str, data: dict[str, Any] | None = None,
            idempotency_key: str | None = None) -> dict[str, Any]:
    if not enabled():
        raise BillingError("Online payments are not set up yet.")
    headers = {"Authorization": f"Bearer {secret_key()}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    body = None
    if data and method != "GET":
        body = urlencode(_flatten(data)).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        response = httpx.request(
            method, f"{settings.stripe_api_base.rstrip('/')}/v1/{path.lstrip('/')}",
            content=body, params=_flatten(data) if data and method == "GET" else None,
            headers=headers, timeout=settings.http_timeout_seconds)
    except httpx.RequestError as exc:
        raise BillingError(f"Could not reach Stripe: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        message = (payload.get("error") or {}).get("message") or f"HTTP {response.status_code}"
        raise BillingError(f"Stripe: {message}")
    return payload


# --- webhooks --------------------------------------------------------------------
def verify_signature(payload: bytes, header: str | None, *, now: float | None = None) -> None:
    secret = webhook_secret()
    if not secret:
        raise SignatureError("Webhook signing secret is not configured.")
    if not header:
        raise SignatureError("Missing Stripe-Signature header.")
    parts: dict[str, list[str]] = {}
    for piece in header.split(","):
        key, _, value = piece.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError, IndexError) as exc:
        raise SignatureError("Malformed Stripe-Signature header.") from exc
    if abs((now or time.time()) - timestamp) > SIGNATURE_TOLERANCE_SECONDS:
        raise SignatureError("Webhook timestamp is outside the tolerance window.")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise SignatureError("Webhook signature does not match.")


def sign_for_tests(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = timestamp or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"

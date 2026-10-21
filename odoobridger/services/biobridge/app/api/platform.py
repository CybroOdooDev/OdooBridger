"""Endpoints the OdooBridger core calls (platform mode only).

``/platform/v1/accounts/{account_id}`` — PUT the account's state, DELETE the
account and everything BioBridge holds for it, GET ``/usage`` for the staff
console. Every call carries a core-signed RS256 token whose ``account_id``
and ``body_sha256`` must match this exact request.
"""

from __future__ import annotations

import hashlib
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.services import platform_link

log = logging.getLogger(__name__)
router = APIRouter(prefix="/platform/v1", tags=["platform"])


async def _verified(request: Request, account_id: str) -> bytes:
    if not settings.platform_mode:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Platform mode is off on this BioBridge.")
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token.")
    body = await request.body()
    try:
        claims = platform_link.verify(header.split(None, 1)[1], "provision")
    except platform_link.PlatformError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    if claims.get("account_id") != account_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The token is for another account.")
    if claims.get("body_sha256") != hashlib.sha256(body).hexdigest():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The token does not match this request.")
    return body


@router.put("/accounts/{account_id}")
async def put_account(account_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    body = await _verified(request, account_id)
    state = json.loads(body or b"{}")
    if (state.get("account") or {}).get("id") != account_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Account id mismatch.")
    try:
        tenant = platform_link.apply_state(db, state)
    except platform_link.PlatformError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return {"tenant_id": tenant.id, "slug": tenant.slug, "status": tenant.status}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    await _verified(request, account_id)
    tenant = platform_link.tenant_for(db, account_id)
    if tenant is None:
        return {"deleted": False, "message": "Nothing held for this account."}
    from app.services.account_deletion import delete_tenant

    delete_tenant(db, tenant, closed_by="platform", closed_by_email=None, reason_code=None,
                  reason_text="Deleted in OdooBridger")
    return {"deleted": True}


@router.get("/accounts/{account_id}/usage")
async def account_usage(account_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    await _verified(request, account_id)
    tenant = platform_link.tenant_for(db, account_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "BioBridge has no tenant for this account yet.")
    return platform_link.usage(db, tenant)

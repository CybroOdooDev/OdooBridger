"""SSO endpoints (see obcore.services.sso) and the public key."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from obcore import keys
from obcore.api.deps import Customer, get_customer
from obcore.db import get_db
from obcore.services import sso

router = APIRouter(prefix="/sso", tags=["sso"])


class AuthorizeIn(BaseModel):
    service: str = Field(max_length=40)
    redirect_uri: str | None = Field(default=None, max_length=500)
    state: str | None = Field(default=None, max_length=200)


class TokenIn(BaseModel):
    client_id: str
    client_secret: str
    code: str


@router.post("/authorize")
def authorize(payload: AuthorizeIn, customer: Customer = Depends(get_customer),
              db: Session = Depends(get_db)) -> dict:
    try:
        url = sso.authorize(db, customer.user, payload.service, payload.redirect_uri, payload.state)
    except sso.SsoError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    return {"redirect_url": url}


@router.post("/token")
def token(payload: TokenIn, db: Session = Depends(get_db)) -> dict:
    """Server to server: a service trades a one-time code for an identity token."""
    try:
        id_token = sso.exchange(db, payload.client_id, payload.client_secret, payload.code)
    except sso.SsoError as exc:
        db.commit()   # a code is spent even when the exchange fails later
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    return {"id_token": id_token, "token_type": "identity"}


@router.get("/public-key", response_class=PlainTextResponse)
def public_key(db: Session = Depends(get_db)) -> str:
    return keys.public_pem(db)

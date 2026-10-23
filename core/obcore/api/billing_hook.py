"""Stripe webhook endpoint."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from obcore.api.deps import stripe_config
from obcore.db import get_db
from obcore.services import billing, provisioning
from obcore.services import stripe_api as stripe

log = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"], dependencies=[Depends(stripe_config)])


@router.post("/webhook", include_in_schema=False)
async def webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    body = await request.body()
    try:
        stripe.verify_signature(body, request.headers.get("stripe-signature"))
    except stripe.SignatureError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    event = json.loads(body or b"{}")
    try:
        note = billing.handle_event(db, event)
    except stripe.BillingError as exc:
        db.rollback()
        # 5xx so Stripe retries later.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    db.commit()
    # Tell services about anything that changed.
    provisioning.push_pending(db)
    db.commit()
    log.info("Stripe %s %s: %s", event.get("type"), event.get("id"), note)
    return {"received": True, "result": note}

"""Reports on this account's own data. See app/services/reports.py."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_principal
from app.db.session import get_db
from app.services import reports
from app.services.timeutils import utc_to_local, utcnow_naive

router = APIRouter(prefix="/reports", tags=["reports"])


def _today(principal: Principal) -> date:
    return utc_to_local(utcnow_naive(), principal.tenant.timezone or "UTC").date()


def _run(fn: Callable[..., dict[str, Any]], principal: Principal, db: Session,
         date_from: date | None, date_to: date | None, **extra: Any) -> dict[str, Any]:
    to = date_to or _today(principal)
    start = date_from or to - timedelta(days=6)
    try:
        return fn(db, principal.tenant, start, to, **extra)
    except reports.ReportError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/exceptions")
def exceptions(date_from: date | None = None, date_to: date | None = None,
               principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    return _run(reports.exceptions, principal, db, date_from, date_to)


@router.get("/terminals")
def terminals(date_from: date | None = None, date_to: date | None = None,
              silent_hours: int = Query(default=24, ge=1, le=720),
              principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    return _run(reports.terminals, principal, db, date_from, date_to, silent_hours=silent_hours)


@router.get("/daily")
def daily(date_from: date | None = None, date_to: date | None = None,
          principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    return _run(reports.daily, principal, db, date_from, date_to)


@router.get("/sync")
def sync_history(date_from: date | None = None, date_to: date | None = None,
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    return _run(reports.sync_history, principal, db, date_from, date_to)

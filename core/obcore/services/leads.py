"""Leads: website enquiries and demo requests, and the sales pipeline.

Stages New → Contacted → Qualified → Won / Lost. Demo is a sub-stage of
Contacted (pending → scheduled → completed), not a stage of its own. Every
move and note is a LeadEvent, so the history can be trusted.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.config import settings
from obcore.mailer import try_send
from obcore.models import DEMO_STATUSES, LEAD_STAGES, Lead, LeadEvent, Service, StaffUser
from obcore.security import utcnow

log = logging.getLogger(__name__)


class LeadError(ValueError):
    pass


def record(db: Session, lead: Lead, kind: str, actor: str | None, *, from_value: str | None = None,
           to_value: str | None = None, note: str | None = None) -> None:
    db.add(LeadEvent(lead_id=lead.id, kind=kind, from_value=from_value, to_value=to_value,
                     note=note, actor=actor))


def create(db: Session, data: dict, *, source: str = "website", actor: str | None = None) -> Lead:
    lead = Lead(**data, source=source, stage="new", stage_changed_at=utcnow())
    if lead.topic == "Demo":
        lead.demo_status = "pending"
    db.add(lead)
    db.flush()
    record(db, lead, "stage", actor or ("website" if source == "website" else None), to_value="new")
    return lead


def move(db: Session, lead: Lead, stage: str, actor: str, *, lost_reason: str | None = None,
         converting: bool = False) -> None:
    if stage not in LEAD_STAGES:
        raise LeadError("Unknown stage.")
    if stage == lead.stage:
        if stage == "lost" and lost_reason and lost_reason != lead.lost_reason:
            # Same stage, a corrected reason: keep the history of what it said before.
            record(db, lead, "note", actor, note=f"Lost reason changed to: {lost_reason}")
            lead.lost_reason = lost_reason
        return
    if lead.stage == "won":
        raise LeadError("This lead is won and closed. It can't be moved to another stage.")
    if stage == "won" and not converting:
        raise LeadError("A lead becomes Won when it's converted to an account. Qualify it, then convert it.")
    record(db, lead, "stage", actor, from_value=lead.stage, to_value=stage,
           note=lost_reason if stage == "lost" else None)
    lead.stage = stage
    lead.stage_changed_at = utcnow()
    if stage in ("won", "lost"):
        lead.follow_up_on = None   # nothing left to follow up on
    if stage == "lost":
        lead.lost_reason = lost_reason or lead.lost_reason
    elif lead.lost_reason:
        lead.lost_reason = None
    if stage == "new":
        # Back to the start: a demo is only meaningful once contacted.
        lead.demo_status = "pending" if lead.topic == "Demo" else None


def set_demo(db: Session, lead: Lead, status: str | None, actor: str, demo_at=None) -> None:
    if lead.stage == "won":
        raise LeadError("This lead is won and closed; its demo can't be changed.")
    if status is not None and status not in DEMO_STATUSES:
        raise LeadError("Unknown demo status.")
    if status and lead.stage not in ("contacted", "qualified", "won"):
        # A demo happens once the lead has been contacted.
        move(db, lead, "contacted", actor)
    if status != lead.demo_status:
        record(db, lead, "demo", actor, from_value=lead.demo_status, to_value=status)
        lead.demo_status = status
    if demo_at is not None:
        lead.demo_at = demo_at


def service_names(db: Session, slugs: list[str]) -> str:
    if not slugs:
        return ""
    names = db.scalars(select(Service.name).where(Service.slug.in_(slugs))).all()
    return ", ".join(names)


def notify_sales(db: Session, lead: Lead) -> None:
    to = [e.strip() for e in settings.sales_email.split(",") if e.strip()] or list(
        db.scalars(select(StaffUser.email).where(StaffUser.is_active.is_(True))).all())
    if not to:
        return
    when = (f"{lead.preferred_date} · {lead.preferred_window or 'any time'} ({lead.timezone or 'timezone unknown'})"
            if lead.preferred_date else None)
    lines = [f"{lead.name} <{lead.email}> at {lead.company} sent a {lead.topic.lower()} request."]
    for label, value in (("Services", service_names(db, lead.services or [])), ("Preferred time", when),
                         ("Phone", lead.phone), ("Country", lead.country)):
        if value:
            lines.append(f"{label}: {value}")
    for key, value in (lead.details or {}).items():
        if value:
            lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    if lead.message:
        lines.append(f"\nMessage:\n{lead.message}")
    lines.append(f"\nIt is in the staff console under Leads: {settings.base}/staff/#/leads/{lead.id}")
    for address in to:
        try_send(address, f"[OdooBridger] {lead.topic}: {lead.name}, {lead.company}", "\n".join(lines))


def acknowledge(lead: Lead) -> None:
    first = lead.name.split()[0] if lead.name.strip() else "there"
    body = (f"Hi {first},\n\nThanks for getting in touch with OdooBridger — we've got your "
            f"{lead.topic.lower()} request and will reply within one working day.\n\n"
            f"If anything changes, just reply to this email.\n\n— The OdooBridger team\n")
    try_send(lead.email, "We've got your OdooBridger request", body)


# =============================================================================
# Trends for the Leads page
# =============================================================================
def trends(db: Session, days: int = 30, now=None) -> dict:
    """The four Leads figures with their comparison, built from the history, not from snapshots.

    Open leads and pipeline value are *as of* a moment, so "then" is rebuilt from each lead's stage
    moves (a lead that came in after that moment didn't exist yet). Won is a count over a window.
    Conversion is a cohort: of the leads that came in during the window, how many are won. A lead's
    amount is today's, since amounts aren't versioned. ``previous`` is None when there is nothing to
    compare with (no leads existed then).
    """
    from datetime import timedelta

    from obcore.models import OPEN_STAGES
    from obcore.security import aware

    now = now or utcnow()
    t1, t2 = now - timedelta(days=days), now - timedelta(days=2 * days)
    all_leads = db.scalars(select(Lead)).all()
    moves: dict[str, list] = {}
    for e in db.scalars(select(LeadEvent).where(LeadEvent.kind == "stage").order_by(LeadEvent.created_at)).all():
        moves.setdefault(e.lead_id, []).append(e)

    def stage_at(lead: Lead, when):
        if aware(lead.created_at) > when:
            return None
        stage = "new"
        for e in moves.get(lead.id, []):
            if aware(e.created_at) <= when:
                stage = e.to_value
            else:
                break
        return stage

    def open_at(when) -> tuple[int, int]:
        picked = [lead for lead in all_leads if stage_at(lead, when) in OPEN_STAGES]
        return len(picked), sum(lead.value_cents or 0 for lead in picked)

    def won_between(a, b) -> int:
        return len({e.lead_id for lead_id, evs in moves.items() for e in evs
                    if e.to_value == "won" and a < aware(e.created_at) <= b})

    def cohort(a, b) -> dict:
        made = [lead for lead in all_leads if a < aware(lead.created_at) <= b]
        won = sum(1 for lead in made if lead.stage == "won")
        return {"total": len(made), "won": won, "rate": round(100 * won / len(made), 1) if made else None}

    open_now, pipe_now = open_at(now)
    open_then, pipe_then = open_at(t1)
    existed_then = any(aware(lead.created_at) <= t1 for lead in all_leads)
    cur, prev = cohort(t1, now), cohort(t2, t1)
    return {
        "days": days,
        "open_leads": {"value": open_now, "previous": open_then if existed_then else None},
        "pipeline_cents": {"value": pipe_now, "previous": pipe_then if existed_then else None},
        "won": {"value": won_between(t1, now), "previous": won_between(t2, t1),
                "all_time": sum(1 for lead in all_leads if lead.stage == "won")},
        "conversion": {"value": cur["rate"], "previous": prev["rate"], "won": cur["won"], "total": cur["total"]},
    }


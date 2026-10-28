"""Alerting from Needs Attention.

Every minute the core works out what needs attention (the same list the staff
overview shows) and compares it with what it saw before, kept in
``alert_state`` so a restart neither repeats nor forgets an alert:

* a problem at or above the chosen severity that has lasted longer than the
  grace period is announced once;
* while it lasts, a reminder goes out every ``reminder_hours`` (0 = never);
* when an announced problem clears, that is announced too.

Everything found in one check goes out as one message per channel: an email
to the chosen staff, and optionally a Slack-compatible webhook
(``{"text": ...}``, which Slack, Mattermost, Google Chat and Teams workflows
all accept). Settings live in the ``alerts`` setting, edited in the console.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.config import settings
from obcore.mailer import try_send
from obcore.models import AlertState, Setting, StaffUser
from obcore.security import aware, decrypt, encrypt, utcnow

log = logging.getLogger(__name__)

SETTING_KEY = "alerts"
SEVERITIES = ("critical", "high", "medium")
RANK = {s: i for i, s in enumerate(SEVERITIES)}
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "min_severity": "high",
    "all_staff": True,
    "emails": [],
    "webhook_url": "",
    "reminder_hours": 6,
    "grace_minutes": 3,
}

#: Tests swap this for a fake webhook receiver.
webhook_client: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=10)


class AlertError(ValueError):
    """Safe to show staff."""


# --- settings -----------------------------------------------------------------
def load(db: Session) -> dict[str, Any]:
    row = db.get(Setting, SETTING_KEY)
    data = {**DEFAULTS, **(json.loads(row.value) if row and row.value else {})}
    enc = data.pop("webhook_url_enc", "")
    data["webhook_url"] = decrypt(enc, "alerts") if enc else ""
    return data


def public(cfg: dict[str, Any]) -> dict[str, Any]:
    """What the console may show: the webhook is a secret, only a hint of it."""
    url = cfg.get("webhook_url") or ""
    return {**{k: v for k, v in cfg.items() if k != "webhook_url"},
            "has_webhook": bool(url), "webhook_hint": (url.split("//", 1)[-1].split("/", 1)[0] if url else None)}


def save(db: Session, changes: dict[str, Any], by: str) -> dict[str, Any]:
    cfg = load(db)
    if "min_severity" in changes and changes["min_severity"] not in SEVERITIES:
        raise AlertError("Choose critical, high or medium.")
    if "emails" in changes:
        emails = [e.strip().lower() for e in changes["emails"] if e and e.strip()]
        if any("@" not in e or " " in e for e in emails):
            raise AlertError("One of the email addresses is not valid.")
        changes["emails"] = sorted(set(emails))
    if "webhook_url" in changes and changes["webhook_url"] is not None:
        url = changes["webhook_url"].strip()
        if url and not url.startswith("https://"):
            raise AlertError("The webhook must be an https:// address.")
        changes["webhook_url"] = url
    for key in ("reminder_hours", "grace_minutes"):
        if key in changes:
            value = int(changes[key] or 0)
            if value < 0 or value > (168 if key == "reminder_hours" else 120):
                raise AlertError("That number is out of range.")
            changes[key] = value
    cfg.update({k: v for k, v in changes.items() if k in DEFAULTS and v is not None})
    stored = {k: v for k, v in cfg.items() if k != "webhook_url"}
    stored["webhook_url_enc"] = encrypt(cfg["webhook_url"], "alerts") if cfg["webhook_url"] else ""
    row = db.get(Setting, SETTING_KEY)
    if row is None:
        row = Setting(key=SETTING_KEY, value="{}")
        db.add(row)
    row.value = json.dumps(stored)
    row.updated_by = by
    db.flush()
    return cfg


def recipients(db: Session, cfg: dict[str, Any]) -> list[str]:
    out = set(cfg.get("emails") or [])
    if cfg.get("all_staff"):
        out |= {s.email for s in db.scalars(select(StaffUser).where(StaffUser.is_active.is_(True)))}
    return sorted(out)


# --- delivery -------------------------------------------------------------------
def _line(a: AlertState | dict[str, Any]) -> str:
    get = (lambda k: a.get(k)) if isinstance(a, dict) else (lambda k: getattr(a, k))
    who = " · ".join(x for x in (get("account"), get("service")) if x)
    text = f"[{get('severity').upper()}] {get('issue')}"
    if who:
        text += f" — {who}"
    if get("detail"):
        text += f"\n    {get('detail')}"
    return text


def _since(start: datetime, now: datetime) -> str:
    minutes = max(1, int((now - aware(start)).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h {minutes % 60} min" if hours < 48 else f"{hours // 24} days"


def deliver(db: Session, cfg: dict[str, Any], subject: str, body: str) -> bool:
    """Send one message on every configured channel. True if any got through."""
    sent = False
    for email in recipients(db, cfg):
        sent = try_send(email, subject, body) or sent
    url = cfg.get("webhook_url")
    if url:
        try:
            with webhook_client() as client:
                response = client.post(url, json={"text": f"*{subject}*\n{body}"})
            if response.status_code < 300:
                sent = True
            else:
                log.warning("Alert webhook answered %s", response.status_code)
        except httpx.HTTPError as exc:
            log.warning("Alert webhook failed: %s", exc)
    return sent


# --- the check ------------------------------------------------------------------
def check(db: Session, *, now: datetime | None = None, incidents: list[dict[str, Any]] | None = None) -> dict[str, int]:
    """Compare what needs attention now with what was known, and alert. Never raises
    for a delivery problem; the next check tries again."""
    cfg = load(db)
    now = now or utcnow()
    if incidents is None:
        from obcore.services.dashboard import current_incidents

        incidents = current_incidents(db)
    known = {s.key: s for s in db.scalars(select(AlertState))}
    floor = RANK.get(cfg["min_severity"], 1)
    grace = timedelta(minutes=cfg["grace_minutes"])
    remind = timedelta(hours=cfg["reminder_hours"]) if cfg["reminder_hours"] else None

    fresh: list[AlertState] = []
    still: list[AlertState] = []
    seen = set()
    for item in incidents:
        key = item["key"][:200]
        seen.add(key)
        state = known.get(key)
        if state is None or state.resolved_at is not None:
            state = state or AlertState(key=key)
            state.first_seen_at, state.notified_at, state.reminded_at, state.resolved_at = now, None, None, None
            db.add(state)
        elif state.notified_at and RANK.get(item["severity"], 9) < RANK.get(state.severity, 9):
            state.notified_at = None   # got worse since it was announced: announce again
        state.severity, state.issue, state.detail = item["severity"], item["issue"][:240], item.get("detail")
        state.account_id, state.account, state.service = item.get("account_id"), item.get("account"), item.get("service")
        state.last_seen_at = now
        if RANK.get(state.severity, 9) > floor:
            continue
        if state.notified_at is None and now - aware(state.first_seen_at) >= grace:
            fresh.append(state)
        elif state.notified_at and remind and now - aware(state.reminded_at or state.notified_at) >= remind:
            still.append(state)

    cleared = []
    for key, state in known.items():
        if key not in seen and state.resolved_at is None:
            state.resolved_at = now
            if state.notified_at:
                cleared.append(state)
    db.flush()

    if not cfg["enabled"] or not (fresh or still or cleared):
        return {"new": 0, "reminders": 0, "resolved": 0}
    parts = []
    if fresh:
        parts.append(f"{len(fresh)} new")
    if still:
        parts.append(f"{len(still)} still open")
    if cleared:
        parts.append(f"{len(cleared)} resolved")
    worst = min((s.severity for s in fresh + still), key=lambda s: RANK.get(s, 9), default=None)
    subject = f"[{settings.app_name}{' ' + worst.upper() if worst else ''}] Needs attention: {', '.join(parts)}"
    sections = []
    if fresh:
        sections.append("NEW\n" + "\n".join(_line(s) for s in fresh))
    if still:
        sections.append("STILL OPEN\n" + "\n".join(f"{_line(s)}\n    open for {_since(s.first_seen_at, now)}" for s in still))
    if cleared:
        sections.append("RESOLVED\n" + "\n".join(f"{_line(s)}\n    lasted {_since(s.first_seen_at, now)}" for s in cleared))
    body = "\n\n".join(sections) + f"\n\nOpen the staff console: {settings.base}/staff/#/overview\n"
    if deliver(db, cfg, subject, body):
        for s in fresh:
            s.notified_at = now
        for s in still:
            s.reminded_at = now
        db.flush()
        return {"new": len(fresh), "reminders": len(still), "resolved": len(cleared)}
    # Nothing got through: leave them unannounced so the next check tries again.
    return {"new": 0, "reminders": 0, "resolved": 0}


def open_alerts(db: Session) -> list[AlertState]:
    return list(db.scalars(select(AlertState).where(AlertState.resolved_at.is_(None))))

"""The staff overview: business figures, service health and what needs attention.

Everything here is computed from the core's own tables plus what each service
reports (``services/ops.py``). Nothing is invented: a figure with no history
yet (monthly revenue before the first daily snapshot) comes back as ``None``.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.models import (
    AccountClosure, MetricSnapshot,
    OPEN_STAGES, Account, AccountService, AccountStatus, Lead, OdooConnection, OdooConnectionStatus,
    PendingSignup, ServiceStatus,
)
from obcore.security import aware, utcnow
from obcore.services import ops

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
#: A change a service hasn't acknowledged is only a problem after this long.
SYNC_GRACE = timedelta(minutes=5)


def _iso(value: datetime | None) -> str | None:
    value = aware(value) if isinstance(value, datetime) else value
    return value.isoformat() if value else None


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _mix(account: Account) -> str:
    """Where the account stands, one bucket each."""
    if account.status == AccountStatus.suspended.value:
        return "suspended"
    rows = [r for r in account.services if r.status != ServiceStatus.cancelled.value]
    if any(r.status == ServiceStatus.past_due.value for r in rows):
        return "past_due"
    if any(r.status == ServiceStatus.active.value and r.stripe_item_id for r in rows):
        return "paying"
    if any(r.status == ServiceStatus.trialing.value for r in rows):
        return "trial"
    if any(r.status == ServiceStatus.active.value for r in rows):
        return "paying"
    return "no_service"


TRIAL_WARNING_DAYS = 7

#: Lower is more urgent: what staff should look at first in the accounts list.
URGENCY = {"past_due": 0, "suspended": 1, "trial_ending": 2, "trial": 3, "mixed": 4, "paying": 5, "none": 6}


def billing_state(account: Account, now: datetime | None = None) -> dict[str, Any]:
    """The account's billing in one pill, whatever number of services it has.

    Worst state wins: a past-due service, then a trial about to end, then a mix of
    paying and trialing, then paying, then trial, then nothing. The pill carries the
    next event (a trial's end); the counts it can't carry go in ``note``. Each
    service's own state is the dot in the Services column. ``renews_at`` is for
    the paying states; the page words it.
    """
    now = now or utcnow()
    rows = [r for r in account.services if r.status != ServiceStatus.cancelled.value]
    renews = _iso(account.renews_at) if getattr(account, "renews_at", None) else None
    out = {"key": "none", "label": "No Service", "tone": "mute", "note": None, "trial_days": None, "renews_at": None}
    if rows:
        past = [r for r in rows if r.status == ServiceStatus.past_due.value]
        paying = [r for r in rows if r.status == ServiceStatus.active.value]
        trials = [r for r in rows if r.status == ServiceStatus.trialing.value]
        days = [max(0, math.ceil((aware(r.trial_ends_at) - now).total_seconds() / 86400)) for r in trials if r.trial_ends_at]
        soonest = min(days) if days else None
        many = len(rows) > 1
        counts = " · ".join(filter(None, [f"{len(paying)} paying" if paying else "", f"{len(trials)} on trial" if trials else "",
                                          f"{len(past)} past due" if past and len(past) < len(rows) else ""]))
        when = None if soonest is None else ("ends today" if soonest == 0 else f"ends in {soonest} d")
        if past:
            out.update(key="past_due", label="Payment Due", tone="bad", note=f"{len(past)} of {len(rows)} services" if many else None)
        elif soonest is not None and soonest <= TRIAL_WARNING_DAYS:
            out.update(key="trial_ending", label=f"Trial · {when}", tone="warn", note=counts if many and paying else None)
        elif paying and trials:
            out.update(key="mixed", label="Paying + Trial", tone="ok", note=counts, renews_at=renews)
        elif paying:
            out.update(key="paying", label="Paying", tone="ok", renews_at=renews)
        else:
            out.update(key="trial", label=f"Trial · {when}" if when else "Trial", tone="trialing")
        out["trial_days"] = soonest
    suspended = account.status == AccountStatus.suspended.value
    out["rank"] = URGENCY["suspended"] if suspended and out["key"] != "past_due" else URGENCY[out["key"]]
    return out


#: What each Billing filter matches (a mixed account is in both Paying and Trial).
BILLING_FILTERS = {
    "paying": {"paying", "mixed"}, "trial": {"trial", "trial_ending", "mixed"},
    "trial_ending": {"trial_ending"}, "past_due": {"past_due"}, "none": {"none"},
}


def _subscription_label(account: Account) -> str | None:
    return {"paying": "paying", "trial": "trial", "past_due": "past_due",
            "suspended": "suspended"}.get(_mix(account))


#: Needs Attention groups, in the order the overview lists them.
CATEGORIES = ("service", "integration", "payment", "suspended", "trial", "onboarding", "unverified")


def _incident(severity: str, issue: str, *, detail: str | None = None, account: Account | None = None,
              service: str | None = None, since: datetime | None = None, key: str,
              link: str | None = None, category: str = "service") -> dict[str, Any]:
    return {"key": key, "severity": severity, "category": category,
            "issue": issue[:120], "detail": (detail or "")[:400] or None,
            "account_id": account.id if account else None, "account": account.name if account else None,
            "service": service, "since": _iso(since), "link": link}


def _incidents(db: Session, accounts: dict[str, Account], health: list[dict[str, Any]],
               now: datetime) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def add(item: dict[str, Any]) -> None:
        found.setdefault(item["key"], item)   # one row per problem, customer and service

    for svc in health:
        if svc["status"] == "unavailable":
            add(_incident("critical", f"{svc['name']} is not answering", detail=svc["error"],
                          service=svc["name"], since=svc["down_since"], key=f"svc:{svc['slug']}"))
        elif svc["status"] == "degraded":
            scheduler = (svc["workers"] or {}).get("scheduler") or {}
            why = svc.get("status_reason") or (
                "Its scheduler has stopped." if scheduler and not scheduler.get("ok") else "Many integrations are failing.")
            add(_incident("high", f"{svc['name']} is degraded", detail=why, service=svc["name"],
                          since=_parse(scheduler.get("last_run_at")) if scheduler and not scheduler.get("ok") else None,
                          key=f"svc:{svc['slug']}"))
        for entry in svc["accounts"]:
            account = accounts.get(entry.get("account_id"))
            if account is None:
                continue
            for failure in entry.get("failures") or []:
                add(_incident("high", failure.get("issue") or "Sync failing", detail=failure.get("detail"),
                              account=account,
                              service=svc["name"], since=_parse(failure.get("since")),
                              key=f"int:{svc['slug']}:{account.id}:{failure.get('key')}",
                              link=f"#/accounts/{account.id}", category="integration"))

    for conn in db.scalars(select(OdooConnection).where(
            OdooConnection.status == OdooConnectionStatus.failed.value)):
        account = accounts.get(conn.account_id)
        if account is None:
            continue
        add(_incident("high", "Odoo connection failing", detail=conn.status_message, account=account,
                      service="Odoo", since=conn.failing_since or conn.last_checked_at,
                      key=f"odoo:{conn.id}", link=f"#/accounts/{account.id}", category="integration"))

    for row in db.scalars(select(AccountService)):
        account = accounts.get(row.account_id)
        if account is None or row.status == ServiceStatus.cancelled.value:
            continue
        if row.status == ServiceStatus.past_due.value:
            issue = "Payment failed" if row.stripe_item_id else "Trial ended unpaid"
            add(_incident("medium", issue, account=account, service=row.service.name,
                          since=row.trial_ends_at if not row.stripe_item_id else row.updated_at,
                          key=f"pay:{row.id}", link=f"#/accounts/{account.id}", category="payment"))
        if row.provisioned_revision < row.revision and aware(row.updated_at) < now - SYNC_GRACE:
            add(_incident("medium", "Change not picked up yet",
                          detail=row.provision_error or f"{row.service.name} has not confirmed the latest change.",
                          account=account, service=row.service.name, since=row.updated_at,
                          key=f"sync:{row.id}", link=f"#/accounts/{account.id}"))

    week = now + timedelta(days=7)
    for account in accounts.values():
        if account.status == AccountStatus.suspended.value:
            add(_incident("low", "Account suspended", detail=account.suspension_reason, account=account,
                          since=account.suspended_at, key=f"susp:{account.id}", link=f"#/accounts/{account.id}",
                          category="suspended"))
            continue
        live_rows = [r for r in account.services if r.status != ServiceStatus.cancelled.value]
        ending = [r for r in live_rows if r.status == ServiceStatus.trialing.value and not r.stripe_item_id
                  and r.trial_ends_at and now <= aware(r.trial_ends_at) <= week]
        if ending and not account.stripe_subscription_id:
            first = min(aware(r.trial_ends_at) for r in ending)
            days = max(0, (first - now).days)
            add(_incident("low", "Trial ending soon",
                          detail=f"Ends {'today' if days == 0 else f'in {days} day' + ('s' if days != 1 else '')}, no payment details yet.",
                          account=account, service=", ".join(r.service.name for r in ending), since=first,
                          key=f"trial:{account.id}", link=f"#/accounts/{account.id}", category="trial"))
        needs = [r for r in live_rows if r.service.uses_odoo and r.status in (
            ServiceStatus.trialing.value, ServiceStatus.active.value)]
        if needs and aware(account.created_at) < now - ONBOARDING_GRACE:
            no_odoo = not account.odoo_connections
            stuck = [r for r in needs if (r.odoo_readiness or {}).get("setup_complete") is False
                     or ((r.odoo_readiness or {}).get("setup_complete") is None and no_odoo)]
            if stuck:
                add(_incident("low", "Setup not finished",
                              detail="Odoo is not connected yet." if no_odoo else "The service's setup is not finished.",
                              account=account, service=", ".join(r.service.name for r in stuck),
                              since=account.created_at, key=f"onb:{account.id}", link=f"#/accounts/{account.id}",
                              category="onboarding"))

    for p in db.scalars(select(PendingSignup).where(PendingSignup.sent_at.isnot(None))):
        if aware(p.expires_at) <= now:
            continue
        add(_incident("low", "Email not verified", detail=p.email, service=None, since=p.sent_at,
                      key=f"unv:{p.id}", category="unverified"))
        found[f"unv:{p.id}"]["account"] = p.company

    items = list(found.values())
    items.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], i["since"] or "9999"))
    return items


def current_incidents(db: Session, *, fresh: bool = False) -> list[dict[str, Any]]:
    """Everything that needs attention right now (what the overview lists)."""
    accounts = {a.id: a for a in db.scalars(select(Account)) if a.status != AccountStatus.closed.value}
    return _incidents(db, accounts, ops.collect(db, fresh=fresh), utcnow())


# =============================================================================
# The overview
# =============================================================================
RANGES = {"30d": ("Last 30 Days", timedelta(days=30)), "6m": ("Last 6 Months", timedelta(days=182)),
          "12m": ("Last 12 Months", timedelta(days=365)), "all": ("All Time", None)}
#: Older names of the ranges (the period switch the overview once had).
_ALIASES = {"7d": "30d", "30d": "30d"}
#: Days after signing up before a customer who has not finished setting up counts as stuck.
ONBOARDING_GRACE = timedelta(days=3)

ACTIVITY_LABELS = {
    "account.signup": ("registered", "New customer registered"),
    "account.create": ("registered", "Customer account created by staff"),
    "user.verified": ("verified", "Email verified"),
    "service.add": ("service", "Service added"),
    "service.remove": ("service", "Service removed"),
    "service.package": ("package", "Package changed"),
    "service.trial_ended": ("trial", "Free trial ended"),
    "odoo.connect": ("integration", "Odoo connected"),
    "odoo.disconnect": ("integration", "Odoo disconnected"),
    "account.suspend": ("suspended", "Account suspended"),
    "account.reactivate": ("reactivated", "Account reactivated"),
    "account.delete": ("closed", "Account deleted"),
    "billing.cancel": ("cancel", "Subscription set to cancel"),
}
REVENUE_LABELS = {
    "new": ("paying", "Subscription activated"), "reactivation": ("paying", "Subscription reactivated"),
    "upgrade": ("package", "Upgraded"), "downgrade": ("package", "Downgraded"),
    "churn": ("cancel", "Stopped paying"), "payment": ("payment", "Payment received"),
    "payment_failed": ("payment_failed", "Payment failed"),
}


def _range(key: str, now: datetime, first: datetime | None) -> dict[str, Any]:
    key = key if key in RANGES else _ALIASES.get(key, "30d")
    label, span = RANGES[key]
    if span is None:
        start = (first or now) - timedelta(seconds=1)
        return {"key": key, "label": label, "start": start, "end": now, "prev": None}
    return {"key": key, "label": label, "start": now - span, "end": now,
            "prev": (now - 2 * span, now - span)}


def _buckets(rng: dict[str, Any]) -> list[tuple[str, datetime, datetime]]:
    """(label key, start, end) per day for 30 days, per month otherwise."""
    start, end = rng["start"], rng["end"]
    if rng["key"] == "30d":
        days = []
        day = datetime(end.year, end.month, end.day, tzinfo=end.tzinfo) - timedelta(days=29)
        for _ in range(30):
            days.append((day.date().isoformat(), day, day + timedelta(days=1)))
            day += timedelta(days=1)
        return days
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        b0 = datetime(y, m, 1, tzinfo=end.tzinfo)
        b1 = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=end.tzinfo)
        months.append((f"{y:04d}-{m:02d}", b0, b1))
        y, m = y + (m == 12), m % 12 + 1
    return months[-60:]


def _trim(points: list[dict[str, Any]], has_data, minimum: int) -> list[dict[str, Any]]:
    """Drop the empty buckets before the first one with data, so a young business
    does not get a chart that is mostly blank. Keeps at least ``minimum`` buckets."""
    first = next((i for i, p in enumerate(points) if has_data(p)), None)
    if first is None:
        return points[-minimum:] if minimum else []
    return points[min(first, max(0, len(points) - minimum)):]


def _cmp(value: Any, previous: Any, basis: str | None) -> dict[str, Any]:
    change = None
    if isinstance(value, (int, float)) and isinstance(previous, (int, float)):
        change = round((value - previous) * 100 / previous, 1) if previous else (None if value else 0.0)
    return {"value": value, "previous": previous, "change_pct": change, "basis": basis if previous is not None else None}


def _rate(part: int, whole: int) -> float | None:
    return round(part * 100 / whole, 1) if whole else None


def overview(db: Session, period: str = "30d", *, fresh: bool = False) -> dict[str, Any]:
    from obcore.models import AuditLog, BillingEvent
    from obcore.services.billing import paid_mrr_cents

    now = utcnow()
    all_accounts = db.scalars(select(Account)).all()
    live = [a for a in all_accounts if a.status != AccountStatus.closed.value]
    accounts = {a.id: a for a in live}
    closures = db.scalars(select(AccountClosure)).all()
    created = [aware(a.created_at) for a in live] + [aware(c.account_created_at or c.created_at) for c in closures]
    rng = _range(period, now, min(created, default=None))
    start, prev = rng["start"], rng["prev"]
    span_label = rng["label"].lower().replace("last ", "previous ") if prev else None

    health = ops.collect(db, fresh=fresh)
    ops.record_snapshot(db)
    figures = ops.current_figures(db)
    at_start = ops.snapshot_on_or_before(db, start.date()) if prev else None
    since_label = {"30d": "30 days ago", "6m": "6 months ago", "12m": "12 months ago"}.get(rng["key"])

    def within(when: datetime | None, a: datetime, b: datetime) -> bool:
        return when is not None and a <= aware(when) < b

    def snap(attr: str):
        return getattr(at_start, attr, None) if at_start is not None else None

    # --- the eight figures -------------------------------------------------------
    def new_customers(a: datetime, b: datetime) -> int:
        return sum(1 for c in created if a <= c < b)

    trial_ended = db.execute(select(AuditLog.account_id, AuditLog.created_at)
                             .where(AuditLog.action == "service.trial_ended")).all()

    def conversion(a: datetime, b: datetime) -> dict[str, Any]:
        converted = {x.id for x in live if not x.paid_at_signup and within(x.first_paid_at, a, b)}
        ended = {acc for acc, when in trial_ended if within(when, a, b)} - converted
        ended = {acc for acc in ended if acc not in accounts or not accounts[acc].first_paid_at
                 or aware(accounts[acc].first_paid_at) >= b}
        return {"converted": len(converted), "ended_unpaid": len(ended),
                "rate": _rate(len(converted), len(converted) + len(ended))}

    events = db.scalars(select(BillingEvent).order_by(BillingEvent.created_at)).all()

    def churn(a: datetime, b: datetime, base: int | None) -> dict[str, Any]:
        """Customers who stopped paying in the window against those paying at its start.

        With no snapshot from the start of the window the base is rebuilt from the
        revenue log: paying now, minus those who started paying since, plus those lost."""
        lost = {e.account_id for e in events if e.kind == "churn" and within(e.created_at, a, b)}
        won = {e.account_id for e in events if e.kind in ("new", "reactivation") and within(e.created_at, a, b)}
        if base is None and events:
            base = max(0, paying_now - len(won - lost) + len(lost))
        if base is None:
            return {"churned": len(lost), "base": None, "rate": None, "why": "Not enough history yet"}
        if not base:
            return {"churned": len(lost), "base": 0, "rate": None, "why": "No paying customers at the start of this period"}
        return {"churned": len(lost), "base": base, "rate": min(100.0, _rate(len(lost), base)), "why": None}

    paying_now = sum(1 for a in live if _mix(a) == "paying")
    failed_int = (sum(int((s["integrations"] or {}).get("failed") or 0) for s in health if s["integrations"])
                  + sum(1 for a in live for c in a.odoo_connections if c.status == OdooConnectionStatus.failed.value))
    leads_open = db.scalars(select(Lead).where(Lead.stage.in_(OPEN_STAGES))).all()
    owed = ops.outstanding(db, live)

    conv_now = conversion(start, now)
    conv_prev = conversion(*prev) if prev else None
    prev_start_snap = ops.snapshot_on_or_before(db, prev[0].date()) if prev else None
    churn_now = churn(start, now, snap("paying"))
    churn_prev = churn(*prev, getattr(prev_start_snap, "paying", None)) if prev else None

    kpis = {
        "new_customers": {**_cmp(new_customers(start, now), new_customers(*prev) if prev else None, span_label),
                          "closed": sum(1 for c in closures if within(c.account_created_at or c.created_at, start, now))},
        "active_customers": _cmp(paying_now, snap("paying"), since_label),
        "trial_conversion": {**_cmp(conv_now["rate"], conv_prev["rate"] if conv_prev else None, span_label),
                             "converted": conv_now["converted"], "ended_unpaid": conv_now["ended_unpaid"],
                             "running": sum(1 for a in live if _mix(a) == "trial")},
        "mrr": _cmp(figures["mrr_cents"], snap("mrr_cents"), since_label),
        "churn": {**_cmp(churn_now["rate"], churn_prev["rate"] if churn_prev else None, span_label),
                  "churned": churn_now["churned"], "base": churn_now["base"], "why": churn_now["why"]},
        "outstanding": {**_cmp(owed["accounts"], snap("outstanding"), since_label), "cents": owed["cents"]},
        "open_leads": _cmp(len(leads_open), snap("open_leads"), since_label),
        "failed_integrations": _cmp(failed_int, snap("failed_integrations"), since_label),
    }

    # --- customer acquisition and revenue, per bucket --------------------------
    buckets = _buckets(rng)
    snaps = db.scalars(select(MetricSnapshot).where(MetricSnapshot.day >= buckets[0][1].date())
                       .order_by(MetricSnapshot.day)).all() if buckets else []
    acquisition, revenue = [], []
    for key, b0, b1 in buckets:
        acquisition.append({"key": key, "new": new_customers(b0, b1)})
        last = None
        for row in snaps:
            if row.day < b1.date():
                last = row
        in_b = [e for e in events if within(e.created_at, b0, b1)]
        revenue.append({
            "key": key, "mrr_cents": last.mrr_cents if last is not None else None,
            "new_cents": sum((e.mrr_after_cents or 0) - (e.mrr_before_cents or 0) for e in in_b
                             if e.kind in ("new", "reactivation", "upgrade")),
            "churned_cents": sum((e.mrr_before_cents or 0) - (e.mrr_after_cents or 0) for e in in_b
                                 if e.kind in ("churn", "downgrade")),
        })
    acquisition = _trim(acquisition, lambda b: b["new"] > 0, 7 if rng["key"] == "30d" else 3)
    revenue = _trim(revenue, lambda b: b["mrr_cents"] is not None, 0)
    in_range = [e for e in events if within(e.created_at, start, now)]
    failed_pay = [e for e in in_range if e.kind == "payment_failed"]
    mrr_start = snap("mrr_cents")
    def _net(kinds_up: tuple, kinds_down: tuple) -> int:
        return (sum((e.mrr_after_cents or 0) - (e.mrr_before_cents or 0) for e in in_range if e.kind in kinds_up)
                - sum((e.mrr_before_cents or 0) - (e.mrr_after_cents or 0) for e in in_range if e.kind in kinds_down))

    revenue_summary = {
        "net_new_cents": _net(("new", "reactivation", "upgrade"), ("churn", "downgrade")),
        "mrr_cents": figures["mrr_cents"],
        "growth_pct": _cmp(figures["mrr_cents"], mrr_start, since_label)["change_pct"],
        "growth_basis": since_label if mrr_start is not None else None,
        "new_cents": sum((e.mrr_after_cents or 0) - (e.mrr_before_cents or 0) for e in in_range
                         if e.kind in ("new", "reactivation")),
        "expansion_cents": sum((e.mrr_after_cents or 0) - (e.mrr_before_cents or 0) for e in in_range if e.kind == "upgrade"),
        "churned_cents": sum((e.mrr_before_cents or 0) - (e.mrr_after_cents or 0) for e in in_range if e.kind == "churn"),
        "contraction_cents": sum((e.mrr_before_cents or 0) - (e.mrr_after_cents or 0) for e in in_range if e.kind == "downgrade"),
        "collected_cents": sum(e.amount_cents or 0 for e in in_range if e.kind == "payment"),
        "failed_payments": len(failed_pay), "failed_cents": sum(e.amount_cents or 0 for e in failed_pay),
        "history_from": snaps[0].day.isoformat() if snaps else None,
    }

    # --- funnel: who came in during the range, and how far they got -------------
    pending_all = db.scalars(select(PendingSignup)).all()
    cohort = [a for a in live if within(a.created_at, start, now)]
    closed_cohort = [c for c in closures if within(c.account_created_at or c.created_at, start, now)]
    verified_n = len(cohort) + len(closed_cohort)
    registered_n = verified_n + sum(1 for p in pending_all if within(p.created_at, start, now) and (p.sent_at or p.paid_at))
    subscribed_n = (sum(1 for a in cohort if a.services or a.paid_at_signup or a.first_paid_at)
                    + sum(1 for c in closed_cohort if c.services))
    paying_n = min(subscribed_n, sum(1 for a in cohort if a.first_paid_at))
    stages = [("registered", "Registered", registered_n), ("verified", "Verified", min(verified_n, registered_n)),
              ("trial", "Subscribed", min(subscribed_n, verified_n)), ("paying", "Paying", paying_n)]
    funnel, before = [], None
    for key, label, value in stages:
        value = min(value, before) if before is not None else value
        funnel.append({"key": key, "label": label, "value": value,
                       "pct_prev": min(100.0, _rate(value, before)) if before else None,
                       "pct_top": min(100.0, _rate(value, stages[0][2])) if stages[0][2] else None})
        before = value

    # --- needs attention, recent customers and activity -------------------------
    incidents = _incidents(db, accounts, health, now)
    core = ops.core_health(db)
    if core["status"] != "operational":
        incident = _incident("critical" if core["status"] == "unavailable" else "high",
                             "OdooBridger Core is " + ("not working" if core["status"] == "unavailable" else "degraded"),
                             detail=core.get("status_reason"), service="OdooBridger Core",
                             since=_parse((core.get("scheduler") or {}).get("last_run_at")), key="svc:core")
        incidents.insert(0, incident)
    mix = Counter(_mix(a) for a in live)

    def last_active(a: Account) -> str | None:
        seen = [aware(u.last_login_at) for u in a.users if u.last_login_at]
        return _iso(max(seen)) if seen else None

    recent = sorted(live, key=lambda a: aware(a.created_at), reverse=True)[:8]
    recent_rows = [{
        "id": a.id, "name": a.name, "subscription": _subscription_label(a),
        "services": [r.service.name for r in a.services if r.status != ServiceStatus.cancelled.value],
        "status": a.status, "created_at": _iso(a.created_at), "last_active_at": last_active(a),
    } for a in recent] + [{
        "id": None, "name": p.company, "subscription": None, "services": [], "status": "unverified",
        "created_at": _iso(p.created_at), "last_active_at": None,
    } for p in pending_all if p.sent_at and aware(p.expires_at) > now]
    recent_rows = sorted(recent_rows, key=lambda r: r["created_at"] or "", reverse=True)[:8]

    activity = _activity(db, accounts, events, pending_all)

    # --- service health ---------------------------------------------------------
    history = ops.check_history(db, now=now)
    service_rows = [{
        "slug": sv["slug"], "name": sv["name"], "status": sv["status"], "status_reason": sv.get("status_reason"),
        "customers": sv["customers"],
        "latency_ms": sv["latency_ms"], "error": sv["error"], "version": sv["version"],
        "availability_pct": (history.get(sv["slug"]) or {}).get("availability_pct"),
        "availability_since": (history.get(sv["slug"]) or {}).get("since"),
        "availability_days": (history.get(sv["slug"]) or {}).get("days_of_data"),
        "availability_checks": (history.get(sv["slug"]) or {}).get("checks"),
        "avg_latency_ms": (history.get(sv["slug"]) or {}).get("avg_latency_ms"),
        "p95_latency_ms": (history.get(sv["slug"]) or {}).get("p95_latency_ms"),
        "failed_checks_24h": (history.get(sv["slug"]) or {}).get("failed_checks_24h"),
        "checks_24h": (history.get(sv["slug"]) or {}).get("checks_24h"),
        "requests": sv.get("requests"), "database": sv.get("database"), "jobs": sv["jobs"],
        "integrations": sv["integrations"], "has_metrics": sv["has_metrics"],
        "scheduler": (sv["workers"] or {}).get("scheduler"), "down_since": _iso(sv["down_since"]),
        "checked_at": _iso(sv["checked_at"]),
    } for sv in health]

    return {
        "range": {"key": rng["key"], "label": rng["label"], "start": _iso(start), "end": _iso(now),
                  "compare": span_label, "since": since_label},
        "kpis": kpis,
        "acquisition": {"buckets": acquisition, "unit": "day" if rng["key"] == "30d" else "month",
                        "total": kpis["new_customers"]["value"], "previous": kpis["new_customers"]["previous"]},
        "revenue": {"summary": revenue_summary, "buckets": revenue},
        "funnel": funnel,
        "funnel_closed": len(closed_cohort),
        "mix_closed": len(closures),
        "mix": {k: mix.get(k, 0) for k in ("paying", "trial", "past_due", "suspended", "no_service")},
        "leads": {"open": len(leads_open), "new_in_range": db.query(Lead).filter(Lead.created_at >= start).count(),
                  "by_stage": {st: sum(1 for l in leads_open if l.stage == st) for st in OPEN_STAGES},
                  "pipeline_cents": sum(l.value_cents or 0 for l in leads_open)},
        "incidents": incidents,
        "recent_customers": recent_rows,
        "activity": activity,
        "core": core,
        "services": service_rows,
        "checked_at": _iso(now),
    }


def _activity(db: Session, accounts: dict[str, Account], events: list, pending: list) -> list[dict[str, Any]]:
    """The latest things that happened across OdooBridger, newest first."""
    from obcore.models import AuditLog

    rows = db.scalars(select(AuditLog).where(AuditLog.action.in_(tuple(ACTIVITY_LABELS)))
                      .order_by(AuditLog.created_at.desc()).limit(40)).all()
    out = []
    for r in rows:
        kind, label = ACTIVITY_LABELS[r.action]
        account = accounts.get(r.account_id)
        detail = r.detail.split("->", 1)[1].strip() if r.action == "service.package" and r.detail and "->" in r.detail else None
        out.append({"at": _iso(r.created_at), "kind": kind, "title": label,
                    "account": account.name if account else None, "account_id": account.id if account else None,
                    "detail": f"to {detail}" if detail else None})
    for e in events[-40:]:
        kind, label = REVENUE_LABELS.get(e.kind, ("payment", e.kind))
        amount = e.amount_cents if e.kind in ("payment", "payment_failed") else None
        out.append({"at": _iso(e.created_at), "kind": kind, "title": label,
                    "account": e.account_name, "account_id": e.account_id if e.account_id in accounts else None,
                    "amount_cents": amount, "currency": e.currency,
                    "mrr_cents": e.mrr_after_cents if e.kind not in ("payment", "payment_failed", "churn") else None})
    for p in pending:
        if p.sent_at or p.paid_at:
            out.append({"at": _iso(p.created_at), "kind": "signup", "title": "Signed up, email not verified yet",
                        "account": p.company, "account_id": None})
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return out[:25]


# =============================================================================
# One service's own dashboard
# =============================================================================
def service_dashboard(db: Session, slug: str, *, fresh: bool = False) -> dict[str, Any] | None:
    """What one service reports for its dashboard, joined with the core's own
    view of its customers (name, plan, status). Only generic figures: services
    send counts and statuses, never connection details."""
    from obcore.models import Service

    service = db.scalar(select(Service).where(Service.slug == slug))
    if service is None:
        return None
    health = next((h for h in ops.collect(db, fresh=fresh) if h["slug"] == slug), None) or {}
    board = health.get("dashboard") or {}
    per_account = board.get("accounts") or {}
    rows = [r for r in db.scalars(select(AccountService).where(AccountService.service_id == service.id))
            if r.status != ServiceStatus.cancelled.value and r.account.status != AccountStatus.closed.value]
    customers = [{
        "account_id": r.account_id, "name": r.account.name,
        "plan": r.package.name if r.package else None, "status": r.status,
        "since": _iso(r.created_at), "billing": _billing(r),
        "values": per_account.get(r.account_id) or {},
    } for r in sorted(rows, key=lambda r: r.account.name.lower())]
    names = {r.account_id: r.account.name for r in rows}
    attention = [{**a, "account": names.get(a.get("account_id"))} for a in board.get("attention") or []
                 if a.get("account_id") in names or not a.get("account_id")]
    attention.sort(key=lambda a: (SEVERITY_ORDER.get(a.get("severity"), 3), a.get("since") or "9999"))
    return {
        "service": {"slug": service.slug, "name": service.name, "status": health.get("status"),
                    "version": health.get("version"), "error": health.get("error"),
                    "scheduler": (health.get("workers") or {}).get("scheduler"),
                    "checked_at": _iso(health.get("checked_at")) if isinstance(health.get("checked_at"), datetime)
                    else health.get("checked_at"),
                    "has_dashboard": bool(board)},
        # The core's own figures about its customers of this service; the
        # service reports none of its own (nothing about customers' usage).
        "tiles": _service_tiles(rows, per_account),
        "series": board.get("series"),
        "columns": board.get("columns") or [],
        "customers": customers,
        "attention": attention,
    }


def _billing(row: AccountService) -> dict[str, Any]:
    """How the customer stands on paying for this service, in a few words."""
    account = row.account
    if row.status == ServiceStatus.past_due.value:
        return {"label": "Payment failed" if row.stripe_item_id else "Trial ended, unpaid", "tone": "bad", "date": None}
    if row.status == ServiceStatus.suspended.value or account.status == AccountStatus.suspended.value:
        return {"label": "Suspended", "tone": "bad", "date": None}
    if row.status == ServiceStatus.trialing.value:
        return {"label": "Trial ends", "tone": "trialing", "date": _iso(row.trial_ends_at)}
    if row.stripe_item_id:
        every = "yearly" if account.billing_interval == "year" else "monthly"
        return {"label": f"Paid online, {every} · renews", "tone": "ok", "date": _iso(account.renews_at)}
    return {"label": "Invoiced manually", "tone": "mute", "date": None}


def _service_tiles(rows: list[AccountService], per_account: dict[str, Any]) -> list[dict[str, Any]]:
    by = lambda st: sum(1 for r in rows if r.status == st)  # noqa: E731
    issues = sum(1 for r in rows if (per_account.get(r.account_id) or {}).get("health") in ("failing", "delayed"))
    return [
        {"key": "customers", "label": "Customers", "value": len(rows), "icon": "users"},
        {"key": "trialing", "label": "On Trial", "value": by(ServiceStatus.trialing.value), "icon": "clock"},
        {"key": "active", "label": "Paying", "value": by(ServiceStatus.active.value), "icon": "card", "tone": "ok"},
        {"key": "past_due", "label": "Payment Pending", "value": by(ServiceStatus.past_due.value), "icon": "alert",
         "tone": "bad" if by(ServiceStatus.past_due.value) else "ok"},
        {"key": "sync_issues", "label": "Sync Issues", "value": issues, "icon": "alert",
         "tone": "warn" if issues else "ok"},
    ]

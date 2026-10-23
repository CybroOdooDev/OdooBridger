"""The built-in catalog entry for the biometric service (BioBridge)."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from obcore.config import settings
from obcore.models import Service, ServicePackage
from obcore.security import hash_token

log = logging.getLogger(__name__)

BIOMETRIC_PACKAGES = [
    {"name": "Starter", "description": "One site, a couple of terminals.",
     "monthly_price_cents": 2900, "yearly_price_cents": 29000, "sort_order": 1,
     "entitlements": {"max_devices": 2, "max_employees": 50, "min_sync_interval_minutes": 15},
     "features": ["2 biometric terminals", "Up to 50 employees", "Sync every 15 minutes"]},
    {"name": "Growth", "description": "Several sites and devices.", "is_default": True,
     "monthly_price_cents": 7900, "yearly_price_cents": 79000, "sort_order": 2,
     "entitlements": {"max_devices": 10, "max_employees": 300, "min_sync_interval_minutes": 5},
     "features": ["10 biometric terminals", "Up to 300 employees", "Sync every 5 minutes"]},
    {"name": "Business", "description": "Large workforces, no limits.",
     "monthly_price_cents": 19900, "yearly_price_cents": 199000, "sort_order": 3,
     "entitlements": {"max_devices": None, "max_employees": None, "min_sync_interval_minutes": 1},
     "features": ["Unlimited terminals", "Unlimited employees", "Sync every minute"]},
]


def ensure_biometric(db: Session) -> Service | None:
    """Register (or update) the biometric service from BIOMETRIC_* settings.

    Only the connection details and secret are managed here; packages are
    created once and then belong to the console.
    """
    if not settings.biometric_client_secret:
        return None
    service = db.scalar(select(Service).where(Service.slug == "biometric"))
    if service is None:
        service = Service(slug="biometric", name="Biometric Attendance",
                          tagline="Biometric terminals into Odoo attendance, automatically.",
                          description="Connect ZKTeco, Hikvision, Suprema and other terminals to Odoo. "
                                      "Punches become hr.attendance records on a schedule.",
                          base_url=settings.biometric_base_url, sort_order=1)
        db.add(service)
        db.flush()
        for spec in BIOMETRIC_PACKAGES:
            db.add(ServicePackage(service_id=service.id, **spec))
        log.info("Registered the biometric service at %s", settings.biometric_base_url)
    service.uses_odoo = True
    service.base_url = settings.biometric_base_url
    service.internal_url = settings.biometric_internal_url or None
    service.client_secret_hash = hash_token(settings.biometric_client_secret)
    db.flush()
    return service


DEMO_PACKAGES = [
    {"name": "Basic", "description": "A small team.", "monthly_price_cents": 900, "yearly_price_cents": 9000,
     "sort_order": 1, "entitlements": {"max_seats": 3}, "features": ["3 seats", "Shared dashboard"]},
    {"name": "Plus", "description": "Growing teams.", "is_default": True, "monthly_price_cents": 1900,
     "yearly_price_cents": 19000, "sort_order": 2, "entitlements": {"max_seats": 10},
     "features": ["10 seats", "Shared dashboard"]},
]


def ensure_demo(db: Session) -> Service | None:
    """Register (or update) the demo service, Team Desk, from DEMO_* settings.

    It exists to show a customer several purchased services side by side.
    Off unless DEMO_CLIENT_SECRET is set.
    """
    if not settings.demo_client_secret:
        return None
    service = db.scalar(select(Service).where(Service.slug == "demo"))
    if service is None:
        service = Service(slug="demo", name="Team Desk (Demo)",
                          tagline="A small demo service for trying OdooBridger.",
                          description="Shows how a second service appears next to Biometric Attendance: "
                                      "its own package, limits, status and sign-in.",
                          base_url=settings.demo_base_url, sso_callback_path="/sso", sort_order=9)
        db.add(service)
        db.flush()
        for spec in DEMO_PACKAGES:
            db.add(ServicePackage(service_id=service.id, **spec))
        log.info("Registered the demo service at %s", settings.demo_base_url)
    service.base_url = settings.demo_base_url
    service.internal_url = settings.demo_internal_url or None
    service.client_secret_hash = hash_token(settings.demo_client_secret)
    db.flush()
    return service

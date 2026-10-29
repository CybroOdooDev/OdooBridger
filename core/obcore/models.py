"""OdooBridger core data model.

One account (a customer company) holds users and subscribes to services. Each
subscribed service is one ``AccountService`` row: which package, its own trial
and status, and — once the account pays online — the Stripe subscription item
that bills it. All of an account's paid services share one Stripe
subscription, so the customer gets one invoice.

Services (the biometric connector, later marketplace, shipping …) are separate
applications. The core tells each one about an account it may serve
(provisioning) and lets users in with a signed identity token (SSO). It never
holds a service's own data.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from obcore.db import Base, Timestamped, UUIDPk


# =============================================================================
# Accounts and people
# =============================================================================
class AccountStatus(str, enum.Enum):
    active = "active"          # may use the services it is subscribed to
    suspended = "suspended"    # stopped by staff; every service is told
    closed = "closed"          # the customer cancelled everything; kept until deleted


class Account(Base, UUIDPk, Timestamped):
    __tablename__ = "account"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default=AccountStatus.active.value)
    country: Mapped[str | None] = mapped_column(String(80))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")

    #: Staff note on a suspension. Never shown to the customer.
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspension_reason: Mapped[str | None] = mapped_column(String(200))

    # --- billing: one Stripe customer, one subscription, an item per service --
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), index=True)
    #: Stripe needs every item of one subscription on the same interval, so the
    #: account (not each service) pays monthly or yearly.
    billing_interval: Mapped[str] = mapped_column(String(5), default="month", server_default="month")
    #: Status of the Stripe subscription as last read ("active", "past_due" …).
    stripe_status: Mapped[str | None] = mapped_column(String(24))
    renews_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    #: Paid at registration ("Buy Now"). Such an account can ask for its free
    #: trial days once, which move the first renewal back without a charge.
    paid_at_signup: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    trial_bonus_days: Mapped[int | None] = mapped_column(Integer)
    trial_bonus_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: The lead this account came from, when staff converted one.
    lead_id: Mapped[str | None] = mapped_column(String(32))

    users: Mapped[list["User"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    services: Mapped[list["AccountService"]] = relationship(
        back_populates="account", cascade="all, delete-orphan")
    odoo_connections: Mapped[list["OdooConnection"]] = relationship(
        back_populates="account", cascade="all, delete-orphan", order_by="OdooConnection.created_at")


class UserRole(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    member = "member"


class User(Base, UUIDPk, Timestamped):
    """A customer person. Staff are a different table — see StaffUser."""

    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("email", name="uq_user_email"),)

    account_id: Mapped[str] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    hashed_password: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default=UserRole.owner.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: A website signup has no password until the person opens the link in the
    #: verification email and sets one themselves. Credentials are never emailed.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Hash of the one live link token (verification/invitation or reset).
    link_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    link_token_purpose: Mapped[str | None] = mapped_column(String(12))   # "verify" | "reset"
    link_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    link_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship(back_populates="users")

    @property
    def can_manage(self) -> bool:
        return self.role in (UserRole.owner.value, UserRole.admin.value)


class StaffUser(Base, UUIDPk, Timestamped):
    """OdooBridger staff. A separate table, so a customer login can never grow
    into the console and a staff login never has a customer workspace."""

    __tablename__ = "staff_user"
    __table_args__ = (UniqueConstraint("email", name="uq_staff_email"),)

    email: Mapped[str] = mapped_column(String(254), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(120))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Set for the login managed by STAFF_EMAIL / STAFF_PASSWORD.
    is_static: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PendingSignup(Base, UUIDPk, Timestamped):
    """A website signup waiting for its email to be confirmed.

    Nothing else exists yet: no account, no user, no trial, no Stripe
    customer, nothing sent to any service. Opening the link in the email
    (and choosing a password) turns this into the account; until then it
    only holds what the form said. Signing up again with the same email
    replaces it and sends a new link. For "Buy Now" the order is reversed:
    payment first, then the link (a paid signup is never discarded).
    """

    __tablename__ = "pending_signup"
    __table_args__ = (UniqueConstraint("email", name="uq_pending_signup_email"),)

    email: Mapped[str] = mapped_column(String(254), nullable=False)
    company: Mapped[str] = mapped_column(String(160), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(80))
    timezone: Mapped[str | None] = mapped_column(String(64))
    #: [{"service": slug, "package_id": id | null}]
    services: Mapped[list] = mapped_column(JSON, default=list)
    interval: Mapped[str] = mapped_column(String(5), default="month")
    #: "trial" or "buy" (go on to Stripe Checkout once confirmed).
    mode: Mapped[str] = mapped_column(String(5), default="trial")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    #: "Buy Now": the Checkout the visitor was sent to, and, once it is paid, the
    #: Stripe customer and subscription the account will take over. The
    #: confirmation link is emailed only after that payment.
    stripe_checkout_id: Mapped[str | None] = mapped_column(String(120))
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64))
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base, UUIDPk, Timestamped):
    """One row per refresh token (its jti hashed), so sessions are revocable."""

    __tablename__ = "user_session"

    #: "customer" or "staff" — which table ``subject_id`` points into.
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# =============================================================================
# The catalog: services and their packages
# =============================================================================
class Service(Base, UUIDPk, Timestamped):
    """One OdooBridger microservice, e.g. the biometric connector."""

    __tablename__ = "service"

    #: Short id used everywhere: token audience, URLs, Stripe metadata. "biometric".
    slug: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    tagline: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    #: Where users open it, e.g. https://biometric.odoobridger.com
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Where the core reaches it server to server (often the same; inside
    #: Docker it is the container address, e.g. http://biobridge:8000).
    internal_url: Mapped[str | None] = mapped_column(String(255))
    #: Path on the service that receives the SSO code.
    sso_callback_path: Mapped[str] = mapped_column(String(120), default="/app/#/sso")
    #: The service proves itself with this when it exchanges an SSO code
    #: (hash only; the clear secret is shown once when set).
    client_secret_hash: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Sold on the website and addable from the portal. An inactive or unlisted
    #: service can still be assigned by staff.
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Works on the account's Odoo: it is sent the Odoo connection it is bound
    #: to. A service without this never receives Odoo credentials.
    uses_odoo: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    packages: Mapped[list["ServicePackage"]] = relationship(
        back_populates="service", cascade="all, delete-orphan",
        order_by="ServicePackage.sort_order")

    @property
    def api_url(self) -> str:
        return (self.internal_url or self.base_url).rstrip("/")


class ServicePackage(Base, UUIDPk, Timestamped):
    """A tier of one service: its price and what it allows.

    ``entitlements`` is handed to the service verbatim on provisioning; the
    core does not interpret it. For the biometric service the keys are
    ``max_devices``, ``max_employees`` and ``min_sync_interval_minutes``
    (null = unlimited).
    """

    __tablename__ = "service_package"
    __table_args__ = (UniqueConstraint("service_id", "name", name="uq_package_name"),)

    service_id: Mapped[str] = mapped_column(ForeignKey("service.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(240))
    monthly_price_cents: Mapped[int | None] = mapped_column(Integer)
    yearly_price_cents: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="usd")
    stripe_price_id: Mapped[str | None] = mapped_column(String(80), index=True)
    stripe_yearly_price_id: Mapped[str | None] = mapped_column(String(80), index=True)
    entitlements: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Feature bullets for the pricing page.
    features: Mapped[list] = mapped_column(JSON, default=list)
    trial_days: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    service: Mapped[Service] = relationship(back_populates="packages")

    def price_id_for(self, interval: str) -> str | None:
        return self.stripe_yearly_price_id if interval == "year" else self.stripe_price_id

    def price_cents_for(self, interval: str) -> int | None:
        return self.yearly_price_cents if interval == "year" else self.monthly_price_cents


class ServiceStatus(str, enum.Enum):
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    suspended = "suspended"
    cancelled = "cancelled"


#: The statuses in which a service should serve the account.
USABLE_STATUSES = frozenset({ServiceStatus.trialing.value, ServiceStatus.active.value})


class AccountService(Base, UUIDPk, Timestamped):
    """An account's subscription to one service."""

    __tablename__ = "account_service"
    __table_args__ = (UniqueConstraint("account_id", "service_id", name="uq_account_service"),)

    account_id: Mapped[str] = mapped_column(ForeignKey("account.id", ondelete="CASCADE"), index=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("service.id", ondelete="RESTRICT"), index=True)
    package_id: Mapped[str | None] = mapped_column(
        ForeignKey("service_package.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(20), default=ServiceStatus.trialing.value)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The item on the account's Stripe subscription that bills this service.
    stripe_item_id: Mapped[str | None] = mapped_column(String(64), index=True)
    #: Staff exceptions merged over the package's entitlements ("Growth, but 8
    #: devices"). Kept across package changes.
    entitlement_overrides: Mapped[dict | None] = mapped_column(JSON)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: A package change the customer asked to make at the next renewal, and when it takes effect.
    pending_package_id: Mapped[str | None] = mapped_column(
        ForeignKey("service_package.id", ondelete="SET NULL"))
    pending_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- provisioning: what the service last acknowledged ---------------------
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provision_error: Mapped[str | None] = mapped_column(String(400))
    #: Bumped on every change; the service is told until it has acknowledged
    #: the current version.
    revision: Mapped[int] = mapped_column(Integer, default=1)
    provisioned_revision: Mapped[int] = mapped_column(Integer, default=0)

    # --- Odoo: which connection the service works on, and whether it can ----
    odoo_connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("odoo_connection.id", ondelete="SET NULL"), index=True)
    #: The service's own readiness check on that Odoo, read from its usage
    #: endpoint: {"ready": bool, "message": str, "checks": [...], "auth_failed": bool}.
    odoo_readiness: Mapped[dict | None] = mapped_column(JSON)
    odoo_readiness_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship(back_populates="services")
    service: Mapped[Service] = relationship()
    package: Mapped[ServicePackage | None] = relationship(foreign_keys=[package_id])
    pending_package: Mapped[ServicePackage | None] = relationship(foreign_keys=[pending_package_id])
    odoo_connection: Mapped["OdooConnection | None"] = relationship()

    @property
    def usable(self) -> bool:
        return self.status in USABLE_STATUSES

    def effective_entitlements(self) -> dict:
        merged = dict((self.package.entitlements if self.package else None) or {})
        merged.update(self.entitlement_overrides or {})
        return merged


# =============================================================================
# The customer's Odoo
# =============================================================================
class OdooConnectionStatus(str, enum.Enum):
    unverified = "unverified"
    connected = "connected"
    failed = "failed"


class OdooConnection(Base, UUIDPk, Timestamped):
    """Where the account's Odoo is and how to sign in to it.

    The core owns only the connection; each service keeps what it does with
    it (company switches, mappings, its own records in Odoo). A service bound
    to a connection is sent it on provisioning and calls Odoo directly.

    One Odoo database belongs to one account: ``url_key`` + ``db_key`` (and
    ``db_uuid`` when Odoo lets us read it) are unique across all accounts.
    """

    __tablename__ = "odoo_connection"
    __table_args__ = (
        UniqueConstraint("url_key", "db_key", name="uq_odoo_url_db"),
        UniqueConstraint("db_uuid", name="uq_odoo_db_uuid"),
    )

    account_id: Mapped[str] = mapped_column(ForeignKey("account.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="Odoo")
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    db_name: Mapped[str] = mapped_column(String(120), nullable=False)
    login: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    #: Normalised host + path (no scheme, no trailing slash, lower case).
    url_key: Mapped[str] = mapped_column(String(500), nullable=False)
    db_key: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Odoo's own database.uuid, when the user may read it.
    db_uuid: Mapped[str | None] = mapped_column(String(64))

    odoo_version: Mapped[str | None] = mapped_column(String(32))
    #: "json2" or "xmlrpc", as detected.
    api: Mapped[str | None] = mapped_column(String(10))
    #: [{"id", "name"}] the Odoo user can see, from the last good test.
    companies: Mapped[list | None] = mapped_column(JSON)
    #: Optional: when the API key expires (Odoo 19+ keys have one).
    key_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    key_warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String(20), default=OdooConnectionStatus.unverified.value)
    status_message: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When the customer was last emailed that it stopped working.
    failure_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When it last went from working to failing (cleared when it works again).
    failing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship(back_populates="odoo_connections")


# =============================================================================
# SSO
# =============================================================================
class SsoCode(Base, UUIDPk, Timestamped):
    """A one-time code handed to a service through the browser, exchanged by
    the service (server to server) for a signed identity token."""

    __tablename__ = "sso_code"

    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    service_id: Mapped[str] = mapped_column(ForeignKey("service.id", ondelete="CASCADE"))
    redirect_uri: Mapped[str] = mapped_column(String(500))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# =============================================================================
# Leads
# =============================================================================
LEAD_STAGES = ("new", "contacted", "qualified", "won", "lost")
OPEN_STAGES = ("new", "contacted", "qualified")
#: Demo is a sub-stage of "contacted", not a stage of its own.
DEMO_STATUSES = ("pending", "scheduled", "completed")


class Lead(Base, UUIDPk, Timestamped):
    __tablename__ = "lead"
    __table_args__ = (Index("ix_lead_stage_created", "stage", "created_at"),)

    #: "Demo" or "Sales question".
    topic: Mapped[str] = mapped_column(String(40), default="Demo")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    company: Mapped[str] = mapped_column(String(160), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40))
    country: Mapped[str | None] = mapped_column(String(80))
    #: Which services they asked about (slugs).
    services: Mapped[list] = mapped_column(JSON, default=list)
    #: Anything else the form asked — Odoo version, hosting, biometric system …
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    message: Mapped[str | None] = mapped_column(Text)
    preferred_date: Mapped[str | None] = mapped_column(String(10))
    preferred_window: Mapped[str | None] = mapped_column(String(20))
    timezone: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(20), default="website")   # website | staff
    ip: Mapped[str | None] = mapped_column(String(64))

    stage: Mapped[str] = mapped_column(String(20), default="new", server_default="new")
    stage_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    demo_status: Mapped[str | None] = mapped_column(String(12))
    demo_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_reason: Mapped[str | None] = mapped_column(String(160))
    owner_staff_id: Mapped[str | None] = mapped_column(String(32))
    #: Expected monthly value, for the pipeline total.
    value_cents: Mapped[int | None] = mapped_column(Integer)
    #: The account it became, once won and converted.
    account_id: Mapped[str | None] = mapped_column(String(32), index=True)


class LeadEvent(Base, UUIDPk, Timestamped):
    """A lead's history: a stage move, a demo change or a note."""

    __tablename__ = "lead_event"

    lead_id: Mapped[str] = mapped_column(ForeignKey("lead.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))          # stage | demo | note | convert
    from_value: Mapped[str | None] = mapped_column(String(20))
    to_value: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(254))


# =============================================================================
# Bookkeeping
# =============================================================================
class StripeEvent(Base, Timestamped):
    """Stripe events already applied — webhooks are delivered at least once."""

    __tablename__ = "stripe_event"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    type: Mapped[str] = mapped_column(String(80))


class AuditLog(Base, UUIDPk, Timestamped):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_account_created", "account_id", "created_at"),)

    account_id: Mapped[str | None] = mapped_column(String(32))
    #: "staff:<email>", "user:<email>" or "system".
    actor: Mapped[str] = mapped_column(String(280))
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(64))


class AccountClosure(Base, UUIDPk, Timestamped):
    """What remains of a deleted account."""

    __tablename__ = "account_closure"

    account_id: Mapped[str] = mapped_column(String(32), index=True)
    account_name: Mapped[str] = mapped_column(String(160))
    owner_email: Mapped[str | None] = mapped_column(String(254))
    services: Mapped[list] = mapped_column(JSON, default=list)
    closed_by: Mapped[str] = mapped_column(String(280))
    reason: Mapped[str | None] = mapped_column(Text)
    #: When the account itself was created (for customer history charts).
    account_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MetricSnapshot(Base, Timestamped):
    """One row per day: the figures the staff overview compares against
    (monthly revenue has no history anywhere else)."""

    __tablename__ = "metric_snapshot"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    mrr_cents: Mapped[int] = mapped_column(Integer, default=0)
    customers: Mapped[int] = mapped_column(Integer, default=0)
    subscriptions: Mapped[int] = mapped_column(Integer, default=0)


class AlertState(Base, Timestamped):
    """One problem from Needs Attention, remembered across sweeps (and restarts)
    so staff are told once when it starts, reminded while it lasts, and told
    when it clears."""

    __tablename__ = "alert_state"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    severity: Mapped[str] = mapped_column(String(10))
    issue: Mapped[str] = mapped_column(String(240))
    detail: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(String(32))
    account: Mapped[str | None] = mapped_column(String(160))
    service: Mapped[str | None] = mapped_column(String(80))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set when it cleared; the row stays as history until it comes back.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base, Timestamped):
    """Key/value settings staff change in the console (and the signing key)."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    updated_by: Mapped[str | None] = mapped_column(String(254))

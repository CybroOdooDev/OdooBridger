"""OdooBridger core settings, read from the environment (and .env)."""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "OdooBridger"
    environment: str = "development"
    debug: bool = False

    #: Where the core itself is reached (portal, staff console, SSO, API),
    #: e.g. https://app.odoobridger.com. The issuer of every token it signs.
    public_base_url: str = "http://localhost:8000"
    #: The marketing site. Empty = served by the core at "/".
    site_url: str = ""

    database_url: str = "sqlite:///./odoobridger.db"

    # --- sessions on the core itself (HS256, never leave the core) ----------
    jwt_secret: str = Field(default="dev-only-change-me-at-least-32-characters-long")
    access_token_ttl_minutes: int = 60 * 8
    refresh_token_ttl_days: int = 30
    staff_access_token_ttl_minutes: int = 60
    staff_refresh_token_ttl_days: int = 1
    max_failed_logins: int = 8
    lockout_minutes: int = 15

    # --- tokens the core signs for services (RS256) --------------------------
    #: PEM private key. Empty: one is generated on first boot and kept in the
    #: database (fine for one core instance; set it explicitly when you run more).
    signing_key_pem: str = ""
    #: Lifetime of the identity token a service receives at SSO, and of the
    #: provisioning calls the core makes to services.
    service_token_ttl_seconds: int = 300
    sso_code_ttl_seconds: int = 60

    #: Secrets stored in the database (Stripe keys, SMTP password) are
    #: encrypted with a key derived from this.
    master_encryption_key: str = Field(default="dev-only-master-key-not-for-production")

    # --- staff ---------------------------------------------------------------
    #: A fixed staff login, created/updated at boot. For hosts with no shell.
    staff_email: str = ""
    staff_password: str = ""

    # --- trials and the subscription sweep -----------------------------------
    default_trial_days: int = 14
    sweep_interval_seconds: int = 300
    #: How often the sweep asks services whether they can still work on the customer's Odoo.
    odoo_readiness_minutes: int = 15
    #: OdooBridger Agent relay: the largest request or answer carried (bytes),
    #: and how long a request through an agent may take.
    relay_max_body_bytes: int = 10_000_000
    relay_timeout_seconds: int = 60
    #: May a customer's Odoo address point into a private network (10.x,
    #: 192.168.x, localhost)? Unset: allowed outside production only. In
    #: production the core would otherwise test-connect to internal hosts.
    odoo_allow_private_targets: bool | None = None

    @field_validator("odoo_allow_private_targets", mode="before")
    @classmethod
    def _blank_is_unset(cls, value):
        return None if isinstance(value, str) and not value.strip() else value
    run_background_jobs: bool = True

    # --- email ---------------------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    mail_from: str = "no-reply@odoobridger.com"
    sales_email: str = ""
    # Company details shown beside the contact form; empty ones are not shown.
    company_name: str = "Cybrosys Technologies"
    contact_email: str = ""
    contact_phone: str = ""
    contact_address: str = ""
    contact_hours: str = ""
    company_website: str = ""
    email_verification_ttl_hours: int = 48
    password_reset_ttl_minutes: int = 60

    # --- Stripe --------------------------------------------------------------
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_api_base: str = "https://api.stripe.com"

    # --- the biometric service, registered at boot when its secret is set -----
    #: Shared with BioBridge's PLATFORM_CLIENT_SECRET. Empty: register the
    #: service from the staff console instead.
    biometric_client_secret: str = ""
    biometric_base_url: str = "http://localhost:8001"
    biometric_internal_url: str = ""
    # --- the demo service (Team Desk), registered at boot when its secret is set
    demo_client_secret: str = ""
    demo_base_url: str = "http://localhost:8002"
    demo_internal_url: str = ""

    http_timeout_seconds: int = 20
    registration_rate_per_hour: int = 10
    contact_rate_per_hour: int = 5
    cors_origins: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def base(self) -> str:
        return self.public_base_url.rstrip("/")

    @property
    def site(self) -> str:
        return (self.site_url or self.public_base_url).rstrip("/")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()

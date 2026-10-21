# OdooBridger

One platform for Odoo integrations: customers have **one account, one login and
one invoice**, and each integration runs as its own service on its own
subdomain. The first service is **BioBridge** (biometric attendance).

```
odoobridger/
├── core/                 OdooBridger core (FastAPI, package `obcore`)
│   ├── obcore/           accounts, SSO, billing, leads, staff console, website
│   └── tests/            core tests (fake Stripe, fake service)
├── services/
│   └── biobridge/        the biometric service — BioBridge, with platform mode
├── tests_e2e/            core + BioBridge in one process, the whole journey
└── deploy/               docker compose, Caddy (three hostnames), .env example
```

## What lives where

| | Core (`app.odoobridger.com`) | BioBridge (`biometric.odoobridger.com`) |
|---|---|---|
| Website, pricing, contact form | ✔ (`/`, `/site/…`) | — |
| Signup, email confirmation, passwords | ✔ | standalone mode only |
| Customer portal: services, people, billing | ✔ (`/app/`) | — |
| Staff console: accounts, payments, leads, catalog, staff | ✔ (`/staff/`) | — |
| Stripe: one subscription per account, an item per service | ✔ | standalone mode only |
| Odoo + biometric connections, punches, attendance, sync | — | ✔ |
| Service operations (scheduler, diagnostics, run a sync) | — | ✔ its own staff console |

## How the core and a service talk

All core → service tokens are **RS256**, signed with the core's key; services
verify them with the public key (`/.well-known/jwks.json` or
`/api/v1/sso/public-key`) and never hold a secret that could mint one.

**Provisioning** — whenever anything about an account's use of a service changes
(signup, package, trial end, payment, suspension, people), the core PUTs the
whole state to `PUT {service}/platform/v1/accounts/{account_id}`:

```json
{"account": {"id", "name", "slug", "timezone", "country"},
 "status": "trialing|active|past_due|suspended|cancelled",
 "package": {"id", "name"}, "entitlements": {"max_devices": 10, …},
 "trial_ends_at", "renews_at", "users": [{"id", "email", "full_name", "role"}],
 "revision": 7}
```

The bearer token carries `typ=provision`, `aud=<service slug>`, the
`account_id` and the SHA-256 of the exact body. Each change bumps a revision;
anything a service has not acknowledged is sent again by the core's background
sweep, and a service ignores an older revision than the one it has. Deleting an
account sends `DELETE …/accounts/{id}` to every service first — if one can't
delete its data, the account is not deleted (staff can force it).
`GET …/accounts/{id}/usage` gives the staff console the service's own numbers.

**SSO** — the portal's "Open" button, or a service sending a signed-out visitor
to `/app/#/open/<slug>?redirect_uri=…&state=…`:

1. the core checks the account has the service and the redirect is the
   service's own origin, and sends the browser back with a one-time code (60 s);
2. the service's backend trades the code plus its **client secret** at
   `POST /api/v1/sso/token` for an identity token (`typ=identity`): user, role,
   account, status and entitlements;
3. the service creates/updates its tenant and user from it and opens its own
   session. The identity token never passes through the browser.

## Billing

* A service added without online payment starts a **trial** (package trial days,
  else `DEFAULT_TRIAL_DAYS`). When it ends unpaid it becomes `past_due` and the
  service stops working (data kept).
* **Add payment details** opens one Stripe Checkout for every unbilled service,
  monthly or yearly (Stripe needs one interval per subscription, so the interval
  is per account).
* Once paying, adding a service adds a subscription item (prorated), changing a
  package changes the item's price (prorated), removing a service deletes its
  item — removing the last one cancels at the end of the paid period instead.
* Webhooks (`/api/v1/billing/webhook`: `checkout.session.completed`,
  `customer.subscription.*`, `invoice.*`) re-read the subscription and map it on
  to the account's services. `suspended` belongs to staff and Stripe never
  changes it. Stripe keys can be set in the staff console (stored encrypted) or
  in the environment; each package needs its Stripe price ids.

## BioBridge in platform mode

`PLATFORM_MODE=true` turns BioBridge into OdooBridger's biometric service
(standalone mode is unchanged and still the default):

* sign-in goes through the core (the dashboard redirects; `#/sso` finishes it);
  its own signup, password login, resets, plans, Stripe billing, account
  deletion and website endpoints answer 404;
* package entitlements land on the tenant's existing per-account limits
  (`max_devices`, `max_employees`, `min_sync_interval_minutes`), so everything
  already enforced keeps working; status comes from the core;
* core roles map owner → owner, admin → admin, member → viewer; people removed
  in the core can no longer sign in;
* Settings → Billing shows the package and links to the portal; the sidebar has
  "All Services & Billing";
* BioBridge's own staff console still works for service operations; status,
  plan and limit changes there are refused (they belong to the core).

Settings: `PLATFORM_MODE`, `PLATFORM_CORE_URL`, `PLATFORM_CORE_INTERNAL_URL`,
`PLATFORM_SERVICE_SLUG` (default `biometric`), `PLATFORM_CLIENT_SECRET`,
`PLATFORM_PUBLIC_KEY_PEM` (optional; fetched from the core otherwise). Database:
migration `0012` adds `tenant.platform_account_id`, `tenant.platform_state`,
`app_user.platform_user_id` (`python3 tools/db_upgrade.py`).

## Running it

```bash
python3 deploy/make-env.py              # writes deploy/.env with random secrets + a staff login
docker compose -f deploy/docker-compose.yml up -d --build
```

Website and portal on http://localhost:8000 (`/app/`, `/staff/`), BioBridge on
http://localhost:8001. The core registers the biometric service at boot from
`BIOMETRIC_CLIENT_SECRET` with three starter packages (edit them in Staff →
Services & Packages, and add Stripe price ids). Production:
`-f deploy/docker-compose.prod.yml` puts Caddy in front with HTTPS for
`SITE_DOMAIN`, `CORE_DOMAIN` and `BIOMETRIC_DOMAIN`, plus the plain-HTTP ADMS
port for ZKTeco push terminals.

Without `SMTP_HOST` emails are written to the core's log (handy locally: the
set-password link is there).

## Adding another service

1. Build it as its own app. Implement `PUT/DELETE /platform/v1/accounts/{id}`
   and `GET …/usage`, verifying the core's RS256 token (`aud` = your slug,
   `account_id`, `body_sha256`); and an SSO landing page that posts the code to
   `/api/v1/sso/token` with your client secret.
2. Staff → Services & Packages → New Service (slug, public URL, internal URL,
   callback path). Copy the client secret shown once.
3. Add packages with the `entitlements` your service understands, and their
   Stripe prices. It appears on the website, signup and the portal.

## Tests

```bash
cd core && python -m pytest                 # core: accounts, SSO, billing, staff, leads
cd services/biobridge && python -m pytest   # BioBridge, incl. tests/test_platform_mode.py
python -m pytest                            # (from the root) end to end: core + BioBridge
```

## Not done yet

* The core creates its tables at boot (`create_all`); add Alembic before the
  first schema change on a live database.
* BioBridge's own website and its staff console's plans/leads/Stripe pages still
  exist for standalone mode; in platform mode their endpoints are switched off
  but the console menu items still show.
* Email server settings for the core are environment-only (no console page yet).
* Not yet run against live Stripe (tested with a fake Stripe) or on a real
  deployment of the compose files.

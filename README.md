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
├── mock-biotime/         a stand-in BioTime server in one Docker image, for development
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

**Signup** — the website form creates nothing but a pending signup and an
email. The account, its owner, its services (so the trial starts then) and the
provisioning to services happen only when the link is opened and a password
chosen; "Buy Now" goes on to Stripe Checkout at that point. Signing up again
replaces the pending one with a new link; unconfirmed signups expire
(`EMAIL_VERIFICATION_TTL_HOURS`) and the sweep clears them.

**The customer's Odoo** — set once per account in the portal (Odoo page) and
used by every service that works on Odoo (`Service.uses_odoo`). The core owns
the connection itself: address, database, login, API key (encrypted), version,
API flavour, company list, status and an optional key expiry date. Each service
keeps what it does with it (BioBridge: company switches, mappings, the records
it writes).

* Saving runs the core's test first (reachable, signs in, the key belongs to
  that login, companies, Odoo's own `database.uuid` when readable); a failing
  test saves nothing. Odoo 19+ is tested over JSON-2, older Odoo over XML-RPC.
* One Odoo database belongs to one account: the same URL + database (any
  spelling: http/https, `www.`, trailing slash, `/odoo`) or the same
  `database.uuid` is refused for a second account.
* Odoo services get it in the provisioning PUT as
  `"odoo": {"id", "name", "url", "db", "login", "api_key", "version", "api",
  "companies", "status"}` (or `null` once disconnected). A service that doesn't
  use Odoo never receives the key; the SSO identity token never carries it.
  Services call Odoo themselves with their copy, so syncs never wait on the core.
* Readiness comes back through `GET …/usage` as `"odoo": {"ready", "message",
  "checks", "auth_failed"}` (`?check_odoo=1` asks for a live check). The
  portal shows it per service; `auth_failed` marks the connection failed and
  the account's managers are emailed once. A known key expiry is warned
  14 days ahead.
* In production the core refuses Odoo addresses in private networks
  (`ODOO_ALLOW_PRIVATE_TARGETS`), so a customer can't make it call internal hosts.
  BioBridge in platform mode does the same by default (`ALLOW_PRIVATE_NETWORK_TARGETS`).
* **A private Odoo or biometric devices** (on-premise, behind NAT) are reached
  the way any public server is: the customer sets up a public IP or domain
  with the port forwarded, and enters it as the address. There is no relay or
  agent. A ZKTeco terminal that can't be forwarded can use the *Cloud push
  device* option instead, which dials out by itself.

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
  plan and limit changes there are refused (they belong to the core);
* the Odoo connection comes from the core: the provisioned copy is stored
  encrypted with the tenant's key and marked managed. An existing connection to
  the same Odoo database is taken over (keeping its company switches, mappings
  and history); any other active one is switched off. Address, database, login
  and key are read-only in Settings → Odoo Connection; the company switches are
  still set there. A sync that finds the key refused reads the connection
  again once (a new key may have just arrived) before failing.

Settings: `PLATFORM_MODE`, `PLATFORM_CORE_URL`, `PLATFORM_CORE_INTERNAL_URL`,
`PLATFORM_SERVICE_SLUG` (default `biometric`), `PLATFORM_CLIENT_SECRET`,
`PLATFORM_PUBLIC_KEY_PEM` (optional; fetched from the core otherwise). Database:
migration `0012` adds `tenant.platform_account_id`, `tenant.platform_state`,
`app_user.platform_user_id`; `0013` adds `odoo_connection.platform_connection_id`
(`python3 tools/db_upgrade.py`).

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

## Database migrations (core)

The core's schema is managed by Alembic (`core/alembic/`, like BioBridge). At
every start it brings the database to the latest revision
(`obcore/migrations.py`, under a Postgres advisory lock so instances take
turns); `python3 core/tools/db_upgrade.py [--check]` does the same by hand.

* A new database is built by the migrations.
* A database made before migrations (Oct 28, 2026) is adopted once,
  automatically: missing tables and columns are added the old way, the
  agent's leftover table and column are dropped, it is checked against the
  models and stamped at the latest revision.

Making a schema change:

1. edit the model;
2. `cd core && alembic revision --autogenerate -m "what changed"`;
3. read and correct the file it wrote (autogenerate is a draft);
4. keep it compatible with the previous release (add now, drop later): a
   rollback doesn't undo a migration.

CI builds a Postgres database from the migrations and runs `alembic check`,
which fails when a model changed without a migration.

## Continuous deployment

`.github/workflows/ci-cd.yml` runs the core, BioBridge and end-to-end tests on
every push and pull request. On `main`, once they pass, it builds both images,
pushes them to GitHub Container Registry as
`ghcr.io/<owner>/odoobridger-core:<commit>` and `…/odoobridger-biobridge:<commit>`
(plus `latest`), and deploys them over SSH with `deploy/deploy.sh`.

`deploy.sh` pulls that release, restarts the stack with the production compose,
waits until both containers are healthy, and records it in `deploy/.release`. If
they don't come up healthy it puts the previous release back and fails the run.
To roll back on purpose: Actions → CI/CD → Run workflow → an earlier commit SHA.
A BioBridge release that migrated its database is not undone by a rollback, so
keep migrations backwards compatible (add columns, don't drop them in the same
release that stops using them).

One-time server setup:

1. A Linux server with Docker and the compose plugin; a user in the `docker` group.
2. `mkdir -p /opt/odoobridger/deploy`, then put `deploy/.env` there (from
   `.env.example` / `make-env.py`, with the production domains). It never leaves the server.
3. Repository secrets: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY` (that
   user's private key), `DEPLOY_PATH` (`/opt/odoobridger`), optionally `DEPLOY_PORT`.
4. Optionally add required reviewers to the `production` environment so each
   deploy waits for approval.

The workflow logs the server in to ghcr.io with its own short-lived token for
each deploy, so the packages can stay private.

## Adding another service

1. Build it as its own app. Implement `PUT/DELETE /platform/v1/accounts/{id}`
   and `GET …/usage`, verifying the core's RS256 token (`aud` = your slug,
   `account_id`, `body_sha256`); and an SSO landing page that posts the code to
   `/api/v1/sso/token` with your client secret.
2. Staff → Services & Packages → New Service (slug, public URL, internal URL,
   callback path). Copy the client secret shown once.
3. Add packages with the `entitlements` your service understands, and their
   Stripe prices. It appears on the website, signup and the portal.
4. If it works on the customer's Odoo, tick "Works on the customer's Odoo".
   Read the `odoo` block from the provisioning PUT, and report your own
   readiness (`ready`, `message`, `checks`, `auth_failed`) under `odoo` in
   `GET …/usage`.

## Tests

```bash
cd core && python -m pytest                 # core: accounts, SSO, billing, staff, leads
cd services/biobridge && python -m pytest   # BioBridge, incl. tests/test_platform_mode.py
python -m pytest                            # (from the root) end to end: core + BioBridge
```

## Not done yet

* One Odoo connection per account for now (the model, API and provisioning
  already carry a connection id, so more can be allowed later). No per-service
  key override yet.
* BioBridge's own website and its staff console's plans/leads/Stripe pages still
  exist for standalone mode; in platform mode their endpoints are switched off
  but the console menu items still show.
* Email server settings for the core are environment-only (no console page yet).
* Not yet run against live Stripe (tested with a fake Stripe) or on a real
  deployment of the compose files.

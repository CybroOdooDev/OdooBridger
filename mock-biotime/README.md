# Mock BioTime

A stand-in **ZKTeco BioTime** server for development and demos. Nothing to
install but Docker: no hardware, no BioTime licence, no BioBridge database, no
tenant, no Odoo. It answers the same API BioBridge's BioTime connector calls,
with sample people and a week of punches, and clocks someone in or out every
20 seconds so a sync always finds something new.

Standard library only (one file, `server.py`); the image adds `tzdata`.

## Run it

```bash
cd mock-biotime
docker compose up -d --build
curl localhost:8100/mock/state          # who is in or out, what is configured
docker compose logs -f                  # a line per request and per simulated punch
```

or without compose:

```bash
docker build -t mock-biotime .
docker run -d --name mock-biotime -p 8100:8100 -v mockdata:/data mock-biotime
```

or with no Docker at all: `python server.py --data-dir ./data` (Python 3.9+).

Inside the OdooBridger stack it is the `mock` profile, on the same network as
BioBridge:

```bash
docker compose -f deploy/docker-compose.yml --profile mock up -d --build mock-biotime
```

## Connect BioBridge to it

New BioTime connection:

| Field | Value |
|---|---|
| Server URL | `http://host.docker.internal:8100` (BioBridge in Docker, mock on this machine) · `http://localhost:8100` (BioBridge run directly) · `http://mock-biotime:8100` (both in the same compose stack) |
| Username / Password | `admin` / `admin` |
| Auth Style | `token` or `jwt`, both work |
| Server Timezone | `Asia/Kolkata`, or whatever `MOCK_TZ` is. It must match: punch times are local wall-clock with no offset |

A hosted BioBridge refuses addresses on private networks. For a local test set
`BIOBRIDGE_ALLOW_PRIVATE_TARGETS=true` (it is `ALLOW_PRIVATE_NETWORK_TARGETS`
inside BioBridge).

## What is in it

Three terminals (`MOCK-GATE-01..03`), three departments, two areas and six
people, each there to exercise something:

| Badge | Person | Their punches |
|---|---|---|
| `1001` | Ahmed Sharma | 09:00 to 18:00 with a lunch break (break-out and break-in punches) |
| `0042` | Sara Tanaka | 08:30 to 17:30; badge with a leading zero. Her last check-out came in **late** (an old punch time, a new upload time and id) |
| `A7` | Jane Haddad | no check-out every third day (an open shift) |
| `2001` | Liam Okafor | **night shift**, in 21:00, out 05:00 the next morning |
| `2002` | Priya Nakamura | taps the reader twice a few seconds apart (duplicates) |
| `2003` | Noah Fernandes | has left: attendance switched off, no punches |

The history is written the first time the server starts, for the last
`MOCK_SEED_DAYS` weekdays, and never reaches into the future. After that the
live simulator adds a punch every `MOCK_INTERVAL` seconds.

Your own people instead: write a JSON file with any of `departments`, `areas`,
`terminals` and `employees` (same shapes as BioTime returns), mount it and set
`MOCK_FIXTURES`. A server with your fixtures starts with no punches; add them
with `/mock/punch`.

## The BioTime API it serves

| | |
|---|---|
| `POST /api-token-auth/`, `POST /jwt-api-token-auth/` | `{username, password}` gives `{"token"}`; send `Authorization: Token <t>` or `JWT <t>`. A wrong login is a 400, as in BioTime |
| `GET /iclock/api/transactions/` | `page`, `page_size`, `emp_code`, `terminal_sn`, `start_time`, `end_time` (inclusive, server local time), `upload_time` |
| `GET /iclock/api/transactions/<id>/` | one transaction, 404 once deleted |
| `GET /iclock/api/terminals/` | |
| `GET /personnel/api/employees/` | `emp_code` filter; `POST` creates one (needs `department` and `area`, a duplicate `emp_code` is a 400, as BioTime does) |
| `GET /personnel/api/departments/`, `/areas/` | |

Lists are paginated the Django REST way: `count`, an absolute `next` URL,
`previous`, `data`. Punch states: `0` check in, `1` check out, `2` break out,
`3` break in, `4` overtime in, `5` overtime out.

## Controls (no token)

```bash
# Clock someone in/out now. With no state it toggles; states: in out break-out break-in overtime-in overtime-out 0..5
curl -X POST localhost:8100/mock/punch -d '{"emp_code": "1001"}'
curl -X POST localhost:8100/mock/punch -d '{"emp_code": "A7", "state": "out", "terminal": "MOCK-GATE-02"}'

# A late upload: an old punch time, received just now (the time window misses it; upload_time and the id read find it)
curl -X POST localhost:8100/mock/punch -d '{"emp_code": "0042", "state": "out", "at": "2026-10-28 17:35:00"}'

# Delete a transaction on the server: leaves a gap in the ids
curl -X DELETE localhost:8100/mock/transactions/1793350701

# Fail every API call for a while (liveness stays up)
curl -X POST localhost:8100/mock/outage -d '{"seconds": 60, "status": 503}'

# Start again: the data is rebuilt, the ids keep growing
curl -X POST localhost:8100/mock/reset -d '{}'
```

`GET /health` is the Docker health check.

## Settings

Environment variables (or the matching `--flags`; `python server.py --help`).

| Variable | Default | |
|---|---|---|
| `MOCK_TZ` | `Asia/Kolkata` | timezone of the punch times |
| `MOCK_USERNAME` / `MOCK_PASSWORD` | `admin` / `admin` | `MOCK_AUTH=any` accepts every login |
| `MOCK_INTERVAL` | `20` | seconds between simulated punches; `0` for none |
| `MOCK_SEED_DAYS` | `7` | days of history written on first start |
| `MOCK_MAX_PAGE_SIZE` | `100` | a small one (`2`) makes every sync paginate |
| `MOCK_TOKEN_TTL` | `0` | seconds a token lasts; `30` exercises re-login mid-sync |
| `MOCK_UPLOAD_FILTER` | `from` | how `?upload_time=` behaves: `from` (that time on), `exact` (that second only), `ignore`. Real servers differ and BioBridge detects which |
| `MOCK_FIXTURES` | | path of your own data file |
| `MOCK_RESET` | `0` | `1` throws away saved data at start |
| `MOCK_PORT` | `8100` | the port inside the container (compose publishes it with `MOCK_HOST_PORT`) |
| `MOCK_DATA_DIR` | `/data` | where `state.json` lives |

## Good to know

- **Ids never go backwards.** They start at the clock, grow on every punch, and
  survive restarts and resets, because a client that remembers "I have
  everything up to id N" would skip a punch numbered below N. Keep `/data` on a
  volume. A bind mount must be writable by uid 10001 (or run with
  `user: "${UID}:${GID}"`).
- Wiping the volume starts the ids from the clock again, so they are still
  higher than before.
- It is for development. There is no TLS, no rate limit and the controls have no
  login: do not expose the port to the internet.

## Tests

```bash
pip install pytest
python -m pytest tests -q
```

`test_server.py` drives the API over HTTP. `test_with_biobridge.py` runs
BioBridge's real BioTime connector against the mock (login, paging, the late
upload, id gaps, creating an employee) and is skipped when BioBridge is not
next to this folder.

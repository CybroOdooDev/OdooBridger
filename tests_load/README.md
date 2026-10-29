# Concurrency and synchronisation checks

`concurrency_check.py` drives a real core and BioBridge, each with several
worker processes on PostgreSQL, with many users at once, then reads both
databases to check that nothing was duplicated, lost or mixed up.

## Run it

Needs a PostgreSQL server you can create databases on.

```bash
PGHOST=localhost PGPORT=5432 PGUSER=postgres tests_load/run_local.sh 30
```

`run_local.sh` creates the `ob_load_core` and `ob_load_bb` databases (dropping
them first), starts:
- a fake BioTime (`fake_biotime.py`);
- the core on :8200;
- BioBridge on :8201, with 4 workers each (`WORKERS=` to change).

It then runs the checks for 30 accounts and stops everything. Exit code 0
means every check passed. The logs' folder is printed at the end.

## What is checked

| Area | Fired at once | Must hold |
|---|---|---|
| Signup | N signups; one email signed up 9 times | one pending signup per email; one of the 9 links works |
| Confirm | every confirmation link | one user per email; each session sees its own account |
| Add service | each account adds BioBridge 6 times | one add wins (the rest 400/409); one row per account |
| Provisioning | 7 plan changes per account | every account reaches BioBridge once; BioBridge ends on the core's final plan; the core records it acknowledged |
| SSO | everyone opens BioBridge; one code used 5 times | each lands in its own account; the code works once |
| Isolation | every account adds a BioTime connection | each sees only its own |
| Sync | per account 4 × Sync Now, 1 inline sync, 3 × Import Terminals | one cycle at a time per account; every punch stored once and under its own account; 2 terminals per connection |
| ADMS push | one terminal sends the same 40 punches 10 times | 40 punches stored |

A 503 ("busy, retry shortly") is allowed. It is what the apps answer when
every database connection of a process is in use. The checker retries it as
a browser user would, and reports how often that happened.

## Capacity

Each worker process has its own connection pool: `DB_POOL_SIZE` (10) plus
`DB_MAX_OVERFLOW` (20), waiting `DB_POOL_TIMEOUT` (30 s) before answering 503.
A process serves up to 40 requests at once, and a running sync holds two
connections. Under the bursts above (hundreds of requests in the same second
on 4 workers), a few hundred requests got a 503 and succeeded on retry.

For production, size `workers × (size + overflow)` to stay under PostgreSQL's
`max_connections` (100 by default).

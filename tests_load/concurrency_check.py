"""Many users at once: concurrency and synchronisation checks for OdooBridger.

Runs against a real core and BioBridge (several workers each, on PostgreSQL),
a fake BioTime server, and reads both databases to check the results. Each
check fires requests at the same time and then asserts what must hold:

  signup        many signups at once; the same email signed up several times
  confirm       every confirmation link used at once; one session each
  add-service   the same service added several times at once (one wins)
  provisioning  every account reaches BioBridge; plan changes fired at once
                converge on the core's final plan
  sso           everyone opens BioBridge at once and lands in their own
                account; a sign-in code used twice works once
  isolation     every account adds a BioTime connection at once and sees
                only its own
  sync          several syncs started at once per account (and across all
                accounts): one cycle at a time, no duplicate punches
  import        Import Terminals several times at once, during a sync
  adms          a push terminal sending the same punches several times at
                once stores each punch once

Usage (see tests_load/README.md for starting the stack):

  python tests_load/concurrency_check.py --accounts 30 \\
      --core http://127.0.0.1:8200 --biobridge http://127.0.0.1:8201 \\
      --core-log /path/core.log --biotime http://127.0.0.1:8799 \\
      --core-db "postgresql://postgres@/obc?host=/tmp/pg&port=5432" \\
      --bb-db   "postgresql://postgres@/bbc?host=/tmp/pg&port=5432"

Exit code 0 when every check passes.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
import psycopg

PASSWORD = "Load-Test-Pass-1"


@dataclass
class Account:
    email: str
    company: str
    core_token: str = ""
    account_id: str = ""
    bb_token: str = ""
    source_id: str = ""


@dataclass
class Report:
    results: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{f' — {detail}' if detail else ''}", flush=True)
        return ok


def statuses(responses) -> Counter:
    return Counter(r.status_code if isinstance(r, httpx.Response) else type(r).__name__ for r in responses)


def no_server_errors(responses) -> bool:
    """No 500s and no dropped connections. A 503 is the service saying it is
    busy (every database connection in use) and asking for a retry: allowed,
    and shown in the counts."""
    return all(isinstance(r, httpx.Response) and (r.status_code < 500 or r.status_code == 503) for r in responses)


def query(dsn: str, sql: str, params=()) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(sql, params).fetchall()


def link_tokens(log_path: str, email: str) -> list[str]:
    """Every link mailed to ``email``, newest last (the core logs mail when
    SMTP is off)."""
    text = open(log_path, encoding="utf-8", errors="replace").read()
    return [m.group(1) for m in re.finditer(r"to " + re.escape(email) + r":[^\n]*\n(?:[^\n]*\n){0,12}?[^\n]*?token=([A-Za-z0-9_\-]+)", text)]


async def gather(*aws):
    return await asyncio.gather(*aws, return_exceptions=True)


BUSY_RETRIES = Counter()


class RetryingClient(httpx.AsyncClient):
    """Behaves like a browser user: a 503 "busy, retry shortly" is retried
    (up to 3 times, after its Retry-After) instead of counted as an answer.
    How often that happened is reported at the end."""

    async def request(self, method, url, **kwargs):
        for attempt in range(4):
            response = await super().request(method, url, **kwargs)
            if response.status_code != 503 or attempt == 3:
                return response
            BUSY_RETRIES[f"{method} {httpx.URL(url).path.split('/api/v1')[-1][:40]}"] += 1
            await asyncio.sleep(float(response.headers.get("Retry-After") or 2))
        return response


# ---------------------------------------------------------------------------
async def check_signup(c: httpx.AsyncClient, args, rep: Report) -> list[Account]:
    print("\n[signup]")
    run = uuid.uuid4().hex[:6]
    accounts = [Account(email=f"load{run}{i}@example.com", company=f"Load {run} {i}") for i in range(args.accounts)]
    body = lambda a: {"company": a.company, "full_name": "Load User", "email": a.email,  # noqa: E731
                      "services": [], "mode": "trial"}
    t0 = time.monotonic()
    rs = await gather(*(c.post(f"{args.core}/api/v1/public/signup", json=body(a)) for a in accounts))
    rep.check(f"{len(accounts)} signups at once", Counter(r.status_code for r in rs if isinstance(r, httpx.Response))
              == Counter({201: len(accounts)}), f"{dict(statuses(rs))} in {time.monotonic() - t0:.1f}s")

    dup = accounts[0]
    rs = await gather(*(c.post(f"{args.core}/api/v1/public/signup", json=body(dup)) for _ in range(8)))
    rep.check("same email signed up 8 times at once: no server errors", no_server_errors(rs), str(dict(statuses(rs))))
    rows = query(args.core_db, "select count(*) from pending_signup where email = %s", (dup.email,))
    rep.check("same email: one pending signup row", rows[0][0] == 1, f"{rows[0][0]} rows")
    return accounts


async def check_confirm(c: httpx.AsyncClient, args, rep: Report, accounts: list[Account]) -> list[Account]:
    print("\n[confirm]")
    await asyncio.sleep(1)
    sent = {a.email: link_tokens(args.core_log, a.email) for a in accounts}
    missing = [e for e, t in sent.items() if not t]
    rep.check("every signup mailed a link", not missing, f"{len(missing)} missing" if missing else "")
    # The account signed up 9 times got 9 emails; only the link of the signup
    # that was stored last works, so it is tried with each until one does.
    dup = accounts[0]
    for token in reversed(sent.get(dup.email) or []):
        r = await c.post(f"{args.core}/api/v1/auth/link/use", json={"token": token, "password": PASSWORD})
        if r.status_code == 200:
            dup.core_token = r.json()["access_token"]
            break
    rep.check("same email signed up 9 times: one of its links works", bool(dup.core_token),
              f"{len(sent.get(dup.email) or [])} emails")
    live = [a for a in accounts[1:] if sent.get(a.email)]
    rs = await gather(*(c.post(f"{args.core}/api/v1/auth/link/use",
                               json={"token": sent[a.email][-1], "password": PASSWORD}) for a in live))
    for a, r in zip(live, rs):
        if isinstance(r, httpx.Response) and r.status_code == 200:
            a.core_token = r.json()["access_token"]
    rep.check(f"{len(live)} links used at once", all(a.core_token for a in live), str(dict(statuses(rs))))
    live = [a for a in accounts if a.core_token]
    ok = live

    # The same link twice at once: one account, never two.
    a = ok[0] if ok else None
    if a:
        users = query(args.core_db, "select count(*) from app_user where email = %s", (a.email,))
        rep.check("one user per confirmed email", users[0][0] == 1, f"{users[0][0]} users")
    dupes = query(args.core_db, "select email, count(*) from app_user group by email having count(*) > 1")
    rep.check("no duplicate users", not dupes, str(dupes[:3]))

    rs = await gather(*(c.get(f"{args.core}/api/v1/portal/account", headers={"Authorization": f"Bearer {a.core_token}"})
                        for a in ok))
    for a, r in zip(ok, rs):
        if isinstance(r, httpx.Response) and r.status_code == 200:
            a.account_id = r.json()["account"]["id"]
    wrong = [a.company for a, r in zip(ok, rs)
             if not (isinstance(r, httpx.Response) and r.status_code == 200 and r.json()["account"]["name"] == a.company)]
    rep.check("each session sees its own account", not wrong, f"{len(wrong)} wrong" if wrong else "")
    return ok


async def check_add_service(c, args, rep, accounts):
    print("\n[add-service]")
    tries = 6
    calls = [c.post(f"{args.core}/api/v1/portal/services", json={"service": "biometric"},
                    headers={"Authorization": f"Bearer {a.core_token}"}) for a in accounts for _ in range(tries)]
    rs = await gather(*calls)
    rep.check(f"{len(calls)} add-service calls at once: no server errors", no_server_errors(rs), str(dict(statuses(rs))))
    per = [rs[i * tries:(i + 1) * tries] for i in range(len(accounts))]
    winners = [sum(1 for r in group if isinstance(r, httpx.Response) and r.status_code == 201) for group in per]
    rep.check("exactly one add wins per account", all(w == 1 for w in winners), f"wins per account: {Counter(winners)}")
    rows = query(args.core_db, """select a.account_id, count(*) from account_service a join service s on s.id = a.service_id
                                  where s.slug = 'biometric' group by a.account_id having count(*) > 1""")
    rep.check("one account_service row per account", not rows, str(rows[:3]))


async def wait_until(fn, timeout=90, every=1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await fn():
            return True
        await asyncio.sleep(every)
    return False


async def check_provisioning(c, args, rep, accounts):
    print("\n[provisioning]")
    ids = [a.account_id for a in accounts]

    async def all_there():
        rows = query(args.bb_db, "select count(*) from tenant where platform_account_id = any(%s)", (ids,))
        return rows[0][0] == len(ids)

    ok = await wait_until(all_there, timeout=args.settle)
    rows = query(args.bb_db, "select count(*) from tenant where platform_account_id = any(%s)", (ids,))
    rep.check("every account reached BioBridge", ok, f"{rows[0][0]} of {len(ids)}")
    dupes = query(args.bb_db, "select platform_account_id, count(*) from tenant where platform_account_id is not null "
                              "group by platform_account_id having count(*) > 1")
    rep.check("one BioBridge tenant per account", not dupes, str(dupes[:3]))

    catalog = (await c.get(f"{args.core}/api/v1/public/catalog")).json()
    packages = next(s for s in catalog["services"] if s["slug"] == "biometric")["packages"]
    if len(packages) < 2:
        rep.check("plan changes at once converge", False, "needs two packages")
        return
    flips = [packages[i % len(packages)]["id"] for i in range(7)]
    calls = [c.patch(f"{args.core}/api/v1/portal/services/biometric", json={"package_id": pid},
                     headers={"Authorization": f"Bearer {a.core_token}"}) for a in accounts for pid in flips]
    rs = await gather(*calls)
    rep.check(f"{len(calls)} plan changes at once: no server errors", no_server_errors(rs), str(dict(statuses(rs))))

    async def converged():
        core = dict(query(args.core_db, """select a.account_id, p.name from account_service a
                     join service_package p on p.id = a.package_id join service s on s.id = a.service_id
                     where s.slug = 'biometric' and a.account_id = any(%s)""", (ids,)))
        bb = dict(query(args.bb_db, "select platform_account_id, platform_state->'package'->>'name' from tenant "
                                    "where platform_account_id = any(%s)", (ids,)))
        return all(bb.get(k) == v for k, v in core.items())

    ok = await wait_until(converged, timeout=args.settle)
    core = dict(query(args.core_db, """select a.account_id, p.name from account_service a
                 join service_package p on p.id = a.package_id join service s on s.id = a.service_id
                 where s.slug = 'biometric' and a.account_id = any(%s)""", (ids,)))
    bb = dict(query(args.bb_db, "select platform_account_id, platform_state->'package'->>'name' from tenant "
                                "where platform_account_id = any(%s)", (ids,)))
    off = [k for k, v in core.items() if bb.get(k) != v]
    rep.check("BioBridge ends on each account's final plan", ok, f"{len(off)} differ" if off else "")
    stale = query(args.core_db, """select count(*) from account_service a join service s on s.id = a.service_id
                   where s.slug = 'biometric' and a.account_id = any(%s) and a.provisioned_revision < a.revision""", (ids,))
    rep.check("core records every change as acknowledged", stale[0][0] == 0, f"{stale[0][0]} behind")


async def sso(c, args, a: Account) -> str | None:
    r = await c.post(f"{args.core}/api/v1/sso/authorize", json={"service": "biometric"},
                     headers={"Authorization": f"Bearer {a.core_token}"})
    if r.status_code != 200:
        return None
    code = re.search(r"code=([^&]+)", r.json()["redirect_url"]).group(1)
    return code


async def check_sso(c, args, rep, accounts):
    print("\n[sso]")
    codes = await gather(*(sso(c, args, a) for a in accounts))
    rs = await gather(*(c.post(f"{args.biobridge}/api/v1/auth/platform/sso", json={"code": code})
                        for code in codes if isinstance(code, str)))
    for a, r in zip([a for a, code in zip(accounts, codes) if isinstance(code, str)], rs):
        if isinstance(r, httpx.Response) and r.status_code == 200:
            a.bb_token = r.json()["access_token"]
    signed = [a for a in accounts if a.bb_token]
    rep.check(f"{len(accounts)} sign-ins to BioBridge at once", len(signed) == len(accounts), str(dict(statuses(rs))))
    rs = await gather(*(c.get(f"{args.biobridge}/api/v1/tenant", headers={"Authorization": f"Bearer {a.bb_token}"})
                        for a in signed))
    wrong = [a.company for a, r in zip(signed, rs)
             if not (isinstance(r, httpx.Response) and r.status_code == 200 and r.json()["name"] == a.company)]
    rep.check("each lands in its own BioBridge account", not wrong, f"{len(wrong)} wrong" if wrong else "")

    code = await sso(c, args, accounts[0])
    rs = await gather(*(c.post(f"{args.biobridge}/api/v1/auth/platform/sso", json={"code": code}) for _ in range(5)))
    wins = sum(1 for r in rs if isinstance(r, httpx.Response) and r.status_code == 200)
    rep.check("a sign-in code used 5 times at once works once", wins == 1 and no_server_errors(rs), str(dict(statuses(rs))))


async def check_isolation(c, args, rep, accounts):
    print("\n[isolation]")
    body = lambda a: {"name": f"BT {a.company}", "provider": "biotime", "base_url": args.biotime,  # noqa: E731
                      "username": "u", "password": "p", "server_timezone": "Asia/Kolkata"}
    rs = await gather(*(c.post(f"{args.biobridge}/api/v1/sources", json=body(a),
                               headers={"Authorization": f"Bearer {a.bb_token}"}) for a in accounts))
    for a, r in zip(accounts, rs):
        if isinstance(r, httpx.Response) and r.status_code == 201:
            a.source_id = r.json()["id"]
    rep.check(f"{len(accounts)} BioTime connections added at once", all(a.source_id for a in accounts),
              str(dict(statuses(rs))))
    rs = await gather(*(c.get(f"{args.biobridge}/api/v1/sources", headers={"Authorization": f"Bearer {a.bb_token}"})
                        for a in accounts))
    wrong = [a.company for a, r in zip(accounts, rs)
             if not (isinstance(r, httpx.Response) and [s["id"] for s in r.json()] == [a.source_id])]
    rep.check("each account sees only its own connection", not wrong, f"{len(wrong)} wrong" if wrong else "")


async def check_sync(c, args, rep, accounts):
    print("\n[sync]")
    head = lambda a: {"Authorization": f"Bearer {a.bb_token}"}  # noqa: E731
    calls = []
    for a in accounts:
        calls += [c.post(f"{args.biobridge}/api/v1/sync/start", headers=head(a)) for _ in range(4)]
        calls.append(c.post(f"{args.biobridge}/api/v1/sync/run-inline", headers=head(a)))
        calls += [c.post(f"{args.biobridge}/api/v1/sources/{a.source_id}/discover-devices", headers=head(a))
                  for _ in range(3)]
    t0 = time.monotonic()
    rs = await gather(*calls)
    rep.check(f"{len(calls)} syncs and imports at once across {len(accounts)} accounts: no server errors",
              no_server_errors(rs), f"{dict(statuses(rs))} in {time.monotonic() - t0:.1f}s")

    ids = [a.account_id for a in accounts]

    async def idle():
        rows = query(args.bb_db, """select count(*) from sync_run r join tenant t on t.id = r.tenant_id
                     where t.platform_account_id = any(%s) and r.status = 'running'""", (ids,))
        return rows[0][0] == 0

    rep.check("every sync finished", await wait_until(idle, timeout=args.settle))
    overlap = query(args.bb_db, """select t.platform_account_id, count(*) from sync_run a join sync_run b
                     on a.tenant_id = b.tenant_id and a.id < b.id
                     and a.started_at < coalesce(b.finished_at, now()) and b.started_at < coalesce(a.finished_at, now())
                     join tenant t on t.id = a.tenant_id where t.platform_account_id = any(%s)
                     group by t.platform_account_id""", (ids,))
    rep.check("never two sync cycles at once for one account", not overlap, f"{len(overlap)} accounts overlapped")
    dupes = query(args.bb_db, """select source_id, external_id, count(*) from punch_record
                   group by source_id, external_id having count(*) > 1""")
    rep.check("no punch stored twice", not dupes, str(dupes[:3]))
    per = query(args.bb_db, """select t.platform_account_id, count(p.id) from tenant t
                 left join punch_record p on p.tenant_id = t.id where t.platform_account_id = any(%s)
                 group by t.platform_account_id""", (ids,))
    counts = Counter(n for _, n in per)
    rep.check("every account has the same punches as its BioTime", len(counts) == 1 and next(iter(counts)) > 0,
              f"punch counts: {dict(counts)}")
    devs = query(args.bb_db, """select d.source_id, count(*) from device d join tenant t on t.id = d.tenant_id
                 where t.platform_account_id = any(%s) group by d.source_id""", (ids,))
    rep.check("every connection has exactly its 2 BioTime terminals",
              len(devs) == len(accounts) and all(n == 2 for _, n in devs), f"{Counter(n for _, n in devs)}")
    cross = query(args.bb_db, """select count(*) from punch_record p join device_source s on s.id = p.source_id
                   where s.tenant_id <> p.tenant_id""")
    rep.check("no punch filed under another account", cross[0][0] == 0, f"{cross[0][0]}")


async def check_adms(c, args, rep, accounts):
    print("\n[adms]")
    a = accounts[-1]
    serial = "LOAD" + uuid.uuid4().hex[:8].upper()
    r = await c.post(f"{args.biobridge}/api/v1/sources", headers={"Authorization": f"Bearer {a.bb_token}"}, json={
        "name": "Push door", "provider": "zk_adms", "connection_kind": "device",
        "base_url": f"adms://{serial}", "server_timezone": "Asia/Kolkata"})
    if not rep.check("push terminal added", r.status_code == 201, f"{r.status_code} {r.text[:120]}"):
        return
    await c.get(f"{args.biobridge}/iclock/cdata?SN={serial}&options=all&pushver=2.4.1")
    now = datetime.now().replace(microsecond=0)
    lines = "\n".join(f"{100 + i}\t{now - timedelta(minutes=i):%Y-%m-%d %H:%M:%S}\t0\t1\t0\t0\t0" for i in range(40))
    rs = await gather(*(c.post(f"{args.biobridge}/iclock/cdata?SN={serial}&table=ATTLOG&Stamp=1", content=lines)
                        for _ in range(10)))
    rep.check("same 40 punches pushed 10 times at once: no server errors", no_server_errors(rs), str(dict(statuses(rs))))
    rows = query(args.bb_db, "select count(*), count(distinct external_id) from punch_record where external_id like %s",
                 (f"{serial}:%",))
    rep.check("each pushed punch stored once", rows[0] == (40, 40), f"{rows[0][0]} rows, {rows[0][1]} distinct")


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", type=int, default=30)
    p.add_argument("--core", default="http://127.0.0.1:8200")
    p.add_argument("--biobridge", default="http://127.0.0.1:8201")
    p.add_argument("--biotime", default="http://127.0.0.1:8799")
    p.add_argument("--core-log", required=True)
    p.add_argument("--core-db", required=True)
    p.add_argument("--bb-db", required=True)
    p.add_argument("--settle", type=int, default=120, help="seconds to wait for background work to converge")
    args = p.parse_args()

    rep = Report()
    # No keep-alive: a server closing an idle pooled connection just as it is
    # reused shows up as a client ReadError, which is not what is measured.
    limits = httpx.Limits(max_connections=400, max_keepalive_connections=0)
    async with RetryingClient(timeout=httpx.Timeout(300, connect=30), limits=limits) as c:
        accounts = await check_signup(c, args, rep)
        accounts = await check_confirm(c, args, rep, accounts)
        if accounts:
            await check_add_service(c, args, rep, accounts)
            await check_provisioning(c, args, rep, accounts)
            await check_sso(c, args, rep, accounts)
            signed = [a for a in accounts if a.bb_token]
            if signed:
                await check_isolation(c, args, rep, signed)
                ready = [a for a in signed if a.source_id]
                if ready:
                    await check_sync(c, args, rep, ready)
                    await check_adms(c, args, rep, ready)
    failed = [r for r in rep.results if not r[1]]
    if BUSY_RETRIES:
        print(f"\nRetried after 503 (busy): {sum(BUSY_RETRIES.values())} — {dict(BUSY_RETRIES.most_common(5))}")
    print(f"\n{len(rep.results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

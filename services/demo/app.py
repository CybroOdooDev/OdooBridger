"""Team Desk (demo): the smallest possible OdooBridger service.

It exists to show a customer several purchased services side by side and to
document, in one readable file, what a service must do:

* ``PUT/DELETE /platform/v1/accounts/{id}`` and ``GET …/usage`` — the core
  tells the service about an account; every call carries a core-signed RS256
  token (``typ=provision``, ``aud=<slug>``, ``account_id``, ``body_sha256``).
* ``GET /sso?code=…`` — the browser arrives with a one-time code; the service
  trades it, with its client secret, for an identity token and opens a session.

State is a JSON file in DATA_DIR. Nothing here is meant for production.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jose import JWTError, jwt

SLUG = os.environ.get("SERVICE_SLUG", "demo")
CORE_PUBLIC = os.environ.get("CORE_PUBLIC_URL", "http://localhost:8000").rstrip("/")
CORE_API = os.environ.get("CORE_INTERNAL_URL", CORE_PUBLIC).rstrip("/")
PUBLIC_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8002").rstrip("/")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or CLIENT_SECRET or "demo-only"
DATA = Path(os.environ.get("DATA_DIR", "/data"))
COOKIE = "demo_session"

app = FastAPI(title="Team Desk (demo)", docs_url=None, redoc_url=None)
_lock = threading.Lock()
_key: tuple[str, float] | None = None


# --- state -------------------------------------------------------------------
def _load() -> dict:
    try:
        return json.loads((DATA / "accounts.json").read_text())
    except (OSError, ValueError):
        return {}


def _save(accounts: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "accounts.json").write_text(json.dumps(accounts, indent=1))


# --- the core's tokens -------------------------------------------------------
def core_key(force: bool = False) -> str:
    global _key
    if _key and not force and time.monotonic() - _key[1] < 3600:
        return _key[0]
    pem = httpx.get(f"{CORE_API}/api/v1/sso/public-key", timeout=10).text.strip()
    _key = (pem, time.monotonic())
    return pem


def verify(token: str, typ: str) -> dict:
    for attempt in (0, 1):
        try:
            claims = jwt.decode(token, core_key(force=attempt == 1), algorithms=["RS256"], audience=SLUG)
            break
        except JWTError as exc:
            if attempt == 1:
                raise HTTPException(401, f"Untrusted token: {exc}") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(503, f"Could not fetch the core's key: {exc}") from exc
    if claims.get("typ") != typ:
        raise HTTPException(401, "Wrong token type.")
    return claims


async def provisioned(request: Request, account_id: str) -> bytes:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token.")
    body = await request.body()
    claims = verify(header.split(None, 1)[1], "provision")
    if claims.get("account_id") != account_id:
        raise HTTPException(403, "The token is for another account.")
    if claims.get("body_sha256") != hashlib.sha256(body).hexdigest():
        raise HTTPException(403, "The token does not match this request.")
    return body


# --- what the core calls -----------------------------------------------------
@app.put("/platform/v1/accounts/{account_id}")
async def put_account(account_id: str, request: Request) -> dict:
    state = json.loads(await provisioned(request, account_id) or b"{}")
    with _lock:
        accounts = _load()
        held = accounts.get(account_id)
        if held and held.get("revision", 0) > state.get("revision", 0):
            return {"ignored": True}  # an older update arriving late
        accounts[account_id] = state
        _save(accounts)
    return {"ok": True, "revision": state.get("revision")}


@app.delete("/platform/v1/accounts/{account_id}")
async def delete_account(account_id: str, request: Request) -> dict:
    await provisioned(request, account_id)
    with _lock:
        accounts = _load()
        existed = accounts.pop(account_id, None) is not None
        _save(accounts)
    return {"deleted": existed}


@app.get("/platform/v1/accounts/{account_id}/usage")
async def usage(account_id: str, request: Request) -> dict:
    await provisioned(request, account_id)
    state = _load().get(account_id)
    if not state:
        raise HTTPException(404, "No such account here.")
    return {"seats_used": len(state.get("users") or []), "seats_allowed": (state.get("entitlements") or {}).get("max_seats"),
            "status": state.get("status")}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "demo"}


# --- sign-in -----------------------------------------------------------------
def to_core() -> RedirectResponse:
    back = quote(f"{PUBLIC_URL}/sso", safe="")
    return RedirectResponse(f"{CORE_PUBLIC}/app/#/open/{SLUG}?redirect_uri={back}")


@app.get("/sso")
def sso(code: str = "") -> Response:
    if not code:
        return to_core()
    try:
        reply = httpx.post(f"{CORE_API}/api/v1/sso/token", timeout=10,
                           json={"client_id": SLUG, "client_secret": CLIENT_SECRET, "code": code})
    except httpx.HTTPError as exc:
        return page("Sign-in failed", f"<p>Could not reach OdooBridger: {html.escape(str(exc))}</p>", status=502)
    if reply.status_code != 200:
        detail = reply.json().get("detail", "") if reply.headers.get("content-type", "").startswith("application/json") else ""
        return page("Sign-in failed", f"<p>{html.escape(str(detail) or 'OdooBridger refused the code.')}</p>"
                    f'<p><a href="/">Try again</a></p>', status=400)
    claims = verify(reply.json()["id_token"], "identity")
    with _lock:  # the identity token carries the same state as provisioning
        accounts = _load()
        held = accounts.get(claims["account"]["id"])
        if not held or held.get("revision", 0) <= claims.get("revision", 0):
            accounts[claims["account"]["id"]] = {
                "account": claims["account"], "status": claims["status"], "package": claims.get("package"),
                "entitlements": claims.get("entitlements") or {}, "trial_ends_at": claims.get("trial_ends_at"),
                "renews_at": claims.get("renews_at"), "users": (held or {}).get("users") or [],
                "revision": claims.get("revision", 0)}
            _save(accounts)
    session = jwt.encode({"sub": claims["sub"], "email": claims["email"], "name": claims.get("name"),
                          "role": claims["role"], "account": claims["account"]["id"],
                          "portal": claims.get("portal_url"), "exp": int(time.time()) + 8 * 3600},
                         SESSION_SECRET, algorithm="HS256")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, session, httponly=True, samesite="lax", max_age=8 * 3600)
    return response


@app.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE)
    return response


# --- the dashboard -----------------------------------------------------------
CSS = """
:root{--ink:#10162f;--muted:#5b6478;--rule:#e3e5ee;--bg:#f5f6fa;--accent:#4f46e5;--ok:#0f7a4d;--bad:#b42318;--warn:#b54708}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
header{background:#fff;border-bottom:1px solid var(--rule)}.wrap{max-width:900px;margin:0 auto;padding:0 20px}
header .wrap{display:flex;align-items:center;gap:14px;height:60px}.brand{font-weight:700}.brand b{color:var(--accent)}
.sp{flex:1}a{color:var(--accent)}main{padding:28px 0 60px}h1{margin:0 0 4px;font-size:24px}.sub{color:var(--muted);margin:0 0 22px}
.card{background:#fff;border:1px solid var(--rule);border-radius:12px;padding:20px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px}
.stat b{display:block;font-size:22px}.stat span{color:var(--muted);font-size:13px}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:600;background:#eceafe;color:var(--accent)}
.pill.ok{background:#e3f5ec;color:var(--ok)}.pill.bad{background:#fde8e6;color:var(--bad)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px 6px;border-bottom:1px solid var(--rule);font-size:14px}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.bar{height:8px;border-radius:8px;background:var(--rule);overflow:hidden;margin-top:8px}.bar i{display:block;height:100%;background:var(--accent)}
.tag{background:#fff7e6;color:var(--warn);border-radius:6px;padding:2px 8px;font-size:12px;font-weight:600}
"""


def page(title: str, body: str, *, user: dict | None = None, status: int = 200) -> HTMLResponse:
    nav = ""
    if user:
        portal = html.escape(user.get("portal") or CORE_PUBLIC + "/app/")
        nav = (f'<a href="{portal}">All Services &amp; Billing</a> · <span>{html.escape(user["email"])}</span> · '
               '<a href="/logout">Sign Out</a>')
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} — Team Desk</title>
<style>{CSS}</style></head><body><header><div class="wrap"><span class="brand"><b>Team</b> Desk</span>
<span class="tag">Demo service</span><span class="sp"></span><small>{nav}</small></div></header>
<main><div class="wrap">{body}</div></main></body></html>""", status_code=status)


def session_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        return jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
    except JWTError:
        return None


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = session_user(request)
    if not user:
        return to_core()
    state = _load().get(user["account"])
    if not state:
        return page("Not set up", "<h1>Not set up yet</h1><p class='sub'>OdooBridger has not sent this account here yet. "
                    "Reload in a few seconds.</p>", user=user)
    status = state.get("status", "")
    tone = "ok" if status in ("active", "trialing") else "bad"
    ents = state.get("entitlements") or {}
    seats, used = ents.get("max_seats"), len(state.get("users") or [])
    pct = min(100, round(used / seats * 100)) if seats else 0
    blocked = status not in ("active", "trialing")
    rows = "".join(f"<tr><td>{html.escape(u.get('full_name') or '—')}</td><td>{html.escape(u['email'])}</td>"
                   f"<td>{html.escape(u['role'])}</td></tr>" for u in state.get("users") or [])
    pkg = (state.get("package") or {}).get("name") or "—"
    body = f"""<h1>{html.escape(state['account']['name'])}</h1>
<p class="sub">You are signed in as {html.escape(user['name'] or user['email'])} ({html.escape(user['role'])}).</p>
{'<div class="card" style="border-color:#f4b8b2"><b>This service is paused</b> for this account (' + html.escape(status) + '). Open All Services &amp; Billing to fix it.</div>' if blocked else ''}
<div class="card"><div class="grid">
<div class="stat"><span>Status</span><b><span class="pill {tone}">{html.escape(status)}</span></b></div>
<div class="stat"><span>Package</span><b>{html.escape(pkg)}</b></div>
<div class="stat"><span>Trial ends</span><b>{html.escape((state.get('trial_ends_at') or '—')[:10])}</b></div>
<div class="stat"><span>Renews</span><b>{html.escape((state.get('renews_at') or '—')[:10])}</b></div></div></div>
<div class="card"><b>Seats</b> <span style="color:var(--muted)">{used} used of {seats if seats else 'unlimited'}</span>
<div class="bar"><i style="width:{pct}%"></i></div></div>
<div class="card"><b>People on this account</b><table><thead><tr><th>Name</th><th>Email</th><th>Role</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<div class="card"><b>Limits sent by OdooBridger</b><table><tbody>{''.join(
        f"<tr><td>{html.escape(k)}</td><td>{html.escape(str(v) if v is not None else 'unlimited')}</td></tr>" for k, v in ents.items()) or '<tr><td>None</td></tr>'}</tbody></table></div>"""
    return page("Dashboard", body, user=user)

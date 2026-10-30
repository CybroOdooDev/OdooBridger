#!/usr/bin/env python3
"""A stand-in ZKTeco BioTime server. Standard library only, one file.

It speaks the parts of BioTime 8.x / 9.x that integrations use, so a connection
can be built, tested and demonstrated with no hardware and no licence:

    POST /api-token-auth/            {username, password} -> {"token"}     (Authorization: Token <t>)
    POST /jwt-api-token-auth/        the same, for the JWT flavour         (Authorization: JWT <t>)
    GET  /iclock/api/transactions/   ?page &page_size &emp_code &terminal_sn &start_time &end_time &upload_time
    GET  /iclock/api/transactions/<id>/
    GET  /iclock/api/terminals/
    GET  /personnel/api/employees/   (+ POST to create one)
    GET  /personnel/api/departments/
    GET  /personnel/api/areas/

Lists are paginated the way Django REST Framework does it:
{"count", "next" (an absolute URL or null), "previous", "data": [...]}.
Punch times are naive local wall-clock strings in the server's own timezone,
as BioTime sends them.

Everything the real server would make awkward is a control endpoint here
(no token needed, they are not part of BioTime's API):

    GET    /health                  liveness, for Docker
    GET    /mock/state              who is in or out, counts, settings
    POST   /mock/punch              {"emp_code","state":"in|out|0..5","terminal","at","uploaded_at"}
    DELETE /mock/transactions/<id>  a transaction deleted on the server (leaves an id gap)
    POST   /mock/outage             {"seconds": 30, "status": 503}  fail every API call for a while
    POST   /mock/reset              {"seed": true}  start again (ids keep growing, never reused)

State lives in one JSON file under --data-dir, so restarts keep their history
and, above all, their transaction ids: an integration that remembers "I have
everything up to id N" must never be handed a new punch numbered below N.

Settings come from the command line or the environment (see ``parse_args``).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

TIME_FMT = "%Y-%m-%d %H:%M:%S"

#: punch_state -> BioTime's own label.
STATES = {"0": "Check In", "1": "Check Out", "2": "Break Out", "3": "Break In",
          "4": "Overtime In", "5": "Overtime Out"}
STATE_NAMES = {"in": "0", "out": "1", "break-out": "2", "break-in": "3",
               "overtime-in": "4", "overtime-out": "5"}
IN_STATES = {"0", "3", "4"}

# --------------------------------------------------------------------------- #
# Built-in people, places and the story their punches tell
# --------------------------------------------------------------------------- #
DEPARTMENTS = [
    {"id": 1, "dept_code": "1", "dept_name": "Production"},
    {"id": 2, "dept_code": "2", "dept_name": "Logistics"},
    {"id": 3, "dept_code": "3", "dept_name": "Administration"},
]
AREAS = [
    {"id": 1, "area_code": "1", "area_name": "Not Authorized"},
    {"id": 2, "area_code": "2", "area_name": "Head Office"},
]
TERMINALS = [
    {"id": 1, "sn": "MOCK-GATE-01", "alias": "Main Gate", "ip_address": "10.0.0.11", "model": "MB460"},
    {"id": 2, "sn": "MOCK-GATE-02", "alias": "Back Door", "ip_address": "10.0.0.12", "model": "MB460"},
    {"id": 3, "sn": "MOCK-GATE-03", "alias": "Loading Bay", "ip_address": "10.0.0.13", "model": "SpeedFace-V5L"},
]
#: (id, emp_code, first, last, department id, attends?)
PEOPLE = [
    (11, "1001", "Ahmed", "Sharma", 1, True),     # day shift, lunch break punches
    (12, "0042", "Sara", "Tanaka", 2, True),      # day shift (a code with a leading zero)
    (13, "A7", "Jane", "Haddad", 3, True),        # forgets to check out every third day
    (14, "2001", "Liam", "Okafor", 1, True),      # night shift: 21:00 to 05:00 next day
    (15, "2002", "Priya", "Nakamura", 2, True),   # double-taps the reader
    (16, "2003", "Noah", "Fernandes", 3, False),  # left the company: attendance switched off
]


def _employee(row: tuple, departments: list[dict]) -> dict:
    id_, code, first, last, dept_id, attends = row
    dept = next(d for d in departments if d["id"] == dept_id)
    return {"id": id_, "emp_code": code, "first_name": first, "last_name": last,
            "department": dept, "area": [AREAS[1]], "enable_attendance": attends}


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
class Store:
    """All data, behind one lock, saved to disk after every change."""

    def __init__(self, path: str | None, tz: str) -> None:
        self.path = path
        self.tz = ZoneInfo(tz)
        self.lock = threading.RLock()
        self.data: dict = {}
        self.tokens: dict[str, float] = {}      # token -> expiry (epoch), 0 = never
        self.outage_until = 0.0
        self.outage_status = 503

    # -- clock -------------------------------------------------------------
    def now_local(self) -> datetime:
        return datetime.now(self.tz).replace(tzinfo=None)

    # -- persistence ---------------------------------------------------------
    def load(self) -> bool:
        if self.path and os.path.exists(self.path):
            with open(self.path) as handle:
                self.data = json.load(handle)
            return True
        return False

    def save(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as handle:
            json.dump(self.data, handle, indent=1)
        os.replace(tmp, self.path)           # a reader never sees half a file

    # -- seeding -------------------------------------------------------------
    def reset(self, *, seed_days: int, fixtures: dict | None = None, keep_ids: bool = False,
              id_start: int | None = None) -> None:
        with self.lock:
            fixtures = fixtures or {}
            departments = fixtures.get("departments") or [dict(d) for d in DEPARTMENTS]
            if fixtures.get("employees"):
                employees = [dict(e) for e in fixtures["employees"]]
            else:
                employees = [_employee(p, departments) for p in PEOPLE]
            previous = self.data.get("next_id") if keep_ids else None
            # Ids start at the clock, not at 1, so wiping the data directory can
            # never make the server reuse an id a client has already stored.
            first = max(previous or 0, id_start or int(time.time()))
            self.data = {
                "next_id": first,
                "departments": departments,
                "areas": fixtures.get("areas") or [dict(a) for a in AREAS],
                "terminals": fixtures.get("terminals") or [dict(t) for t in TERMINALS],
                "employees": employees,
                "transactions": [],
            }
            if seed_days > 0 and not fixtures.get("employees"):
                self._seed_history(seed_days)
            self.save()

    def _seed_history(self, days: int) -> None:
        """A believable week. Seeded by the date, so the same day gives the same story."""
        rng = random.Random(self.now_local().date().toordinal())
        now = self.now_local()
        terminals = [t["sn"] for t in self.data["terminals"]]

        def punch(code: str, when: datetime, state: str, upload_lag: int | None = None) -> None:
            if when > now:
                return
            uploaded = when + timedelta(seconds=rng.randint(1, 25) if upload_lag is None else upload_lag)
            self._add(code, when, state, rng.choice(terminals), min(uploaded, now))

        def at(day: date, hour: int, minute: int, jitter: int) -> datetime:
            return datetime(day.year, day.month, day.day, hour, minute) + timedelta(
                minutes=rng.randint(-jitter, jitter), seconds=rng.randint(0, 59))

        workdays = [d for d in (now.date() - timedelta(days=n) for n in range(days, -1, -1)) if d.weekday() < 5]
        past = [d for d in workdays if d < now.date()]
        late_day = past[-1] if past else None
        for index, day in enumerate(workdays):
            # Ahmed: 09:00-18:00 with a lunch break.
            punch("1001", at(day, 9, 0, 10), "0")
            punch("1001", at(day, 13, 0, 5), "2")
            punch("1001", at(day, 13, 45, 5), "3")
            punch("1001", at(day, 18, 0, 10), "1")
            # Sara: 08:30-17:30.
            punch("0042", at(day, 8, 30, 10), "0")
            if day != late_day:
                punch("0042", at(day, 17, 30, 15), "1")
            # Jane: no check-out every third day (an open shift to deal with).
            punch("A7", at(day, 9, 15, 10), "0")
            if index % 3 != 2:
                punch("A7", at(day, 17, 45, 10), "1")
            # Liam: in at 21:00, out at 05:00 the next morning.
            punch("2001", at(day, 21, 0, 10), "0")
            punch("2001", at(day + timedelta(days=1), 5, 0, 10), "1")
            # Priya: taps twice in a few seconds, in and out.
            in_time, out_time = at(day, 9, 30, 10), at(day, 18, 15, 10)
            punch("2002", in_time, "0")
            punch("2002", in_time + timedelta(seconds=rng.randint(3, 9)), "0")
            punch("2002", out_time, "1")
            punch("2002", out_time + timedelta(seconds=rng.randint(3, 9)), "1")
        # One late upload: Sara's terminal was offline and sent her last check-out
        # this morning. Its punch time is old; its upload time and id are new.
        if late_day is not None:
            self._add("0042", datetime(late_day.year, late_day.month, late_day.day, 17, 40), "1",
                      terminals[-1], now - timedelta(minutes=rng.randint(1, 30)))
        self.data["transactions"].sort(key=lambda r: r["id"])

    # -- punches -------------------------------------------------------------
    def _add(self, emp_code: str, when: datetime, state: str, terminal_sn: str,
             uploaded: datetime | None = None) -> dict:
        employee = next((e for e in self.data["employees"] if str(e["emp_code"]) == str(emp_code)), None)
        terminal = next((t for t in self.data["terminals"] if t["sn"] == terminal_sn), None)
        row_id = self.data["next_id"] + 1
        self.data["next_id"] = row_id
        dept = (employee or {}).get("department")
        row = {
            "id": row_id,
            "emp_code": str(emp_code),
            "first_name": (employee or {}).get("first_name", ""),
            "last_name": (employee or {}).get("last_name", ""),
            "department": dept.get("dept_name") if isinstance(dept, dict) else dept,
            "position": None,
            "punch_time": when.strftime(TIME_FMT),
            "punch_state": state,
            "punch_state_display": STATES.get(state, state),
            "verify_type": 15,
            "verify_type_display": "Face",
            "area_alias": "Head Office",
            "terminal_sn": terminal_sn,
            "terminal_alias": (terminal or {}).get("alias"),
            "latitude": None, "longitude": None, "gps_location": None, "mobile": None,
            "source": 1, "purpose": None, "crc": None, "is_attendance": 1, "reserved": None,
            "upload_time": (uploaded or when).strftime(TIME_FMT),
            "sync_status": None, "sync_time": None, "company": "Mock Company",
        }
        self.data["transactions"].append(row)
        return row

    def last_state(self, emp_code: str) -> tuple[str | None, str | None]:
        mine = [r for r in self.data["transactions"] if r["emp_code"] == str(emp_code)]
        if not mine:
            return None, None
        last = max(mine, key=lambda r: (r["punch_time"], r["id"]))
        return last["punch_state"], last["punch_time"]

    def punch(self, emp_code: str, state: str | None = None, terminal: str | None = None,
              at: str | None = None, uploaded_at: str | None = None) -> dict:
        code = str(emp_code or "").strip()
        if not code:
            raise ValueError("emp_code is required")
        with self.lock:
            if not any(str(e["emp_code"]) == code for e in self.data["employees"]):
                raise ValueError(f"no employee with emp_code {code}")
            last, _ = self.last_state(code)
            if state in (None, ""):
                state = "1" if last in IN_STATES else "0"        # toggle
            state = STATE_NAMES.get(str(state).lower(), str(state))
            if state not in STATES:
                raise ValueError("state must be in, out, break-out, break-in, overtime-in, "
                                 "overtime-out or 0 to 5")
            when = datetime.strptime(at, TIME_FMT) if at else self.now_local()
            uploaded = datetime.strptime(uploaded_at, TIME_FMT) if uploaded_at else (
                self.now_local() if at else when)
            if not terminal:
                sns = [t["sn"] for t in self.data["terminals"]]
                terminal = sns[sum(map(ord, code)) % len(sns)] if sns else "MOCK-GATE-01"
            row = self._add(code, when, state, terminal, uploaded)
            self.save()
            return row

    def delete(self, row_id: int) -> bool:
        with self.lock:
            rows = self.data["transactions"]
            kept = [r for r in rows if r["id"] != row_id]
            self.data["transactions"] = kept
            if len(kept) != len(rows):
                self.save()
            return len(kept) != len(rows)

    # -- employees -------------------------------------------------------------
    def create_employee(self, body: dict) -> tuple[int, dict]:
        code = str(body.get("emp_code") or "").strip()
        if not code:
            return 400, {"emp_code": ["This field is required."]}
        with self.lock:
            if any(str(e["emp_code"]) == code for e in self.data["employees"]):
                return 400, {"emp_code": ["employee with this emp code already exists."]}
            missing = {f: ["This field is required."] for f in ("department", "area") if not body.get(f)}
            if missing:
                return 400, missing                      # real BioTime refuses these too
            dept = next((d for d in self.data["departments"] if d["id"] == body.get("department")), None)
            if dept is None:
                return 400, {"department": ["Invalid pk - object does not exist."]}
            area_ids = body["area"] if isinstance(body["area"], list) else [body["area"]]
            areas = [a for a in self.data["areas"] if a["id"] in area_ids]
            if not areas:
                return 400, {"area": ["Invalid pk - object does not exist."]}
            row = {"id": max([int(e.get("id") or 0) for e in self.data["employees"]] + [0]) + 1,
                   "emp_code": code, "first_name": str(body.get("first_name") or code),
                   "last_name": str(body.get("last_name") or ""), "department": dept, "area": areas,
                   "enable_attendance": bool(body.get("enable_attendance", True))}
            self.data["employees"].append(row)
            self.save()
            return 201, row

    # -- auth ---------------------------------------------------------------
    def issue_token(self, ttl: int) -> str:
        token = secrets.token_hex(20)
        with self.lock:
            self.tokens[token] = time.time() + ttl if ttl > 0 else 0
        return token

    def token_valid(self, token: str) -> bool:
        with self.lock:
            expiry = self.tokens.get(token)
            if expiry is None:
                return False
            if expiry and expiry < time.time():
                del self.tokens[token]
                return False
            return True


STORE: Store
SETTINGS: argparse.Namespace


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    #: A client that opens a socket and never finishes a request (a port scan, a
    #: browser pre-connect) must not hold a thread forever.
    timeout = 15

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (TimeoutError, ConnectionError):
            self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:           # one line per request, not per health check
        if SETTINGS.log and not self.path.startswith("/health"):
            print(f"{datetime.now().strftime('%H:%M:%S')} {self.command:<6} {self.path}  -> {args[1] if len(args) > 1 else ''}",
                  flush=True)

    # -- helpers -------------------------------------------------------------
    def _send(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else None
        except (ValueError, json.JSONDecodeError):
            return None

    def _outage(self) -> bool:
        if STORE.outage_until > time.time():
            self._send({"detail": "Mock outage in progress."}, STORE.outage_status)
            return True
        return False

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization") or ""
        scheme, _, token = header.partition(" ")
        if scheme not in ("Token", "JWT") or not STORE.token_valid(token.strip()):
            self._send({"detail": "Authentication credentials were not provided or are invalid."}, 401)
            return False
        return True

    def _page(self, rows: list, query: dict, path: str) -> dict:
        def number(name: str, default: int) -> int:
            try:
                return max(1, int(query.get(name, [default])[0]))
            except ValueError:
                return default

        page = number("page", 1)
        size = min(number("page_size", 10), SETTINGS.max_page_size)
        start = (page - 1) * size
        base = f"http://{self.headers.get('Host', 'localhost')}{path}"

        def link(n: int) -> str:
            rest = [(k, v) for k, vs in query.items() if k != "page" for v in vs]
            return base + "?" + urlencode(rest + [("page", n)])        # re-encoded, as DRF does

        return {"count": len(rows),
                "next": link(page + 1) if start + size < len(rows) else None,
                "previous": link(page - 1) if page > 1 else None,
                "data": rows[start:start + size]}

    # -- POST ---------------------------------------------------------------
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/mock/"):
            return self._control("POST", path)
        if self._outage():
            return
        if path in ("/api-token-auth/", "/jwt-api-token-auth/"):
            return self._login()
        if path == "/personnel/api/employees/":
            if not self._authorised():
                return
            body = self._body()
            if body is None:
                return self._send({"detail": "Invalid JSON."}, 400)
            status, payload = STORE.create_employee(body)
            return self._send(payload, status)
        self._send({"detail": "Not found."}, 404)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/mock/"):
            return self._control("DELETE", path)
        self._send({"detail": "Method not allowed."}, 405)

    def _login(self) -> None:
        body = self._body() or {}
        if SETTINGS.auth != "any" and (body.get("username") != SETTINGS.username
                                       or body.get("password") != SETTINGS.password):
            # BioTime's own wording for bad credentials.
            return self._send({"non_field_errors": ["Unable to log in with provided credentials."]}, 400)
        self._send({"token": STORE.issue_token(SETTINGS.token_ttl)})

    # -- GET ----------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/health":
            return self._send({"status": "ok"})
        if path == "/":
            return self._send({"service": "mock BioTime", "login": f"{SETTINGS.username} / {SETTINGS.password}",
                               "docs": "see the README; GET /mock/state for what is in the data"})
        if path.startswith("/mock/"):
            return self._control("GET", path)
        if self._outage() or not self._authorised():
            return

        with STORE.lock:
            data = STORE.data
            if path == "/personnel/api/departments/":
                return self._send(self._page(data["departments"], query, path))
            if path == "/personnel/api/areas/":
                return self._send(self._page(data["areas"], query, path))
            if path == "/personnel/api/employees/":
                rows = data["employees"]
                code = (query.get("emp_code") or [None])[0]
                if code:
                    rows = [e for e in rows if str(e["emp_code"]) == code]
                return self._send(self._page(rows, query, path))
            if path == "/iclock/api/terminals/":
                return self._send(self._page([self._terminal(t) for t in data["terminals"]], query, path))
            one = re.fullmatch(r"/iclock/api/transactions/(\d+)/", path)
            if one:
                row = next((r for r in data["transactions"] if r["id"] == int(one.group(1))), None)
                return self._send(row, 200) if row else self._send({"detail": "Not found."}, 404)
            if path == "/iclock/api/transactions/":
                return self._send(self._page(self._transactions(query), query, path))
        self._send({"detail": "Not found."}, 404)

    @staticmethod
    def _terminal(t: dict) -> dict:
        area = next((a for a in STORE.data["areas"] if a["id"] == t.get("area_id", 2)), STORE.data["areas"][0])
        return {**t, "terminal_name": t["alias"], "area": area, "fw_ver": "Ver 8.0.4", "state": 1,
                "terminal_tz": 330, "user_count": len(STORE.data["employees"]),
                "last_activity": STORE.now_local().strftime(TIME_FMT)}

    @staticmethod
    def _transactions(query: dict) -> list[dict]:
        rows = STORE.data["transactions"]
        first = lambda name: (query.get(name) or [None])[0]       # noqa: E731
        if first("emp_code"):
            rows = [r for r in rows if r["emp_code"] == first("emp_code")]
        if first("terminal_sn"):
            rows = [r for r in rows if r["terminal_sn"] == first("terminal_sn")]
        # Both bounds inclusive, on the server's own wall clock.
        if first("start_time"):
            rows = [r for r in rows if r["punch_time"] >= first("start_time")]
        if first("end_time"):
            rows = [r for r in rows if r["punch_time"] <= first("end_time")]
        uploaded = first("upload_time")
        if uploaded and SETTINGS.upload_filter != "ignore":
            # "from": received at or after that time. "exact": only that second.
            # "ignore": the server takes the parameter and does nothing with it.
            if SETTINGS.upload_filter == "exact":
                rows = [r for r in rows if r["upload_time"] == uploaded]
            else:
                rows = [r for r in rows if r["upload_time"] >= uploaded]
        return sorted(rows, key=lambda r: (r["punch_time"], r["id"]))

    # -- the control endpoints ------------------------------------------------
    def _control(self, method: str, path: str) -> None:
        try:
            if method == "GET" and path == "/mock/state":
                return self._send(self._state())
            if method == "POST" and path == "/mock/punch":
                body = self._body()
                if body is None:
                    return self._send({"detail": "Invalid JSON."}, 400)
                return self._send(STORE.punch(body.get("emp_code", ""), body.get("state"), body.get("terminal"),
                                              body.get("at"), body.get("uploaded_at")), 201)
            if method == "POST" and path == "/mock/outage":
                body = self._body() or {}
                seconds = float(body.get("seconds", 30))
                STORE.outage_until = time.time() + seconds
                STORE.outage_status = int(body.get("status", 503))
                return self._send({"outage_seconds": seconds, "status": STORE.outage_status})
            if method == "POST" and path == "/mock/reset":
                body = self._body() or {}
                STORE.reset(seed_days=SETTINGS.seed_days if body.get("seed", True) else 0,
                            fixtures=load_fixtures(SETTINGS.fixtures), keep_ids=True)
                return self._send(self._state())
            gone = re.fullmatch(r"/mock/transactions/(\d+)", path)
            if method == "DELETE" and gone:
                found = STORE.delete(int(gone.group(1)))
                return self._send({"deleted": found}, 200 if found else 404)
        except ValueError as exc:
            return self._send({"detail": str(exc)}, 400)
        self._send({"detail": "Not found."}, 404)

    @staticmethod
    def _state() -> dict:
        with STORE.lock:
            rows = STORE.data["transactions"]
            people = {}
            for e in STORE.data["employees"]:
                state, when = STORE.last_state(e["emp_code"])
                people[str(e["emp_code"])] = {
                    "name": f"{e['first_name']} {e['last_name']}".strip(),
                    "attends": e.get("enable_attendance", True),
                    "last_punch": when,
                    "now": "no punches" if state is None else ("in" if state in IN_STATES else "out"),
                }
            return {"server_time": STORE.now_local().strftime(TIME_FMT), "timezone": str(STORE.tz),
                    "transactions": len(rows), "highest_id": max((r["id"] for r in rows), default=None),
                    "terminals": [t["sn"] for t in STORE.data["terminals"]], "people": people,
                    "settings": {"login": SETTINGS.username, "auth": SETTINGS.auth,
                                 "token_ttl": SETTINGS.token_ttl, "upload_filter": SETTINGS.upload_filter,
                                 "max_page_size": SETTINGS.max_page_size,
                                 "simulate_every_seconds": SETTINGS.interval}}


# --------------------------------------------------------------------------- #
# Live punches
# --------------------------------------------------------------------------- #
def simulate(stop: threading.Event) -> None:
    """Every interval a random active employee clocks in or out right now, so a
    sync always has something fresh. Nobody punches twice inside 30 seconds."""
    rng = random.Random()
    while not stop.wait(SETTINGS.interval):
        with STORE.lock:
            people = [str(e["emp_code"]) for e in STORE.data["employees"] if e.get("enable_attendance", True)]
            rng.shuffle(people)
            now = STORE.now_local()
            for code in people:
                _, when = STORE.last_state(code)
                if when and (now - datetime.strptime(when, TIME_FMT)).total_seconds() < 30:
                    continue
                row = STORE.punch(code)
                print(f"  simulated {STATES[row['punch_state']]:<9} badge {code:<5} {row['punch_time']} "
                      f"{row['terminal_sn']} (id {row['id']})", flush=True)
                break


def load_fixtures(path: str | None) -> dict | None:
    if path and os.path.exists(path):
        with open(path) as handle:
            return json.load(handle)
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.environ.get

    def flag(name: str, default: str) -> bool:
        return env(name, default).lower() in ("1", "true", "yes", "on")

    p = argparse.ArgumentParser(description="A stand-in ZKTeco BioTime server.")
    p.add_argument("--host", default=env("MOCK_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(env("MOCK_PORT", "8100")))
    p.add_argument("--tz", default=env("MOCK_TZ", "Asia/Kolkata"),
                   help="timezone of the punch times handed out (the connection's Server Timezone)")
    p.add_argument("--data-dir", default=env("MOCK_DATA_DIR", "/data"))
    p.add_argument("--fixtures", default=env("MOCK_FIXTURES", ""),
                   help="JSON file with your own departments, areas, terminals and employees")
    p.add_argument("--username", default=env("MOCK_USERNAME", "admin"))
    p.add_argument("--password", default=env("MOCK_PASSWORD", "admin"))
    p.add_argument("--auth", choices=["strict", "any"], default=env("MOCK_AUTH", "strict"),
                   help="strict: the username and password must match; any: every login works")
    p.add_argument("--token-ttl", type=int, default=int(env("MOCK_TOKEN_TTL", "0")),
                   help="seconds a token lasts (0 = forever); a short one exercises re-login")
    p.add_argument("--upload-filter", choices=["from", "exact", "ignore"],
                   default=env("MOCK_UPLOAD_FILTER", "from"),
                   help="how ?upload_time= behaves: from that time on, that second only, or ignored")
    p.add_argument("--max-page-size", type=int, default=int(env("MOCK_MAX_PAGE_SIZE", "100")),
                   help="largest page served; a small one makes every sync paginate")
    p.add_argument("--seed-days", type=int, default=int(env("MOCK_SEED_DAYS", "7")),
                   help="days of history written the first time the server starts")
    p.add_argument("--interval", type=float, default=float(env("MOCK_INTERVAL", "20")),
                   help="seconds between simulated live punches (0 = none)")
    p.add_argument("--id-start", type=int, default=int(env("MOCK_ID_START", "0")) or None)
    p.add_argument("--reset", action="store_true", default=flag("MOCK_RESET", "0"),
                   help="throw away the saved data and start again (ids keep growing)")
    p.add_argument("--no-log", dest="log", action="store_false", default=not flag("MOCK_QUIET", "0"))
    return p.parse_args(argv)


def build(args: argparse.Namespace) -> ThreadingHTTPServer:
    """Set everything up and return the server, not yet serving (tests use this)."""
    global STORE, SETTINGS
    SETTINGS = args
    path = None
    if args.data_dir:
        os.makedirs(args.data_dir, exist_ok=True)
        path = os.path.join(args.data_dir, "state.json")
    STORE = Store(path, args.tz)
    loaded = STORE.load()
    if args.reset or not loaded:
        STORE.reset(seed_days=args.seed_days, fixtures=load_fixtures(args.fixtures),
                    keep_ids=loaded and args.reset, id_start=args.id_start)
    return ThreadingHTTPServer((args.host, args.port), Handler)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    server = build(args)
    state = Handler._state()
    print(f"mock BioTime on http://{args.host}:{server.server_address[1]}", flush=True)
    print(f"  login        {args.username} / {args.password}"
          f"{'  (any login is accepted)' if args.auth == 'any' else ''}", flush=True)
    print(f"  timezone     {args.tz}  <- set the connection's Server Timezone to this", flush=True)
    print(f"  data         {len(STORE.data['employees'])} employees, {len(STORE.data['terminals'])} terminals, "
          f"{state['transactions']} transactions (highest id {state['highest_id']})", flush=True)
    print(f"  upload_time  filter behaves as '{args.upload_filter}'; pages up to {args.max_page_size}", flush=True)
    print("  controls     GET /mock/state   POST /mock/punch   POST /mock/outage   POST /mock/reset", flush=True)
    if args.interval > 0:
        threading.Thread(target=simulate, args=(threading.Event(),), daemon=True).start()
        print(f"  simulating   one punch every {args.interval:g}s", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()

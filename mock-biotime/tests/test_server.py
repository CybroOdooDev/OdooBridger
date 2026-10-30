"""The mock BioTime, driven over real HTTP."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as mock  # noqa: E402


def _start(tmp_path, **overrides):
    argv = ["--host", "127.0.0.1", "--port", "0", "--data-dir", str(tmp_path), "--interval", "0",
            "--tz", "Asia/Kolkata", "--no-log"]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    httpd = mock.build(mock.parse_args(argv))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _local_now() -> datetime:
    return datetime.now(mock.STORE.tz).replace(tzinfo=None)


class Client:
    def __init__(self, base: str, jwt: bool = False):
        self.base, self.jwt, self.token = base, jwt, None

    def call(self, method, path, body=None, auth=True):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(path if path.startswith("http") else self.base + path,
                                         data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if auth and self.token:
            request.add_header("Authorization", f"{'JWT' if self.jwt else 'Token'} {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def login(self, username="admin", password="admin"):
        path = "/jwt-api-token-auth/" if self.jwt else "/api-token-auth/"
        status, body = self.call("POST", path, {"username": username, "password": password}, auth=False)
        if status == 200:
            self.token = body["token"]
        return status, body

    def get(self, path):
        return self.call("GET", path)

    def pages(self, path):
        rows, url = [], path
        while url:
            status, body = self.get(url)
            assert status == 200, body
            rows += body["data"]
            url = body["next"]
        return rows


@pytest.fixture()
def server(tmp_path):
    httpd, base = _start(tmp_path)
    client = Client(base)
    client.login()
    yield client
    httpd.shutdown()
    httpd.server_close()


def test_login_wrong_password_is_refused_the_way_biotime_does(server):
    status, body = Client(server.base).login(password="nope")
    assert status == 400 and "non_field_errors" in body


def test_both_auth_flavours_work(server):
    jwt = Client(server.base, jwt=True)
    assert jwt.login()[0] == 200
    assert jwt.get("/personnel/api/employees/")[0] == 200


def test_every_endpoint_needs_a_token(server):
    anon = Client(server.base)
    for path in ("/iclock/api/transactions/", "/iclock/api/terminals/", "/personnel/api/employees/"):
        assert anon.get(path)[0] == 401


def test_pagination_follows_next_links_to_the_end(tmp_path):
    httpd, base = _start(tmp_path, max_page_size=5)
    client = Client(base)
    client.login()
    status, first = client.get("/iclock/api/transactions/?page_size=100")
    assert status == 200 and len(first["data"]) == 5 and first["next"].startswith(base)
    everything = client.pages("/iclock/api/transactions/?page_size=100")
    assert len(everything) == first["count"] and len({r["id"] for r in everything}) == first["count"]
    httpd.shutdown()


def test_time_window_is_inclusive_and_in_server_local_time(server):
    rows = server.pages("/iclock/api/transactions/")
    mid = rows[len(rows) // 2]["punch_time"]
    window = server.pages(f"/iclock/api/transactions/?start_time={mid.replace(' ', '%20')}&end_time={mid.replace(' ', '%20')}")
    assert window and all(r["punch_time"] == mid for r in window)
    later = server.pages(f"/iclock/api/transactions/?start_time={mid.replace(' ', '%20')}")
    assert later and all(r["punch_time"] >= mid for r in later)


def test_punch_toggles_and_accepts_named_states(server):
    first = server.call("POST", "/mock/punch", {"emp_code": "2003"})[1]
    second = server.call("POST", "/mock/punch", {"emp_code": "2003"})[1]
    assert (first["punch_state"], second["punch_state"]) == ("0", "1") and second["id"] > first["id"]
    assert server.call("POST", "/mock/punch", {"emp_code": "2003", "state": "break-out"})[1]["punch_state"] == "2"
    assert server.call("POST", "/mock/punch", {"emp_code": "nobody"})[0] == 400
    assert server.call("POST", "/mock/punch", {"emp_code": "2003", "state": "9"})[0] == 400


def test_a_late_upload_is_found_by_upload_time_but_not_by_a_later_window(server):
    since = (_local_now() - timedelta(minutes=2)).strftime(mock.TIME_FMT).replace(" ", "%20")
    old = "2026-10-01 17:00:00"
    row = server.call("POST", "/mock/punch", {"emp_code": "1001", "state": "out", "at": old})[1]
    assert row["punch_time"] == old and row["upload_time"] > since.replace("%20", " ")
    found = {r["id"] for r in server.pages(f"/iclock/api/transactions/?upload_time={since}")}
    assert row["id"] in found
    by_time = {r["id"] for r in server.pages(f"/iclock/api/transactions/?start_time={since}")}
    assert row["id"] not in by_time


@pytest.mark.parametrize("mode", ["exact", "ignore"])
def test_upload_filter_can_misbehave_like_some_servers_do(tmp_path, mode):
    httpd, base = _start(tmp_path, upload_filter=mode)
    client = Client(base)
    client.login()
    everything = client.pages("/iclock/api/transactions/")
    stamp = everything[-1]["upload_time"].replace(" ", "%20")
    rows = client.pages(f"/iclock/api/transactions/?upload_time={stamp}")
    assert (len(rows) == len(everything)) if mode == "ignore" else len(rows) < len(everything)
    httpd.shutdown()


def test_one_transaction_by_id_and_404_once_deleted(server):
    row = server.call("POST", "/mock/punch", {"emp_code": "1001"})[1]
    assert server.get(f"/iclock/api/transactions/{row['id']}/")[1]["emp_code"] == "1001"
    assert server.call("DELETE", f"/mock/transactions/{row['id']}")[0] == 200
    assert server.get(f"/iclock/api/transactions/{row['id']}/")[0] == 404
    assert server.call("DELETE", f"/mock/transactions/{row['id']}")[0] == 404


def test_ids_only_grow_across_restarts_and_resets(tmp_path):
    httpd, base = _start(tmp_path)
    client = Client(base)
    client.login()
    before = client.call("POST", "/mock/punch", {"emp_code": "1001"})[1]["id"]
    httpd.shutdown()
    httpd.server_close()
    httpd, base = _start(tmp_path)                       # restart: same data directory
    client = Client(base)
    client.login()
    after = client.call("POST", "/mock/punch", {"emp_code": "1001"})[1]["id"]
    assert after > before
    client.call("POST", "/mock/reset", {})
    again = client.call("POST", "/mock/punch", {"emp_code": "1001"})[1]["id"]
    assert again > after
    httpd.shutdown()


def test_outage_fails_every_call_then_recovers(server):
    assert server.call("POST", "/mock/outage", {"seconds": 1, "status": 502})[0] == 200
    assert server.get("/iclock/api/terminals/")[0] == 502
    assert server.call("GET", "/health")[0] == 200                # liveness is never faked
    time.sleep(1.2)
    assert server.get("/iclock/api/terminals/")[0] == 200


def test_a_short_lived_token_expires(tmp_path):
    httpd, base = _start(tmp_path, token_ttl=1)
    client = Client(base)
    client.login()
    assert client.get("/iclock/api/terminals/")[0] == 200
    time.sleep(1.3)
    assert client.get("/iclock/api/terminals/")[0] == 401
    assert client.login()[0] == 200 and client.get("/iclock/api/terminals/")[0] == 200
    httpd.shutdown()


def test_creating_an_employee_follows_biotimes_rules(server):
    assert server.call("POST", "/personnel/api/employees/", {"emp_code": "7001"})[0] == 400      # no dept or area
    dept = server.pages("/personnel/api/departments/")[0]["id"]
    area = server.pages("/personnel/api/areas/")[0]["id"]
    body = {"emp_code": "7001", "first_name": "Zed", "department": dept, "area": [area]}
    status, row = server.call("POST", "/personnel/api/employees/", body)
    assert status == 201 and row["department"]["id"] == dept
    status, error = server.call("POST", "/personnel/api/employees/", body)
    assert status == 400 and "already exists" in error["emp_code"][0]
    assert any(e["emp_code"] == "7001" for e in server.pages("/personnel/api/employees/"))


def test_the_seeded_week_has_the_awkward_cases(server):
    rows = server.pages("/iclock/api/transactions/")
    by_person: dict[str, list] = {}
    for r in rows:
        by_person.setdefault(r["emp_code"], []).append(r)
    assert "2003" not in by_person                                           # a leaver: no punches
    assert any(r["punch_state"] == "2" for r in by_person["1001"])           # break punches
    night = [r for r in by_person["2001"] if r["punch_state"] == "1"]
    assert night and all(r["punch_time"][11:13] in ("04", "05", "06") for r in night)   # out next morning
    taps = [(a, b) for a, b in zip(by_person["2002"], by_person["2002"][1:])
            if a["punch_state"] == b["punch_state"] == "0"
            and (datetime.strptime(b["punch_time"], mock.TIME_FMT)
                 - datetime.strptime(a["punch_time"], mock.TIME_FMT)).seconds < 15]
    assert taps                                                              # a double tap
    ins = sum(r["punch_state"] == "0" for r in by_person["A7"])
    outs = sum(r["punch_state"] == "1" for r in by_person["A7"])
    assert outs < ins                                                        # a forgotten check-out
    late = [r for r in rows if r["upload_time"][:10] > r["punch_time"][:10]]
    assert late and late[0]["id"] == max(r["id"] for r in rows)              # the late upload has the newest id


def test_no_future_punches(server):
    now = _local_now().strftime(mock.TIME_FMT)
    assert all(r["punch_time"] <= now for r in server.pages("/iclock/api/transactions/"))


def test_your_own_fixtures_replace_the_sample_people(tmp_path):
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({
        "departments": [{"id": 5, "dept_code": "5", "dept_name": "Plant"}],
        "terminals": [{"id": 9, "sn": "ACME-1", "alias": "Door", "ip_address": "1.2.3.4"}],
        "employees": [{"id": 1, "emp_code": "55", "first_name": "Eve", "last_name": "",
                       "department": {"id": 5, "dept_code": "5", "dept_name": "Plant"},
                       "enable_attendance": True}]}))
    httpd, base = _start(tmp_path / "data", fixtures=str(fixtures))
    client = Client(base)
    client.login()
    assert [e["emp_code"] for e in client.pages("/personnel/api/employees/")] == ["55"]
    assert [t["sn"] for t in client.pages("/iclock/api/terminals/")] == ["ACME-1"]
    assert client.pages("/iclock/api/transactions/") == []                   # nothing invented for them
    assert client.call("POST", "/mock/punch", {"emp_code": "55", "terminal": "ACME-1"})[0] == 201
    httpd.shutdown()

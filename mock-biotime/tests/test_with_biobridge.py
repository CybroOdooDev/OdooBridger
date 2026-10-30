"""BioBridge's real BioTime connector against the mock: the point of the mock.

Skipped when BioBridge is not next to this folder (the mock ships on its own).
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BIOBRIDGE = Path(__file__).resolve().parents[2] / "services" / "biobridge"
if not (BIOBRIDGE / "app").is_dir():
    pytest.skip("BioBridge is not alongside the mock", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as mock  # noqa: E402

sys.path.insert(0, str(BIOBRIDGE))
from app.integrations.base import EmployeeRecord, SourceConfig  # noqa: E402
from app.integrations.providers.biotime import BioTimeProvider  # noqa: E402


@pytest.fixture()
def provider(tmp_path):
    argv = ["--host", "127.0.0.1", "--port", "0", "--data-dir", str(tmp_path), "--interval", "0",
            "--tz", "Asia/Kolkata", "--no-log", "--max-page-size", "7"]
    httpd = mock.build(mock.parse_args(argv))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def make(**options):
        return BioTimeProvider(SourceConfig(base_url=f"http://127.0.0.1:{httpd.server_address[1]}",
                                            username="admin", password="admin", timezone="Asia/Kolkata",
                                            options={"auth_type": "token", **options}))
    yield make
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("auth", ["token", "jwt"])
def test_connection_test_passes(provider, auth):
    info = provider(auth_type=auth).test_connection()
    assert info.ok and "6 employee" in info.message


def test_wrong_password_is_reported(provider):
    bad = provider()
    bad.config.password = "nope"
    assert not bad.test_connection().ok


def test_a_sync_reads_every_punch_across_pages(provider):
    p = provider()
    punches = list(p.fetch_punches(since=datetime.now() - timedelta(days=10)))
    assert len(punches) > 60 and len({x.external_id for x in punches}) == len(punches)
    assert not p.truncated


def test_the_late_upload_is_found_and_the_filter_is_learned(provider):
    p = provider()
    since = datetime.now() + timedelta(hours=5, minutes=30) - timedelta(days=1)
    ids = {x.external_id for x in p.fetch_punches(since=since)}
    assert p.config_updates == {"biotime_upload_filter": "from"}
    assert max(int(i) for i in ids) == max(int(x.external_id) for x in provider().fetch_punches())


def test_employees_terminals_and_creating_an_employee(provider):
    p = provider()
    assert {e.emp_code: e.is_active for e in p.fetch_employees()}["2003"] is False
    assert {t.serial_number for t in p.fetch_terminals()} == {"MOCK-GATE-01", "MOCK-GATE-02", "MOCK-GATE-03"}
    made = p.create_employee(EmployeeRecord(external_id=None, emp_code="9001", first_name="New",
                                            last_name="Hire", department="Logistics", is_active=True, raw={}))
    assert made.emp_code == "9001" and made.department == "Logistics"


def test_id_gap_reading_finds_a_punch_the_time_window_missed(provider):
    import json
    import urllib.request
    # With the upload-time read switched off, only the id read can find it.
    p = provider(biotime_upload_filter="off")
    base = p.base_url
    before = list(p.fetch_punches(since=datetime.now() - timedelta(days=10)))
    top = max(int(x.external_id) for x in before)
    request = urllib.request.Request(base + "/mock/punch", method="POST", data=json.dumps(
        {"emp_code": "1001", "state": "out", "at": "2026-10-01 17:00:00"}).encode())
    new_id = json.loads(urllib.request.urlopen(request).read())["id"]
    again = provider(biotime_upload_filter="off")
    list(again.fetch_punches(since=datetime.now() - timedelta(days=10)))   # the time window misses it
    found = [int(x.external_id) for x in again.fetch_punches_after(top)]
    assert new_id in found

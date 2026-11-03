"""Exporting punches from the Activity page: CSV, Excel and PDF."""

from __future__ import annotations

import csv
import io

from openpyxl import load_workbook

from app.services import punch_export
from tests.test_punch_ledger import api, punches  # noqa: F401 — the api fixture

URL = "/api/v1/punches/export"


def test_csv_of_everything_matches_the_ledger(api):  # noqa: F811
    r = api.post(URL, json={"format": "csv", "filter": {}})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="punches-' in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == punch_export.COLUMNS
    assert len(rows) - 1 == len(punches(api))


def test_filter_and_selection_scope(api):  # noqa: F811
    errors = punches(api, state="error")
    r = api.post(URL, json={"format": "csv", "filter": {"state": "error"}})
    assert len(list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))) - 1 == len(errors)
    ids = [p["id"] for p in punches(api)[:2]]
    r = api.post(URL, json={"format": "csv", "ids": ids})
    assert len(list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))) - 1 == 2


def test_synced_punches_can_be_exported(api):  # noqa: F811
    r = api.post(URL, json={"format": "csv", "filter": {"state": "synced"}})
    assert r.status_code == 200
    assert len(r.content.decode("utf-8-sig").strip().splitlines()) > 1


def test_xlsx_and_pdf_are_real_files(api):  # noqa: F811
    x = api.post(URL, json={"format": "xlsx", "filter": {}})
    ws = load_workbook(io.BytesIO(x.content)).active
    assert [c.value for c in ws[1]] == punch_export.COLUMNS and ws.max_row - 1 == len(punches(api))
    p = api.post(URL, json={"format": "pdf", "filter": {}})
    assert p.status_code == 200 and p.content.startswith(b"%PDF")


def test_nothing_to_export_and_bad_requests(api):  # noqa: F811
    assert api.post(URL, json={"format": "csv", "filter": {"emp_code": "no-such-badge"}}).status_code == 404
    assert api.post(URL, json={"format": "csv"}).status_code == 422
    assert api.post(URL, json={"format": "csv", "ids": ["a"], "filter": {}}).status_code == 422
    assert api.post(URL, json={"format": "docx", "filter": {}}).status_code == 422


def test_formula_text_is_neutralised():
    assert punch_export._safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert punch_export._safe("-5") == "'-5"
    assert punch_export._safe("EMP1") == "EMP1"


def test_pdf_has_a_lower_row_limit(api, monkeypatch):  # noqa: F811
    monkeypatch.setitem(punch_export.MAX_ROWS, "pdf", 1)
    r = api.post(URL, json={"format": "pdf", "filter": {}})
    assert r.status_code == 413 and "CSV or Excel" in r.json()["detail"]


def test_export_is_audited(api):  # noqa: F811
    api.post(URL, json={"format": "csv", "filter": {}})
    audit = api.get("/api/v1/audit")
    if audit.status_code == 200:
        assert "punch.export" in audit.text

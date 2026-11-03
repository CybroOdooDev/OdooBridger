"""Punch ledger exports: CSV, Excel and PDF of the punches on the Activity page.

One row shape for all three, so a file and the screen always agree.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

COLUMNS = ["Punch time (local)", "Punch time (UTC)", "Badge", "Direction", "Device", "State",
           "Odoo attendance ID", "Attempts", "Detail"]

#: Hard limits so one request can't exhaust memory. PDF is lower: it is for reading, not analysis.
MAX_ROWS = {"csv": 50_000, "xlsx": 50_000, "pdf": 2_000}

MEDIA = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


def _stamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def row_of(punch) -> list:
    return [
        _stamp(punch.punch_time_local or punch.punch_time_utc),
        _stamp(punch.punch_time_utc),
        punch.emp_code,
        punch.direction,
        punch.terminal_sn or "",
        punch.process_state,
        punch.odoo_attendance_id if punch.odoo_attendance_id is not None else "",
        punch.attempts,
        punch.error_message or "",
    ]


def _safe(cell):
    """Stop spreadsheet apps reading text as a formula (CSV/Excel injection): a badge or error
    message beginning with = + - @ would otherwise run when the file is opened."""
    if isinstance(cell, str) and cell[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + cell
    return cell


def to_csv(rows: list[list]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    writer.writerows([[_safe(c) for c in r] for r in rows])
    return ("﻿" + out.getvalue()).encode("utf-8")  # BOM so Excel reads UTF-8 properly


def to_xlsx(rows: list[list]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Punches"
    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in rows:
        ws.append([_safe(c) for c in r])
    ws.freeze_panes = "A2"
    widths = [20, 20, 12, 10, 18, 12, 18, 9, 50]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_pdf(rows: list[list], *, title: str, subtitle: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=7, leading=8.5)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24,
                            topMargin=28, bottomMargin=28, title=title)
    # Drop "Punch time (UTC)" and "Attempts" on paper; they're in the spreadsheet formats.
    keep = [0, 2, 3, 4, 5, 6, 8]
    head = [COLUMNS[i] for i in keep]
    body = [[Paragraph(escape(str(r[i])), small) for i in keep] for r in rows]
    table = Table([head] + body, repeatRows=1,
                  colWidths=[95, 60, 45, 100, 65, 70, 380])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eceefb")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c9ccd6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    doc.build([Paragraph(escape(title), styles["Title"]),
               Paragraph(escape(subtitle), styles["BodyText"]), Spacer(1, 8), table])
    return buf.getvalue()

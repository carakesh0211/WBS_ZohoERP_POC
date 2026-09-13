"""The .xlsx rendering of a verified export result (Stream C, 2026-09-13).

WHAT THIS IS
============

An export job's chunks are CSV text (migration 018): append-only, digested
at completion, re-verified before a byte is served. This module turns that
VERIFIED text plus the job's own metadata into a workbook, at download time,
in memory. Nothing is stored twice and the integrity check stays the one
`exports.read_result` already makes.

THE WORKBOOK
============

Four sheets, in this order:

* **Summary** -- the report's title and dataset, when it was generated and
  by whom, how many rows, how many filters, the scope note (rows are those
  the requester's own scope permits -- nothing wider), the masking note, the
  CSV result's SHA-256 (so the two formats can be reconciled), the job id,
  the application version.
* **Data** -- one header row, frozen (`A2`), an autofilter over the whole
  range, column widths from the content (capped), every cell TYPED from the
  column's declared kind: money as a number in RUPEES with the Indian
  grouping format, integers and numerics as numbers, dates and timestamps as
  real date cells with a format, booleans as booleans, text as text. A
  totals row closes the sheet with `SUM` formulas over the data range for
  every money, integer and numeric column -- a range built from the counted
  rows, never from a lookup, and no other formula anywhere in the file.
* **Applied Filters** -- the filters the job ran with, one per row; "none"
  when it ran unfiltered.
* **Metadata** -- every column with its kind and the cell format used, the
  chunking, the money note.

FORMULA INJECTION. Every string cell is written as a STRING: openpyxl would
otherwise treat a value beginning with ``=`` as a formula, and a WBS
description reading ``=cmd|' /c calc'!A0`` is data. The CSV renderer already
prefixes an apostrophe to such text (`exports._render_text`); the apostrophe
is kept, visibly, and the cell's data type is forced to text as well, so the
guard holds even for a value that reached the CSV some other way.

MONEY. A paise column is written as rupees = paise / 100, computed by integer
division into a Decimal and handed to the cell as that Decimal. A cell holds
an IEEE double, exact to the paisa below 2^53 paise (about ninety trillion
rupees); the CSV text remains the exact record and the Summary sheet says
so. No float is created in this module.
"""
from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

#: Indian grouping: lakhs and crores above a thousand, two decimals. Negative
#: values fall through to the last section and keep their sign.
INDIAN_CURRENCY_FORMAT = (
    '[>=10000000]##\\,##\\,##\\,##0.00;[>=100000]##\\,##\\,##0.00;#,##0.00')
INT_FORMAT = "0"
NUMERIC_FORMAT = "#,##0.####"
DATE_FORMAT = "yyyy-mm-dd"
TIMESTAMP_FORMAT = 'yyyy-mm-dd hh:mm:ss "UTC"'

MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
#: The most rows a workbook is rendered for. openpyxl keeps every cell as
#: an object (a few hundred bytes each); at this cap a wide dataset stays
#: well inside the AppSail memory budget. Larger results are served as CSV.
MAX_ROWS = 20000
#: The most rows a workbook is rendered for. openpyxl keeps every cell as
#: an object (a few hundred bytes each); at this cap a wide dataset stays
#: well inside the AppSail memory budget. Larger results are served as CSV.
MAX_ROWS = 20000

_SUMMED_KINDS = frozenset({"paise", "int", "numeric"})
_MAX_WIDTH = 60
_MIN_WIDTH = 8

_HEADER_FONT = Font(bold=True)
_HEADER_FILL = PatternFill("solid", fgColor="DDE3EA")
_TOTAL_FONT = Font(bold=True)


def _safe_sheet_title(title: str) -> str:
    cleaned = re.sub(r"[\[\]\*\?/\\:]", " ", title).strip() or "Sheet"
    return cleaned[:31]


def _text_cell(ws, row: int, col: int, value: str):
    """A STRING cell, whatever the text begins with."""
    cell = ws.cell(row=row, column=col)
    cell.value = value
    cell.data_type = "s"
    return cell


def _parse(kind: str, text: str) -> Any:
    """The typed value of one CSV cell, or the text when it cannot be typed.

    The CSV writer is `exports.render_value`; each branch here is its inverse
    and refuses nothing -- an unparseable cell stays text rather than
    becoming a wrong number.
    """
    if text == "":
        return None
    try:
        if kind == "paise":
            # "1234.56" -> Decimal('1234.56'); integer paise / 100, exactly.
            whole, _, frac = text.partition(".")
            negative = whole.startswith("-")
            whole = whole.lstrip("-")
            paise = int(whole) * 100 + int((frac + "00")[:2])
            return Decimal(-paise if negative else paise) / Decimal(100)
        if kind == "int":
            return int(text)
        if kind == "numeric":
            return Decimal(text)
        if kind == "bool":
            return text.strip().lower() == "true"
        if kind == "date":
            return date.fromisoformat(text)
        if kind == "timestamp":
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
    except (ValueError, InvalidOperation):
        return text
    return text


def _number_format(kind: str) -> str | None:
    return {"paise": INDIAN_CURRENCY_FORMAT, "int": INT_FORMAT,
            "numeric": NUMERIC_FORMAT, "date": DATE_FORMAT,
            "timestamp": TIMESTAMP_FORMAT}.get(kind)


def _kinds_for(columns: Sequence[Any], column_order: Sequence[str]) -> list[str]:
    by_name = {c.name: c.kind for c in columns}
    return [by_name.get(name, "text") for name in column_order]


def render_workbook(*, dataset_name: str, dataset_title: str,
                    columns: Sequence[Any], column_order: Sequence[str],
                    csv_body: str, filters: Mapping[str, Any] | None,
                    requested_by: str, export_job_id: str, rows_total: int,
                    csv_sha256: str, chunk_rows: int, generated_at: datetime,
                    app_version: str, finished_at: str | None = None,
                    masking_note: str | None = None) -> bytes:
    """The workbook, as bytes. Pure: no database, no clock of its own."""
    reader = csv.reader(io.StringIO(csv_body))
    header = next(reader, None) or list(column_order)
    if list(header) != list(column_order):
        raise ValueError("the CSV header does not match the job's column order")
    kinds = _kinds_for(columns, column_order)
    filters = dict(filters or {})

    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    data = wb.create_sheet("Data")
    applied = wb.create_sheet("Applied Filters")
    meta = wb.create_sheet("Metadata")

    # ------------------------------------------------------------- Data
    for col, name in enumerate(column_order, start=1):
        cell = _text_cell(data, 1, col, name)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(vertical="center")
    widths = [max(_MIN_WIDTH, min(_MAX_WIDTH, len(name) + 2)) for name in column_order]
    row_count = 0
    for row_no, row in enumerate(reader, start=2):
        if len(row) != len(kinds):
            raise ValueError(f"row {row_no - 1} carries {len(row)} cells for "
                             f"{len(kinds)} columns")
        row_count += 1
        for col, (kind, text) in enumerate(zip(kinds, row), start=1):
            value = _parse(kind, text)
            if isinstance(value, str) or value is None:
                cell = _text_cell(data, row_no, col, value or "")
            else:
                cell = data.cell(row=row_no, column=col, value=value)
                fmt = _number_format(kind)
                if fmt:
                    cell.number_format = fmt
            widths[col - 1] = max(widths[col - 1], min(_MAX_WIDTH, len(text) + 2))
    if row_count != rows_total:
        raise ValueError(f"the CSV holds {row_count} rows; the job records {rows_total}")
    last_data_row = row_count + 1
    data.freeze_panes = "A2"
    if row_count:
        data.auto_filter.ref = f"A1:{get_column_letter(len(column_order))}{last_data_row}"
        total_row = last_data_row + 1
        label = _text_cell(data, total_row, 1, "Total")
        label.font = _TOTAL_FONT
        for col, kind in enumerate(kinds, start=1):
            if kind in _SUMMED_KINDS:
                letter = get_column_letter(col)
                cell = data.cell(row=total_row, column=col,
                                 value=f"=SUM({letter}2:{letter}{last_data_row})")
                cell.font = _TOTAL_FONT
                fmt = _number_format(kind)
                if fmt:
                    cell.number_format = fmt
    for col, width in enumerate(widths, start=1):
        data.column_dimensions[get_column_letter(col)].width = width

    # ---------------------------------------------------------- Summary
    rows = [
        ("Report", dataset_title),
        ("Dataset", dataset_name),
        ("Generated at (UTC)", generated_at.astimezone(timezone.utc).isoformat()),
        ("Completed at", finished_at or ""),
        ("Requested by", requested_by),
        ("Rows", rows_total),
        ("Filters applied", len(filters)),
        ("Scope", "Rows are those the requester's own scope permits at the "
                  "time the job ran; nothing wider is in this file."),
        ("Masking", masking_note or "No column in this dataset is masked; every "
                                    "value is the one the screen shows."),
        ("Money", "Money columns are rupees to the paisa, typed as numbers with "
                  "the Indian grouping format; the CSV result of the same job "
                  "is the exact integer-paise record."),
        ("Formulas", "The only formulas are the SUM totals on the Data sheet, "
                     "over the data range; nothing external or volatile."),
        ("CSV result SHA-256", csv_sha256),
        ("Export job", export_job_id),
        ("Application", app_version),
    ]
    for r, (key, value) in enumerate(rows, start=1):
        k = _text_cell(summary, r, 1, key)
        k.font = _HEADER_FONT
        if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
            summary.cell(row=r, column=2, value=value)
        else:
            _text_cell(summary, r, 2, str(value))
    summary.column_dimensions["A"].width = 24
    summary.column_dimensions["B"].width = 90

    # -------------------------------------------------- Applied Filters
    for col, name in enumerate(("Filter", "Value"), start=1):
        cell = _text_cell(applied, 1, col, name)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
    if filters:
        for r, (key, value) in enumerate(sorted(filters.items()), start=2):
            _text_cell(applied, r, 1, str(key))
            rendered = (", ".join(str(v) for v in value)
                        if isinstance(value, (list, tuple, set)) else str(value))
            _text_cell(applied, r, 2, rendered)
    else:
        _text_cell(applied, 2, 1, "none")
        _text_cell(applied, 2, 2, "the job ran unfiltered within the requester's scope")
    applied.freeze_panes = "A2"
    applied.column_dimensions["A"].width = 24
    applied.column_dimensions["B"].width = 80

    # --------------------------------------------------------- Metadata
    for col, name in enumerate(("Column", "Kind", "Cell format"), start=1):
        cell = _text_cell(meta, 1, col, name)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
    for r, (name, kind) in enumerate(zip(column_order, kinds), start=2):
        _text_cell(meta, r, 1, name)
        _text_cell(meta, r, 2, kind)
        _text_cell(meta, r, 3, _number_format(kind) or "text")
    r = len(column_order) + 3
    for key, value in (("Chunk rows", str(chunk_rows)),
                       ("Rows", str(rows_total)),
                       ("Renderer", "pg/exports_xlsx.py (openpyxl)"),
                       ("Media type", MEDIA_TYPE)):
        k = _text_cell(meta, r, 1, key)
        k.font = _HEADER_FONT
        _text_cell(meta, r, 2, value)
        r += 1
    meta.freeze_panes = "A2"
    meta.column_dimensions["A"].width = 28
    meta.column_dimensions["B"].width = 20
    meta.column_dimensions["C"].width = 48

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def xlsx_filename(dataset_name: str, generated_at: datetime) -> str:
    """`<report>-<YYYY-MM-DD>.xlsx`: the report's name and the date."""
    return f"{dataset_name}-{generated_at.astimezone(timezone.utc):%Y-%m-%d}.xlsx"

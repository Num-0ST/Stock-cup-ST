"""
File readers for the Excel import engine.

Every reader returns a *grid*: list of rows, each row a list of cell values (str | int | float | None).
Supported: XLSX, real XLS, CSV/TSV, and HTML tables saved with an .xls extension
(including PivotTable.js exports, which are flattened to one row per (row-labels, column-labels) cell).
"""
from __future__ import annotations

import csv
import io
import math
import re
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any

from .engine import StockError

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_ROWS = 200_000
MAX_COLS = 200
MAX_SHEETS = 30


class BadFile(StockError):
    status, code = 422, "BAD_FILE"


# ---------------------------------------------------------------- cell normalisation


def norm_cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        return int(v) if v.is_integer() and abs(v) < 1e15 else v
    s = str(v).strip()
    return s if s else None


def _row_is_empty(row: list) -> bool:
    return all(c is None for c in row)


# ---------------------------------------------------------------- sniffing


def sniff_kind(data: bytes, filename: str) -> str:
    if len(data) == 0:
        raise BadFile("ไฟล์ว่างเปล่า", code_hint="EMPTY_FILE")
    if data[:4] == b"PK\x03\x04":
        return "xlsx"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xls"
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<") and b"<table" in data[:2_000_000].lower():
        return "html"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("csv", "tsv", "txt"):
        return "csv"
    raise BadFile("ไม่รองรับรูปแบบไฟล์นี้ (รองรับ XLSX, XLS, CSV)", code_hint="UNSUPPORTED_FILE")


# ---------------------------------------------------------------- XLSX / XLS / CSV


def _xlsx_sheets(data: bytes) -> list[str]:
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001  (corrupt zip, wrong format, ...)
        raise BadFile("เปิดไฟล์ XLSX ไม่ได้", code_hint="CORRUPT_FILE", reason=str(e)[:120])
    names = list(wb.sheetnames)
    wb.close()
    return names[:MAX_SHEETS]


def _xlsx_grid(data: bytes, sheet: str) -> list[list]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        if sheet not in wb.sheetnames:
            raise BadFile("ไม่พบ Sheet ที่เลือก", code_hint="SHEET_NOT_FOUND")
        grid = []
        for row in wb[sheet].iter_rows(values_only=True):
            r = [norm_cell(c) for c in row[:MAX_COLS]]
            grid.append(r)
            if len(grid) > MAX_ROWS:
                raise BadFile(f"ไฟล์มีมากกว่า {MAX_ROWS:,} แถว", code_hint="TOO_MANY_ROWS")
    finally:
        wb.close()
    while grid and _row_is_empty(grid[-1]):
        grid.pop()
    return grid


def _xls_book(data: bytes):
    import xlrd

    try:
        return xlrd.open_workbook(file_contents=data)
    except Exception as e:  # noqa: BLE001
        raise BadFile("เปิดไฟล์ XLS ไม่ได้", code_hint="CORRUPT_FILE", reason=str(e)[:120])


def _xls_grid(data: bytes, sheet: str) -> list[list]:
    import xlrd

    book = _xls_book(data)
    if sheet not in book.sheet_names():
        raise BadFile("ไม่พบ Sheet ที่เลือก", code_hint="SHEET_NOT_FOUND")
    ws = book.sheet_by_name(sheet)
    if ws.nrows > MAX_ROWS:
        raise BadFile(f"ไฟล์มีมากกว่า {MAX_ROWS:,} แถว", code_hint="TOO_MANY_ROWS")
    grid = []
    for r in range(ws.nrows):
        row = []
        for c in range(min(ws.ncols, MAX_COLS)):
            t, v = ws.cell_type(r, c), ws.cell_value(r, c)
            if t == xlrd.XL_CELL_DATE:
                try:
                    v = xlrd.xldate_as_datetime(v, book.datemode)
                except Exception:  # noqa: BLE001
                    v = None
            elif t in (xlrd.XL_CELL_ERROR, xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                v = None
            row.append(norm_cell(v))
        grid.append(row)
    while grid and _row_is_empty(grid[-1]):
        grid.pop()
    return grid


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp874", "cp1252"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("latin-1")


def _csv_grid(data: bytes) -> list[list]:
    text = _decode_text(data)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = "\t" if sample.count("\t") > sample.count(",") else ","
    grid = []
    for row in csv.reader(io.StringIO(text), delimiter=delim):
        r = [norm_cell(c) for c in row[:MAX_COLS]]
        r = [_numberish(c) for c in r]
        grid.append(r)
        if len(grid) > MAX_ROWS:
            raise BadFile(f"ไฟล์มีมากกว่า {MAX_ROWS:,} แถว", code_hint="TOO_MANY_ROWS")
    while grid and _row_is_empty(grid[-1]):
        grid.pop()
    return grid


_NUM_RE = re.compile(r"^-?\d{1,15}(\.\d+)?$")


def _numberish(v):
    """CSV cells are text; keep plain numbers numeric so 'qty' behaves the same as in XLSX.
    Leading-zero strings (codes like 00123) stay text."""
    if isinstance(v, str) and _NUM_RE.match(v) and not (len(v) > 1 and v[0] == "0" and v[1] != "."):
        return float(v) if "." in v else int(v)
    return v


# ---------------------------------------------------------------- HTML tables (+ PivotTable.js)


class _Cell:
    __slots__ = ("text", "cls", "colspan", "rowspan")

    def __init__(self, cls="", colspan=1, rowspan=1):
        self.text, self.cls, self.colspan, self.rowspan = "", cls, colspan, rowspan


class _Table:
    def __init__(self, cls=""):
        self.cls = cls
        self.rows: list[list[_Cell]] = []


class _HtmlTables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[_Table] = []
        self._stack: list[_Table] = []
        self._cell: _Cell | None = None
        self._row: list[_Cell] | None = None

    @staticmethod
    def _int(v, default=1, hi=1000):
        try:
            return max(1, min(int(v), hi))
        except (TypeError, ValueError):
            return default

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            t = _Table(a.get("class", "") or "")
            self.tables.append(t)
            self._stack.append(t)
        elif tag == "tr" and self._stack:
            self._row = []
            self._stack[-1].rows.append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            self._cell = _Cell(a.get("class", "") or "", self._int(a.get("colspan"), hi=MAX_COLS), self._int(a.get("rowspan"), hi=MAX_ROWS))
            self._row.append(self._cell)
        elif tag == "br" and self._cell is not None:
            self._cell.text += " "

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._cell = None
        elif tag == "tr":
            self._row = None
        elif tag == "table" and self._stack:
            self._stack.pop()
            self._row = None
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.text += data


def _expand(table: _Table) -> list[list[_Cell | None]]:
    """Resolve colspan/rowspan into a rectangular grid (spanned positions point at the same cell)."""
    n_rows = len(table.rows)
    occupied: dict[tuple[int, int], _Cell] = {}
    width = 0
    for ri, row in enumerate(table.rows):
        ci = 0
        for cell in row:
            while (ri, ci) in occupied:
                ci += 1
            for dr in range(min(cell.rowspan, n_rows - ri)):
                for dc in range(cell.colspan):
                    if ci + dc < MAX_COLS + 20:
                        occupied[(ri + dr, ci + dc)] = cell
            if len(occupied) > 6_000_000:  # guard against hostile span values
                raise BadFile("ตารางในไฟล์ใหญ่เกินไป", code_hint="TOO_MANY_ROWS")
            ci += cell.colspan
            width = max(width, min(ci, MAX_COLS + 20))
    return [[occupied.get((r, c)) for c in range(width)] for r in range(n_rows)]


def _txt(c: _Cell | None) -> str:
    return re.sub(r"\s+", " ", c.text).strip() if c else ""


def _cls(c: _Cell | None) -> str:
    return c.cls if c else ""


def _to_number(s: str):
    if not s:
        return None
    try:
        f = float(s.replace(",", ""))
    except ValueError:
        return s
    if not math.isfinite(f):
        return None
    return int(f) if f.is_integer() and abs(f) < 1e15 else f


def _flatten_pivot(grid: list[list[_Cell | None]]) -> list[list] | None:
    """PivotTable.js export -> flat rows: [row labels..., column labels..., (DATE), VALUE]."""
    k = None
    for i, row in enumerate(grid):
        # the row-label axis row starts with an axis label in column 0
        # (rows above it start with the empty corner cell; 'Day' header cells may span into it)
        if row and "pvtAxisLabel" in _cls(row[0]):
            k = i
            break
    if k is None:
        return None
    axis_row = grid[k]
    n_row = 0
    while n_row < len(axis_row) and "pvtAxisLabel" in _cls(axis_row[n_row]):
        n_row += 1
    if n_row == 0:
        return None
    row_names = [(_txt(axis_row[i]) or f"ROW_{i + 1}") for i in range(n_row)]
    width = len(axis_row)
    has_total_col = "pvtTotalLabel" in _cls(grid[0][width - 1]) if grid[0] else False

    if k == 0:  # no column attributes: one value column (the Totals column)
        col_names: list[str] = []
        first_val, last_val = width - 1, width - 1
    else:
        col_names = [(_txt(grid[t][n_row]) or f"COL_{t + 1}") for t in range(k)]
        first_val = n_row + 1
        last_val = width - 2 if has_total_col else width - 1

    low = [n.lower() for n in col_names]
    has_date = all(x in low for x in ("year", "month", "day"))
    out_header = row_names + col_names + (["DATE"] if has_date else []) + ["VALUE"]
    out: list[list] = [out_header]
    for i in range(k + 1, len(grid)):
        row = grid[i]
        if not row or "pvtTotalLabel" in _cls(row[0]):
            continue
        labels = [_txt(row[c]) or None for c in range(n_row)]
        for j in range(first_val, last_val + 1):
            val = _txt(row[j]) if j < len(row) else ""
            if not val:
                continue
            keys = [_txt(grid[t][j]) for t in range(k)] if k else []
            extra = []
            if has_date:
                y, m, d = (keys[low.index("year")], keys[low.index("month")], keys[low.index("day")])
                try:
                    extra = [f"{int(y):04d}-{int(m):02d}-{int(d):02d}"]
                except ValueError:
                    extra = [None]
            out.append([norm_cell(x) for x in labels] + keys + extra + [_to_number(val)])
            if len(out) > MAX_ROWS:
                raise BadFile(f"ไฟล์มีมากกว่า {MAX_ROWS:,} แถว", code_hint="TOO_MANY_ROWS")
    return out


def _html_sheets(data: bytes) -> dict[str, list[list]]:
    p = _HtmlTables()
    p.feed(_decode_text(data))
    sheets: dict[str, list[list]] = {}
    for n, t in enumerate(p.tables[:MAX_SHEETS], start=1):
        if not t.rows:
            continue
        grid = _expand(t)
        if "pvtTable" in t.cls:
            flat = _flatten_pivot(grid)
            if flat is not None:
                sheets["Pivot" if "Pivot" not in sheets else f"Pivot {n}"] = flat
                continue
        rows = [[norm_cell(_txt(c) or None) for c in row[:MAX_COLS]] for row in grid]
        rows = [[_numberish(c) for c in r] for r in rows]
        while rows and _row_is_empty(rows[-1]):
            rows.pop()
        if rows:
            sheets[f"Table {n}"] = rows
    if not sheets:
        raise BadFile("ไม่พบตารางข้อมูลในไฟล์", code_hint="NO_TABLE")
    return sheets


# ---------------------------------------------------------------- public API


def list_sheets(data: bytes, kind: str) -> list[str]:
    if kind == "xlsx":
        return _xlsx_sheets(data)
    if kind == "xls":
        return list(_xls_book(data).sheet_names())[:MAX_SHEETS]
    if kind == "html":
        return list(_html_sheets(data).keys())
    return ["Sheet1"]


def read_grid(data: bytes, kind: str, sheet: str | None) -> tuple[list[list], str]:
    """Return (grid, sheet_name_used)."""
    names = list_sheets(data, kind)
    name = sheet if sheet in names else names[0]
    if kind == "xlsx":
        return _xlsx_grid(data, name), name
    if kind == "xls":
        return _xls_grid(data, name), name
    if kind == "html":
        return _html_sheets(data)[name], name
    return _csv_grid(data), name


def make_headers(row: list) -> list[str]:
    """Unique, non-empty header names for a header row."""
    out, seen = [], {}
    for i, v in enumerate(row):
        name = str(v).strip() if v is not None else ""
        if not name:
            name = f"COL_{i + 1}"
        base, n = name, seen.get(name, 0)
        seen[base] = n + 1
        if n:
            name = f"{base}_{n + 1}"
        out.append(name)
    return out


def guess_header_row(grid: list[list]) -> int:
    """1-based number of the first 'wide' row near the top (the row that looks like column titles)."""
    top = grid[:30]
    widest = max((sum(c is not None for c in r) for r in top), default=0)
    for i, r in enumerate(top, start=1):
        filled = sum(c is not None for c in r)
        textual = sum(isinstance(c, str) for c in r)
        if filled >= max(2, int(widest * 0.6)) and textual >= filled * 0.6:
            return i
    return 1

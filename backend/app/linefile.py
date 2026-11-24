"""Fill a document's lines from an Excel / CSV file (e.g. 200-300 opening-balance lines).

The file needs a product column (code, barcode or exact name) and a quantity column, optionally a unit column -
or separate quantity columns per unit (ลัง / แพ็ค / ชิ้น). Headers are recognised in Thai, Lao and English; without
a header row the columns are guessed from the data. Nothing is saved here: the lines go into the document form
for the user to check, and the document is posted the normal way.
"""
from __future__ import annotations

import io
import re
from decimal import Decimal, InvalidOperation

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from . import readers
from .billscan import unit_code_of

MAX_BYTES = 10 * 1024 * 1024
MAX_LINES = 5000

KEYS = {
    "code": ["รหัสสินค้า", "รหัส", "code", "item code", "itemcode", "sku", "product code", "productcode", "ລະຫັດ", "barcode", "บาร์โค้ด", "บาร์โค้ต"],
    "name": ["ชื่อสินค้า", "ชื่อ", "รายการ", "สินค้า", "name", "description", "product", "item", "ຊື່", "ລາຍການ", "ສິນຄ້າ"],
    "qty": ["จำนวน", "qty", "quantity", "ยอดยกมา", "ยอดคงเหลือ", "คงเหลือ", "ยอด", "balance", "on hand", "onhand", "stock",
            "สต็อก", "สต๊อก", "ຈຳນວນ", "ຍອດ", "ຄົງເຫຼືອ", "ສະຕັອກ", "ສະຕ໊ອກ"],
    "unit": ["หน่วย", "unit", "uom", "ຫົວໜ່ວຍ"],
    "CT": ["ct", "ctn", "carton", "case", "ลัง", "หีบ", "ລັງ", "ແກັດ"],
    "PK": ["pk", "pack", "แพ็ค", "แพ็ก", "แพค", "ห่อ", "ແພັກ", "ແພັກ", "ແພກ", "ຫໍ່"],
    "PC": ["pc", "pcs", "piece", "ชิ้น", "อัน", "ຊິ້ນ", "ອັນ"],
}
NOT_QTY = re.compile(r"(เงิน|ราคา|มูลค่า|amount|price|value|cost|ต้นทุน|ລາຄາ|ມູນຄ່າ|ເງິນ)", re.I)


def _norm(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _num(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v).replace(",", "").replace(" ", ""))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _classify(cell) -> str | None:
    n = _norm(cell)
    if not n:
        return None
    for unit in ("CT", "PK", "PC"):          # a column titled just "ลัง" / "CT" = quantity in that unit
        if n in KEYS[unit] or n.strip("() ") in KEYS[unit]:
            return "q_" + unit
    if any(k in n for k in KEYS["unit"]):
        return "unit"
    if any(k in n for k in KEYS["code"]):
        return "code"
    if any(k in n for k in KEYS["qty"]) and not NOT_QTY.search(n):
        return "qty"
    if any(k in n for k in KEYS["name"]):
        return "name"
    return None


def _roles(row: list) -> dict[str, int]:
    cols: dict[str, int] = {}
    for c, cell in enumerate(row):
        role = _classify(cell)
        if role and role not in cols:
            cols[role] = c
    return cols


def _find_header(grid: list[list]) -> tuple[int, dict, int]:
    """(header row index, {role: column}, first data row) of the most header-like row among the first 20.

    A header may span two rows, e.g. "Code | Product | Stock" with "CT | PK | PC" under the merged "Stock" cell:
    the unit columns of the second row are then added to the first row's columns."""
    best, best_cols, best_start = -1, {}, 0
    rows = grid[:21]
    for r, row in enumerate(rows[:20]):
        cols, start = _roles(row), r + 1
        nxt = _roles(rows[r + 1]) if r + 1 < len(rows) else {}
        units = {k: v for k, v in nxt.items() if k.startswith("q_") or k == "unit"}
        # a data row carries a product code / name under those titles; a real second header row leaves them empty
        # (or repeats the title, as merged cells do in some exports)
        nrow = rows[r + 1] if units else []
        own = [c for c in (cols.get("code"), cols.get("name")) if c is not None]
        if units and own and all(c >= len(nrow) or not _cell_str(nrow[c]) or _norm(nrow[c]) == _norm(row[c]) for c in own):
            cols = {**cols, **{k: v for k, v in units.items() if k not in cols}}
            start = r + 2
        if _score(cols) >= 4 and _score(cols) > (_score(best_cols) if best_cols else 0):
            best, best_cols, best_start = r, cols, start
    return best, best_cols, best_start


def _score(cols: dict) -> float:
    return (2 if "code" in cols or "name" in cols else 0) + \
           (2 if "qty" in cols or any(k.startswith("q_") for k in cols) else 0) + len(cols) * 0.1


def _load_products(conn) -> dict:
    rows = conn.execute(
        """SELECT p.product_id, p.product_code, p.name, p.barcode,
                  COALESCE((SELECT json_agg(json_build_object('unit_code', u.unit_code, 'factor_to_base', pc.factor_to_base,
                                                             'barcode', pc.barcode) ORDER BY pc.factor_to_base DESC)
                            FROM product_unit_conversions pc JOIN units u USING (unit_id)
                            WHERE pc.product_id = p.product_id), '[]'::json) AS conversions
           FROM products p WHERE p.is_active""").fetchall()
    idx = {"code": {}, "barcode": {}, "unit_barcode": {}, "name": {}}
    for p in rows:
        idx["code"][p["product_code"].upper()] = p
        if p["barcode"]:
            idx["barcode"][p["barcode"]] = p
        for cv in p["conversions"]:
            if cv.get("barcode"):
                idx["unit_barcode"][cv["barcode"]] = (p, cv["unit_code"])
        idx["name"].setdefault(_norm(p["name"]), p)
    return idx


def _match(idx: dict, code: str, name: str):
    """-> (product, unit from a unit barcode or None)"""
    if code:
        p = idx["code"].get(code.upper()) or idx["barcode"].get(code)
        if p:
            return p, None
        if code in idx["unit_barcode"]:
            return idx["unit_barcode"][code]
    if name:
        p = idx["name"].get(_norm(name))
        if p:
            return p, None
    return None, None


def _guess_columns(grid: list[list], idx: dict) -> dict:
    """No header row: the column with most known codes is the product, the first numeric column after it is qty."""
    width = max((len(r) for r in grid[:200]), default=0)
    hits = [0] * width
    nums = [0] * width
    for row in grid[:200]:
        for c, v in enumerate(row):
            s = _cell_str(v)
            if s and _match(idx, s, "")[0]:
                hits[c] += 1
            if _num(v) is not None:
                nums[c] += 1
    if not width or max(hits) == 0:
        return {}
    code_col = hits.index(max(hits))
    after = [c for c in range(code_col + 1, width) if nums[c] >= max(1, max(hits) // 2) and hits[c] == 0]
    return {"code": code_col, "qty": after[0]} if after else {}


def parse(conn, data: bytes, filename: str) -> dict:
    if len(data) > MAX_BYTES:
        raise readers.BadFile("ไฟล์ใหญ่เกิน 10 MB", code_hint="FILE_TOO_LARGE")
    kind = readers.sniff_kind(data, filename)
    idx = _load_products(conn)

    # pick the sheet that looks most like a product list
    best = None
    for sheet in readers.list_sheets(data, kind):
        grid, name = readers.read_grid(data, kind, sheet)
        hrow, cols, start = _find_header(grid)
        if hrow < 0:
            cols, start = _guess_columns(grid, idx), 0
        if not cols:
            continue
        cand = (_score(cols), name, grid, hrow, cols, start)
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:
        raise readers.BadFile("ไม่พบคอลัมน์รหัสสินค้าและจำนวนในไฟล์", code_hint="NO_COLUMNS")
    _, sheet, grid, hrow, cols, start = best
    # quantity per unit column (ลัง | แพ็ค | ชิ้น) wins over a single "quantity" column, unless a unit column says
    # which unit the quantity is in (a merged "Stock" title above CT/PK/PC is not a quantity column of its own)
    if any(k.startswith("q_") for k in cols) and "unit" not in cols:
        cols.pop("qty", None)

    lines: dict[tuple, dict] = {}
    unmatched, errors = [], []
    zero = 0
    unit_cols = {k[2:]: v for k, v in cols.items() if k.startswith("q_")}
    for r in range(start, len(grid)):
        row = grid[r]
        cell = lambda role: row[cols[role]] if role in cols and cols[role] < len(row) else None  # noqa: E731
        code, name = _cell_str(cell("code")), _cell_str(cell("name"))
        if not code and not name:
            continue
        rowno = r + 1
        if "qty" in cols:
            raw_q = cell("qty")
            qty = _num(raw_q)
            unit_txt = _cell_str(cell("unit"))
            if qty is None:
                if raw_q not in (None, ""):
                    errors.append({"row": rowno, "code": "BAD_QTY", "value": _cell_str(raw_q), "product": code or name})
                else:
                    zero += 1
                continue
            parts = [(unit_txt, qty)]
        else:
            parts = []
            for unit, c in unit_cols.items():
                q = _num(row[c]) if c < len(row) else None
                if q:
                    parts.append((unit, q))
            if not parts:
                zero += 1
                continue
        p, ub_unit = _match(idx, code, name)
        total_q = sum(q for _, q in parts)
        if not p:
            unmatched.append({"row": rowno, "code": code or None, "name": name or None, "qty": str(total_q)})
            continue
        if any(q < 0 for _, q in parts):
            errors.append({"row": rowno, "code": "NEGATIVE_QTY", "product": p["product_code"]})
            continue
        if all(q == 0 for _, q in parts):
            zero += 1
            continue
        factors = {"PC": 1, **{cv["unit_code"]: cv["factor_to_base"] for cv in p["conversions"]}}
        resolved = []
        bad = None
        for utxt, q in parts:
            u = (utxt.upper() if utxt and utxt.upper() in factors else unit_code_of(utxt)) if utxt else (ub_unit or "PC")
            if u is None:
                bad = {"row": rowno, "code": "UNKNOWN_UNIT", "unit": utxt, "product": p["product_code"]}
                break
            if u not in factors:
                bad = {"row": rowno, "code": "UNIT_NOT_SET", "unit": u, "product": p["product_code"]}
                break
            resolved.append((u, q))
        if bad:
            errors.append(bad)
            continue
        # several unit columns (ลัง | แพ็ค | ชิ้น) stay separate, so each lands in its own unit box on the form
        frac = next(((u, q) for u, q in resolved if (q * factors[u]) != (q * factors[u]).to_integral_value()), None)
        if frac:
            errors.append({"row": rowno, "code": "NOT_WHOLE_PC", "product": p["product_code"], "qty": str(frac[1]), "unit": frac[0]})
            continue
        for u, q in resolved:
            key = (p["product_id"], u)
            if key in lines:
                lines[key]["qty"] += q
                if rowno not in lines[key]["rows"]:
                    lines[key]["rows"].append(rowno)
            else:
                if len(lines) >= MAX_LINES:
                    raise readers.BadFile(f"ไฟล์มีมากกว่า {MAX_LINES} รายการ", code_hint="TOO_MANY_LINES")
                lines[key] = {"product": {k: p[k] for k in ("product_id", "product_code", "name", "barcode", "conversions")},
                              "unit_code": u, "qty": q, "rows": [rowno]}
    out = []
    for ln in lines.values():
        q = ln["qty"]
        out.append({**ln, "qty": str(int(q)) if q == q.to_integral_value() else format(q.normalize(), "f")})
    return {
        "sheet": sheet, "header_row": hrow + 1 if hrow >= 0 else None,
        "columns": {k: _col_name(grid, hrow, start, v) for k, v in cols.items()},
        "lines": out, "unmatched": unmatched, "errors": errors, "skipped_empty": zero,
        "merged": sum(1 for ln in lines.values() if len(ln["rows"]) > 1),
    }


def _col_name(grid: list[list], hrow: int, start: int, c: int) -> str:
    if hrow < 0:
        return f"COL_{c + 1}"
    for r in range(start - 1, hrow - 1, -1):          # the lowest header row that names this column
        if c < len(grid[r]) and _cell_str(grid[r][c]):
            return _cell_str(grid[r][c])
    return f"COL_{c + 1}"


def template(conn, with_products: bool) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Items"
    ws.append(["รหัสสินค้า", "ชื่อสินค้า", "จำนวน", "หน่วย"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F5FBF")
    if with_products:
        for p in conn.execute("SELECT product_code, name FROM products WHERE is_active ORDER BY product_code").fetchall():
            ws.append([p["product_code"], p["name"], None, "PC"])
    else:
        ws.append(["P00061", "ตัวอย่าง: แก้ว 16 ออนซ์", 10, "CT"])
    for col, w in zip("ABCD", (18, 42, 12, 10)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    note = wb.create_sheet("วิธีใช้")
    for line in ["กรอกจำนวนในคอลัมน์ 'จำนวน' (เว้นว่างหรือ 0 = ไม่นำเข้า)",
                 "หน่วย: PC / PK / CT หรือ ชิ้น / แพ็ค / ลัง (ว่าง = PC)",
                 "ใช้รหัสสินค้า บาร์โค้ด หรือชื่อสินค้าที่ตรงกับในระบบก็ได้",
                 "หรือใช้คอลัมน์แยกหน่วย: ลัง | แพ็ค | ชิ้น ระบบจะรวมเป็นชิ้นให้"]:
        note.append([line])
    note.column_dimensions["A"].width = 70
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

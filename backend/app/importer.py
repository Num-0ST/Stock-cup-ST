"""
Excel import engine: mapping -> row validation -> preview -> confirm.

Rules that matter for stock safety
  * Nothing touches stock until confirm_batch(); parsing/validation only writes import_rows.
  * The unit of import is the DOCUMENT: a document is posted only if every one of its rows is valid.
  * Confirm is one DB transaction: any failure rolls the whole batch back (no half-imported file).
  * Documents already in the system (same type + warehouse + doc_no) are skipped as DUPLICATE,
    so importing the same file twice cannot double-deduct stock.
  * Stock sufficiency is simulated chronologically during validation, and enforced again
    under row locks by the stock engine during confirm.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from psycopg.types.json import Jsonb

from . import engine, readers
from .engine import DocumentStateError, NotFound, StockError, ValidationFailed

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/tmp/wms_uploads"))

TARGETS = ["doc_no", "doc_date", "product_code", "barcode", "qty", "unit_code",
           "customer_code", "customer_name", "salesman"]
IMPORT_DOC_TYPES = ("SALE", "WITHDRAWAL", "RECEIVING")
INCREASE = engine.INCREASES

SYNONYMS = {
    "doc_no": ["orderid", "orderno", "docno", "documentno", "invoice", "invoiceno", "billno", "salesno",
               "เลขที่เอกสาร", "เลขที่ใบขาย", "เลขที่บิล", "เลขที่", "ເລກທີ"],
    "doc_date": ["date", "docdate", "orderdate", "invoicedate", "saledate", "วันที่", "วันที่เอกสาร", "ວັນທີ"],
    "product_code": ["productcode", "itemcode", "sku", "partno", "รหัสสินค้า", "รหัส", "ລະຫັດສິນຄ້າ"],
    "barcode": ["barcode", "ean", "upc", "บาร์โค้ด", "ບາໂຄດ"],
    "qty": ["qty", "quantity", "value", "amount", "จำนวน", "ຈຳນວນ"],
    "unit_code": ["unit", "uom", "unitcode", "หน่วย", "หน่วยนับ", "ຫົວໜ່ວຍ"],
    "customer_code": ["customer", "customercode", "custcode", "รหัสลูกค้า", "ລະຫັດລູກຄ້າ"],
    "customer_name": ["customername", "custname", "ชื่อลูกค้า", "ຊື່ລູກຄ້າ"],
    "salesman": ["salesman", "salesmancode", "salesperson", "พนักงานขาย", "ພະນັກງານຂາຍ"],
}
UNIT_ALIASES = {"CTN": "CT", "CARTON": "CT", "CASE": "CT", "BOX": "CT", "PACK": "PK", "PKT": "PK",
                "PCS": "PC", "PIECE": "PC", "PIECES": "PC", "EA": "PC", "EACH": "PC", "UNIT": "PC"}

# ---------------------------------------------------------------- errors


class BadParams(StockError):
    status, code = 422, "BAD_IMPORT_PARAMS"


class ImportStateError(StockError):
    status, code = 409, "IMPORT_STATE"


class ImportHasErrors(StockError):
    status, code = 409, "IMPORT_HAS_ERRORS"


# ---------------------------------------------------------------- file storage


def file_path(batch_id: int) -> Path:
    return UPLOAD_DIR / f"{int(batch_id)}.bin"


def save_file(batch_id: int, data: bytes) -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_path(batch_id).write_bytes(data)


def load_file(batch_id: int) -> bytes:
    p = file_path(batch_id)
    if not p.exists():
        raise NotFound("ไม่พบไฟล์ที่อัปโหลด (อาจถูกลบแล้ว) กรุณาอัปโหลดใหม่")
    return p.read_bytes()


def remove_file(batch_id: int) -> None:
    try:
        file_path(batch_id).unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- value parsing


def norm_header(s: Any) -> str:
    return re.sub(r"[\s_.\-]+", "", str(s).lower())


def suggest_mapping(headers: list[str]) -> dict[str, str]:
    by_norm = {norm_header(h): h for h in headers}
    out = {}
    for target, names in SYNONYMS.items():
        for n in names:
            if norm_header(n) in by_norm:
                out[target] = by_norm[norm_header(n)]
                break
    return out


def parse_date(v: Any, order: str = "DMY") -> date | None:
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if 20000 <= v <= 80000:  # Excel serial number
            return date(1899, 12, 30) + timedelta(days=int(v))
        return None
    s = str(v).strip()
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?", s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})(?:[ T].*)?", s)
        if not m:
            return None
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if y < 100:
            y += 2000
        if a > 12:
            d, mo = a, b
        elif b > 12:
            mo, d = a, b
        else:
            d, mo = (a, b) if order == "DMY" else (b, a)
    if y > 2400:  # Buddhist era
        y -= 543
    if not 1990 <= y <= 2100:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_qty(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        if isinstance(v, (int, float)):
            d = Decimal(str(v))
        else:
            s = str(v).strip().replace(",", "").replace(" ", "")
            neg = s.startswith("(") and s.endswith(")")
            d = Decimal(s.strip("()"))
            d = -d if neg else d
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def text_of(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


# ---------------------------------------------------------------- options


def _unit_code(v: Any) -> str:
    u = (str(v) if v else "PC").strip().upper()
    return UNIT_ALIASES.get(u, u)


def normalize_options(o: dict | None, batch_doc_type: str) -> dict:
    o = dict(o or {})
    match_by = o.get("match_by") or "PRODUCT_CODE"
    if match_by not in ("PRODUCT_CODE", "BARCODE", "BOTH"):
        raise BadParams("match_by ไม่ถูกต้อง", field="match_by")
    default_date = o.get("default_date")
    if default_date:
        if parse_date(default_date) is None:
            raise BadParams("วันที่เริ่มต้นไม่ถูกต้อง", field="default_date")
    rules = []
    for r in o.get("doc_type_rules") or []:
        prefix = str(r.get("prefix", "")).strip()
        dt = r.get("doc_type")
        if not prefix:
            continue
        if dt not in engine.DOC_TYPES or dt == "ADJUSTMENT":       # adjustments are entered by hand only
            raise BadParams("ประเภทเอกสารในกฎไม่ถูกต้อง", field="doc_type_rules")
        rules.append({"prefix": prefix, "doc_type": dt})
    wmap = {}
    for k, v in (o.get("warehouse_map") or {}).items():
        if v in (None, ""):
            continue
        try:
            wmap[str(k)] = int(v)
        except (TypeError, ValueError):
            raise BadParams("warehouse_map ไม่ถูกต้อง", field="warehouse_map")
    return {
        "match_by": match_by,
        "default_unit": _unit_code(o.get("default_unit")),
        "default_doc_no": (str(o.get("default_doc_no")).strip() or None) if o.get("default_doc_no") else None,
        "default_date": default_date or None,
        "doc_type_rules": rules,
        "negative_as_return": bool(o.get("negative_as_return", batch_doc_type == "SALE")),
        "warehouse_by": "salesman" if o.get("warehouse_by") == "salesman" else None,
        "warehouse_map": wmap,
        "create_missing_customers": bool(o.get("create_missing_customers", True)),
        "duplicate_lines": "KEEP" if o.get("duplicate_lines") == "KEEP" else "ERROR",
        "date_order": "MDY" if o.get("date_order") == "MDY" else "DMY",
    }


def check_mapping(mapping: dict, headers: list[str], opt: dict) -> dict:
    m = {k: v for k, v in (mapping or {}).items() if k in TARGETS and v}
    for target, col in m.items():
        if col not in headers:
            raise BadParams(f"ไม่พบคอลัมน์ '{col}' ในไฟล์", field=target, code_hint="MAPPING_COLUMN_MISSING", column=col)
    if "qty" not in m:
        raise BadParams("ต้องจับคู่คอลัมน์ 'จำนวน'", field="qty", code_hint="MAPPING_REQUIRED")
    need = {"PRODUCT_CODE": ["product_code"], "BARCODE": ["barcode"], "BOTH": ["product_code", "barcode"]}[opt["match_by"]]
    if not any(n in m for n in need):
        raise BadParams("ต้องจับคู่คอลัมน์รหัสสินค้า/บาร์โค้ด", field=need[0], code_hint="MAPPING_REQUIRED")
    if "doc_no" not in m and not opt["default_doc_no"]:
        raise BadParams("ต้องจับคู่คอลัมน์เลขที่เอกสาร หรือกำหนดเลขที่เอกสารเริ่มต้น", field="doc_no", code_hint="MAPPING_REQUIRED")
    return m


# ---------------------------------------------------------------- preview helpers


def preview_payload(grid: list[list], header_row: int, rows: int = 20, cols: int = 30) -> dict:
    return {
        "n_rows": len(grid),
        "header_row": header_row,
        "rows": [[(None if c is None else c) for c in r[:cols]] for r in grid[:rows]],
        "headers": readers.make_headers(grid[header_row - 1]) if 0 < header_row <= len(grid) else [],
    }


def suggest_template(conn, headers: list[str], doc_type: str) -> int | None:
    hs = set(headers)
    best, best_n = None, 0
    for t in conn.execute("SELECT template_id FROM import_templates WHERE doc_type=%s", (doc_type,)).fetchall():
        cols = [r["source_column"] for r in conn.execute(
            "SELECT source_column FROM import_mappings WHERE template_id=%s", (t["template_id"],)).fetchall()]
        if cols and set(cols) <= hs and len(cols) > best_n:
            best, best_n = t["template_id"], len(cols)
    return best


# ---------------------------------------------------------------- validation core


def _bad(r: dict, code: str, detail: str | None = None) -> None:
    if r["status"] == "ERROR":
        return  # keep the first problem found
    r["status"], r["error_code"], r["error_detail"] = "ERROR", code, detail


def _group_key(r: dict):
    return (r["warehouse_id"], r["doc_type"], r["doc_no"])


def validate_rows(conn, batch: dict, raw_rows: list[dict], mapping: dict, opt: dict,
                  can_transact: Callable[[int], bool]) -> tuple[list[dict], dict]:
    cur = conn.cursor()
    default_wh = batch["warehouse_id"]

    # ---- lookup tables -------------------------------------------------
    cur.execute("SELECT warehouse_id, warehouse_type, is_active FROM warehouses")
    whs = {w["warehouse_id"]: w for w in cur.fetchall()}
    cur.execute("SELECT unit_id, unit_code FROM units")
    units = {u["unit_code"]: u["unit_id"] for u in cur.fetchall()}

    refs_code, refs_bar = set(), set()
    for rr in raw_rows:
        for field, bucket in (("product_code", refs_code), ("barcode", refs_bar)):
            if field in mapping:
                t = text_of(rr["raw"].get(mapping[field]))
                if t:
                    bucket.add(t)
    allrefs = list(refs_code | refs_bar)
    by_code, by_bar, unit_bar = {}, {}, {}
    if allrefs:
        cur.execute("""SELECT product_id, product_code, barcode, base_unit_id, is_active FROM products
                       WHERE product_code = ANY(%s) OR barcode = ANY(%s)
                          OR lower(product_code) = ANY(%s)""", (allrefs, allrefs, [x.lower() for x in allrefs]))
        for p in cur.fetchall():
            by_code[p["product_code"]] = p
            by_code.setdefault(p["product_code"].lower(), p)
            if p["barcode"]:
                by_bar[p["barcode"]] = p
        cur.execute("""SELECT c.barcode, c.unit_id, c.factor_to_base, p.product_id, p.product_code, p.barcode AS pbar,
                              p.base_unit_id, p.is_active
                       FROM product_unit_conversions c JOIN products p USING (product_id)
                       WHERE c.barcode = ANY(%s)""", (allrefs,))
        for c in cur.fetchall():
            unit_bar[c["barcode"]] = c
    pids = {p["product_id"] for p in by_code.values()} | {p["product_id"] for p in by_bar.values()} \
        | {c["product_id"] for c in unit_bar.values()}
    factors: dict[tuple[int, int], int] = {}
    if pids:
        cur.execute("SELECT product_id, unit_id, factor_to_base FROM product_unit_conversions WHERE product_id = ANY(%s)",
                    (list(pids),))
        factors = {(f["product_id"], f["unit_id"]): f["factor_to_base"] for f in cur.fetchall()}

    # ---- row level -----------------------------------------------------
    today = date.today()
    default_date = parse_date(opt["default_date"]) if opt["default_date"] else today
    rules = [(r["prefix"].lower(), r["doc_type"]) for r in opt["doc_type_rules"]]
    rows: list[dict] = []
    for rr in raw_rows:
        raw = rr["raw"]
        g = lambda f: raw.get(mapping[f]) if f in mapping else None  # noqa: E731
        r = {"row_no": rr["row_no"], "raw": raw, "status": "VALID", "error_code": None, "error_detail": None,
             "doc_no": None, "doc_date": None, "doc_type": None, "warehouse_id": None, "product_ref": None,
             "product_id": None, "unit_code": None, "unit_id": None, "qty": None, "base_qty": None,
             "customer_code": text_of(g("customer_code")), "customer_name": text_of(g("customer_name")),
             "salesman": text_of(g("salesman")), "factor": None}
        rows.append(r)

        doc_no = text_of(g("doc_no")) if "doc_no" in mapping else None
        doc_no = doc_no or opt["default_doc_no"]
        if not doc_no:
            _bad(r, "MISSING_DOC_NO")
        elif len(doc_no) > 50:
            _bad(r, "DOC_NO_TOO_LONG", doc_no[:30])
        r["doc_no"] = doc_no

        if "doc_date" in mapping:
            d = parse_date(g("doc_date"), opt["date_order"])
            if d is None:
                _bad(r, "BAD_DATE", text_of(g("doc_date")))
            r["doc_date"] = d
        else:
            r["doc_date"] = default_date

        qty = parse_qty(g("qty"))
        if qty is None or qty == 0:
            _bad(r, "BAD_QTY", text_of(g("qty")))
        r["qty"] = qty

        # document type for this row: prefix rule first (e.g. CN -> customer return), then sign of the quantity
        dt = batch["doc_type"]
        if dt == "SALE":
            rule_dt = next((t for p, t in rules if doc_no and doc_no.lower().startswith(p)), None)
            if rule_dt:
                dt = rule_dt
            elif qty is not None and qty < 0 and opt["negative_as_return"]:
                dt = "CUSTOMER_RETURN"
        if qty is not None and qty < 0 and not (batch["doc_type"] == "SALE" and dt == "CUSTOMER_RETURN"):
            _bad(r, "NEGATIVE_QTY", str(qty))
        r["doc_type"] = dt

        # warehouse
        wh = default_wh
        if opt["warehouse_by"] == "salesman" and r["salesman"] and r["salesman"] in opt["warehouse_map"]:
            wh = opt["warehouse_map"][r["salesman"]]
        w = whs.get(wh)
        if not w or not w["is_active"] or w["warehouse_type"] != "PHYSICAL":
            _bad(r, "WAREHOUSE_INVALID", str(wh))
        elif not can_transact(wh):
            _bad(r, "WAREHOUSE_NOT_ALLOWED", str(wh))
        r["warehouse_id"] = wh

        # product
        code_v, bar_v = text_of(g("product_code")), text_of(g("barcode"))
        r["product_ref"] = (code_v or bar_v or "")[:100] or None
        prod, bar_unit_id = None, None
        order = {"PRODUCT_CODE": ["code"], "BARCODE": ["bar"], "BOTH": ["code", "bar"]}[opt["match_by"]]
        for how in order:
            if how == "code" and code_v:
                prod = by_code.get(code_v) or by_code.get(code_v.lower())
            elif how == "bar" and bar_v:
                prod = by_bar.get(bar_v)
                if prod is None and bar_v in unit_bar:
                    c = unit_bar[bar_v]
                    prod = {"product_id": c["product_id"], "base_unit_id": c["base_unit_id"], "is_active": c["is_active"]}
                    bar_unit_id = c["unit_id"]
            if prod:
                break
        if not r["product_ref"]:
            _bad(r, "MISSING_PRODUCT")
        elif prod is None:
            _bad(r, "PRODUCT_NOT_FOUND", r["product_ref"])
        elif not prod["is_active"]:
            _bad(r, "PRODUCT_INACTIVE", r["product_ref"])
        else:
            r["product_id"] = prod["product_id"]

        # unit
        if r["product_id"]:
            raw_unit = text_of(g("unit_code")) if "unit_code" in mapping else None
            if raw_unit:
                code = UNIT_ALIASES.get(raw_unit.upper(), raw_unit.upper())
                uid = units.get(code)
                if uid is None:
                    _bad(r, "UNIT_NOT_FOUND", raw_unit)
            elif bar_unit_id:
                uid = bar_unit_id
                code = next((c for c, i in units.items() if i == uid), None)
            else:
                code = opt["default_unit"]
                uid = units.get(code)
                if uid is None:
                    _bad(r, "UNIT_NOT_FOUND", code)
            if uid is not None:
                r["unit_id"], r["unit_code"] = uid, code
                factor = 1 if uid == prod["base_unit_id"] else factors.get((r["product_id"], uid))
                if factor is None:
                    _bad(r, "UNIT_NOT_DEFINED", f"{r['product_ref']}/{code}")
                else:
                    r["factor"] = factor
                    if qty:
                        base = abs(qty) * factor
                        if base != base.to_integral_value():
                            _bad(r, "NOT_WHOLE_PC", str(base))
                        else:
                            r["base_qty"] = int(base)

        # customers only apply to sales-type documents
        if r["customer_code"] and len(r["customer_code"]) > 30:
            _bad(r, "BAD_CUSTOMER_CODE", r["customer_code"][:30])

    # ---- duplicate lines inside the file --------------------------------
    if opt["duplicate_lines"] == "ERROR":
        seen = set()
        for r in rows:
            if r["status"] != "VALID":
                continue
            k = (*_group_key(r), r["product_id"])
            if k in seen:
                _bad(r, "DUP_IN_FILE", f"{r['doc_no']} / {r['product_ref']}")
            seen.add(k)

    # ---- one document must have one type/date/warehouse ----------------
    by_doc_no = defaultdict(list)
    for r in rows:
        if r["status"] == "VALID":
            by_doc_no[(r["warehouse_id"], r["doc_no"])].append(r)
    for grp in by_doc_no.values():
        if len({r["doc_type"] for r in grp}) > 1:
            for r in grp:
                _bad(r, "DOC_INCONSISTENT", "mixed document types")
        elif len({r["doc_date"] for r in grp}) > 1:
            for r in grp:
                _bad(r, "DOC_INCONSISTENT", "different dates")

    # ---- documents that already exist -> DUPLICATE (skipped, not an error) --
    key_invalid = {"MISSING_DOC_NO", "DOC_NO_TOO_LONG", "WAREHOUSE_INVALID", "WAREHOUSE_NOT_ALLOWED"}
    keyed = [r for r in rows if r["doc_no"] and r["doc_type"] and r["warehouse_id"]
             and not (r["status"] == "ERROR" and r["error_code"] in key_invalid)]
    keys = sorted({_group_key(r) for r in keyed}, key=lambda k: (k[0], k[1], k[2]))
    existing = set()
    if keys:
        cur.execute("""SELECT warehouse_id, doc_type, doc_no FROM stock_documents
                       WHERE (warehouse_id, doc_type, doc_no) IN
                             (SELECT * FROM unnest(%s::int[], %s::text[], %s::text[]))""",
                    ([k[0] for k in keys], [k[1] for k in keys], [k[2] for k in keys]))
        existing = {(e["warehouse_id"], e["doc_type"], e["doc_no"]) for e in cur.fetchall()}
    for r in keyed:
        if _group_key(r) in existing:
            r["status"], r["error_code"], r["error_detail"] = "DUPLICATE", "DUP_IN_DB", r["doc_no"]

    # ---- documents with any bad row are blocked as a whole ------------------
    # grouped by (warehouse, doc_no) only: a row whose quantity could not be read has no reliable document
    # type, but it still belongs to its document and must stop the other rows from being posted without it.
    by_no: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r["doc_no"] and r["warehouse_id"]:
            by_no[(r["warehouse_id"], r["doc_no"])].append(r)
    for grp in by_no.values():
        if any(r["status"] == "ERROR" for r in grp):
            for r in grp:
                if r["status"] == "VALID":
                    r["status"], r["error_code"] = "BLOCKED", "DOC_HAS_ERRORS"
    docs: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r["doc_no"] and r["doc_type"] and r["warehouse_id"]:
            docs[_group_key(r)].append(r)

    # ---- stock simulation, oldest document first ---------------------------
    ready = {k: g for k, g in docs.items() if all(r["status"] == "VALID" for r in g)}
    shortages: list[dict] = []
    if ready:
        pairs = {(k[0], r["product_id"]) for k, g in ready.items() for r in g}
        cur.execute("""SELECT warehouse_id, product_id, qty_on_hand FROM stock_balances
                       WHERE (warehouse_id, product_id) IN (SELECT * FROM unnest(%s::int[], %s::bigint[]))""",
                    ([p[0] for p in pairs], [p[1] for p in pairs]))
        sim = {(b["warehouse_id"], b["product_id"]): b["qty_on_hand"] for b in cur.fetchall()}
        order_keys = sorted(ready, key=lambda k: (ready[k][0]["doc_date"], k[2], k[0], k[1]))
        for k in order_keys:
            grp = ready[k]
            sign = 1 if k[1] in INCREASE else -1
            delta: dict[int, int] = defaultdict(int)
            for r in grp:
                delta[r["product_id"]] += sign * r["base_qty"]
            short = {pid: (sim.get((k[0], pid), 0), -d) for pid, d in delta.items()
                     if d < 0 and sim.get((k[0], pid), 0) + d < 0}
            if short:
                for r in grp:
                    if r["product_id"] in short:
                        have, need = short[r["product_id"]]
                        r["status"], r["error_code"], r["error_detail"] = "ERROR", "INSUFFICIENT_STOCK", f"available {have}, required {need}"
                        shortages.append({"doc_no": r["doc_no"], "warehouse_id": k[0], "product_ref": r["product_ref"],
                                          "available": have, "required": need})
                    else:
                        r["status"], r["error_code"] = "BLOCKED", "DOC_HAS_ERRORS"
            else:
                for pid, d in delta.items():
                    sim[(k[0], pid)] = sim.get((k[0], pid), 0) + d

    # ---- summary ----------------------------------------------------------
    counts = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    doc_status = defaultdict(int)
    qty_in = qty_out = 0
    for k, grp in docs.items():
        sts = {r["status"] for r in grp}
        if sts == {"VALID"}:
            doc_status["ready"] += 1
            tot = sum(r["base_qty"] for r in grp)
            if k[1] in INCREASE:
                qty_in += tot
            else:
                qty_out += tot
        elif sts == {"DUPLICATE"}:
            doc_status["duplicate"] += 1
        else:
            doc_status["blocked"] += 1
    by_type = defaultdict(int)
    for k, grp in docs.items():
        if all(r["status"] == "VALID" for r in grp):
            by_type[k[1]] += 1
    new_customers = len({r["customer_code"] for r in rows if r["status"] == "VALID" and r["customer_code"]
                         and r["doc_type"] in ("SALE", "CUSTOMER_RETURN")})
    if new_customers:
        cur.execute("SELECT customer_code FROM customers WHERE customer_code = ANY(%s)",
                    ([r["customer_code"] for r in rows if r["customer_code"]],))
        have = {c["customer_code"] for c in cur.fetchall()}
        new_customers = len({r["customer_code"] for r in rows if r["status"] == "VALID" and r["customer_code"]
                             and r["doc_type"] in ("SALE", "CUSTOMER_RETURN")} - have)
    salesmen = defaultdict(int)
    for r in rows:
        if r["salesman"]:
            salesmen[r["salesman"]] += 1
    err_codes = defaultdict(int)
    for r in rows:
        if r["status"] == "ERROR":
            err_codes[r["error_code"]] += 1
    summary = {
        "rows": {"total": len(rows), "valid": counts["VALID"], "error": counts["ERROR"],
                 "blocked": counts["BLOCKED"], "duplicate": counts["DUPLICATE"]},
        "docs": {"ready": doc_status["ready"], "blocked": doc_status["blocked"], "duplicate": doc_status["duplicate"],
                 "by_type": dict(by_type)},
        "qty_in": qty_in, "qty_out": qty_out,
        "error_codes": dict(err_codes),
        "shortages": shortages[:50], "shortage_count": len(shortages),
        "new_customers": new_customers,
        "salesmen": dict(sorted(salesmen.items())),
    }
    return rows, summary


# ---------------------------------------------------------------- process (parse + validate + persist)

_INSERT_ROW = """INSERT INTO import_rows
  (batch_id,row_no,raw_data,doc_no,doc_date,doc_type,warehouse_id,product_ref,product_id,unit_code,unit_id,qty,base_qty,
   customer_code,customer_name,salesman,status,error_code,error_message,row_hash)
  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""


def _load_batch(conn, batch_id: int, lock: bool = False) -> dict:
    b = conn.execute(f"SELECT * FROM import_batches WHERE batch_id=%s{' FOR UPDATE' if lock else ''}",
                     (batch_id,)).fetchone()
    if not b:
        raise NotFound("ไม่พบรายการนำเข้า")
    return b


def process_batch(conn, batch_id: int, params: dict, user_id: int, can_transact: Callable[[int], bool]) -> dict:
    data = None
    with conn.transaction():
        b = _load_batch(conn, batch_id, lock=True)
        if b["status"] not in ("UPLOADED", "VALIDATED"):
            raise ImportStateError(f"รายการนำเข้านี้อยู่ในสถานะ {b['status']} แก้ไขไม่ได้", status=b["status"])
        data = load_file(batch_id)
        kind = readers.sniff_kind(data, b["file_name"])
        grid, sheet = readers.read_grid(data, kind, params.get("sheet"))
        header_row = int(params.get("header_row") or 1)
        if not 1 <= header_row <= max(1, len(grid)):
            raise BadParams("แถวหัวตารางไม่ถูกต้อง", field="header_row")
        headers = readers.make_headers(grid[header_row - 1]) if grid else []
        opt = normalize_options(params.get("options"), b["doc_type"])
        mapping = check_mapping(params.get("mapping"), headers, opt)

        raw_rows = []
        for gi in range(header_row, len(grid)):
            row = grid[gi]
            if all(c is None for c in row):
                continue
            row = list(row) + [None] * (len(headers) - len(row))
            raw_rows.append({"row_no": gi + 1, "raw": {h: row[i] for i, h in enumerate(headers)}})
        if not raw_rows:
            raise BadParams("ไม่พบข้อมูลใต้แถวหัวตาราง", field="header_row", code_hint="NO_DATA_ROWS")

        rows, summary = validate_rows(conn, b, raw_rows, mapping, opt, can_transact)

        conn.execute("DELETE FROM import_rows WHERE batch_id=%s", (batch_id,))
        with conn.cursor() as cur:
            cur.executemany(_INSERT_ROW, [(
                batch_id, r["row_no"], Jsonb(r["raw"]), r["doc_no"], r["doc_date"], r["doc_type"], r["warehouse_id"],
                r["product_ref"], r["product_id"], r["unit_code"], r["unit_id"], r["qty"], r["base_qty"],
                r["customer_code"], r["customer_name"], r["salesman"], r["status"], r["error_code"], r["error_detail"],
                hashlib.sha256(f"{r['doc_no']}|{r['product_ref']}|{r['qty']}|{r['doc_date']}".encode()).hexdigest(),
            ) for r in rows])

        prev = conn.execute(
            """SELECT batch_id FROM import_batches WHERE file_hash=%s AND status='CONFIRMED' AND batch_id<>%s
               ORDER BY batch_id DESC LIMIT 1""", (b["file_hash"], batch_id)).fetchone()
        summary["file_already_imported"] = prev["batch_id"] if prev else None
        s = summary["rows"]
        options_store = {"mapping": mapping, "options": opt, "headers": headers, "file_kind": kind}
        conn.execute(
            """UPDATE import_batches SET status='VALIDATED', sheet_name=%s, header_row=%s, total_rows=%s, valid_rows=%s,
                      error_rows=%s, duplicate_rows=%s, options=%s, summary=%s, template_id=%s WHERE batch_id=%s""",
            (sheet, header_row, s["total"], s["valid"], s["error"] + s["blocked"], s["duplicate"],
             Jsonb(options_store), Jsonb(summary), params.get("template_id"), batch_id))
        engine.audit(conn.cursor(), user_id, "IMPORT_VALIDATE", "import_batch", batch_id, b["warehouse_id"],
                     new={"rows": s, "docs": summary["docs"]})
    return summary


# ---------------------------------------------------------------- confirm


def confirm_batch(conn, batch_id: int, user_id: int, skip_errors: bool,
                  can_post: Callable[[str, int], bool]) -> dict:
    with conn.transaction():
        b = _load_batch(conn, batch_id, lock=True)
        if b["status"] != "VALIDATED":
            raise ImportStateError(f"รายการนำเข้านี้อยู่ในสถานะ {b['status']} ยืนยันไม่ได้", status=b["status"])
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) AS n FROM import_rows WHERE batch_id=%s GROUP BY status", (batch_id,)).fetchall()}
        bad = counts.get("ERROR", 0) + counts.get("BLOCKED", 0)
        if bad and not skip_errors:
            raise ImportHasErrors("ยังมีแถวที่ผิดพลาด กรุณาแก้ไขหรือเลือกข้ามเอกสารที่ผิดพลาด",
                                  error_rows=bad)
        rows = conn.execute("SELECT * FROM import_rows WHERE batch_id=%s AND status='VALID' ORDER BY row_no",
                            (batch_id,)).fetchall()
        if not rows:
            raise ImportHasErrors("ไม่มีเอกสารที่พร้อมนำเข้า", error_rows=bad, code_hint="NOTHING_TO_IMPORT")

        docs: dict[tuple, list[dict]] = defaultdict(list)
        for r in rows:
            docs[(r["warehouse_id"], r["doc_type"], r["doc_no"])].append(r)
        for (wh, dt, _no) in docs:
            if not can_post(dt, wh):
                raise StockError("คุณไม่มีสิทธิ์นำเข้าเอกสารประเภทนี้ในคลังที่เลือก", wh=wh, doc_type=dt)

        opt = (b["options"] or {}).get("options", {})
        cust_ids: dict[str, int] = {}
        codes = sorted({r["customer_code"] for r in rows if r["customer_code"] and r["doc_type"] in ("SALE", "CUSTOMER_RETURN")})
        created_customers = 0
        if codes:
            have = {c["customer_code"]: c["customer_id"] for c in conn.execute(
                "SELECT customer_code, customer_id FROM customers WHERE customer_code = ANY(%s)", (codes,)).fetchall()}
            if opt.get("create_missing_customers", True):
                names = {}
                for r in rows:
                    if r["customer_code"] and r["customer_code"] not in names:
                        names[r["customer_code"]] = r["customer_name"] or r["customer_code"]
                for code in codes:
                    if code not in have:
                        c = conn.execute(
                            """INSERT INTO customers (customer_code, name) VALUES (%s,%s)
                               ON CONFLICT (customer_code) DO UPDATE SET customer_code = EXCLUDED.customer_code
                               RETURNING customer_id""", (code, names[code][:200])).fetchone()
                        have[code] = c["customer_id"]
                        created_customers += 1
            cust_ids = have

        order = sorted(docs, key=lambda k: (docs[k][0]["doc_date"], k[2], k[0], k[1]))
        posted = 0
        for k in order:
            grp = docs[k]
            first = grp[0]
            did = engine.create_document(conn, {
                "doc_type": k[1], "doc_no": k[2], "doc_date": first["doc_date"], "warehouse_id": k[0],
                "customer_id": cust_ids.get(first["customer_code"]) if k[1] in ("SALE", "CUSTOMER_RETURN") else None,
                "source": "IMPORT", "import_batch_id": batch_id,
                "remark": f"Import batch #{batch_id}",
                "items": [{"product_id": r["product_id"], "unit_code": r["unit_code"], "qty": abs(r["qty"])} for r in grp],
            }, user_id)
            engine.post_document(conn, did, user_id)
            conn.execute("UPDATE import_rows SET status='POSTED', document_id=%s WHERE batch_id=%s AND row_id = ANY(%s)",
                         (did, batch_id, [r["row_id"] for r in grp]))
            posted += 1

        result = {"documents_posted": posted, "rows_posted": len(rows), "rows_skipped": bad + counts.get("DUPLICATE", 0),
                  "customers_created": created_customers}
        conn.execute("""UPDATE import_batches SET status='CONFIRMED', confirmed_by=%s, confirmed_at=now(),
                               summary = summary || %s::jsonb WHERE batch_id=%s""",
                     (user_id, Jsonb({"result": result}), batch_id))
        engine.audit(conn.cursor(), user_id, "IMPORT_CONFIRM", "import_batch", batch_id, b["warehouse_id"], new=result)
    return result


def cancel_batch(conn, batch_id: int, user_id: int) -> None:
    with conn.transaction():
        b = _load_batch(conn, batch_id, lock=True)
        if b["status"] not in ("UPLOADED", "VALIDATED"):
            raise ImportStateError(f"รายการนำเข้านี้อยู่ในสถานะ {b['status']} ยกเลิกไม่ได้", status=b["status"])
        conn.execute("UPDATE import_batches SET status='CANCELLED' WHERE batch_id=%s", (batch_id,))
        engine.audit(conn.cursor(), user_id, "IMPORT_CANCEL", "import_batch", batch_id, b["warehouse_id"])
    remove_file(batch_id)


# ---------------------------------------------------------------- export of problem rows


def export_errors_xlsx(conn, batch_id: int, include_duplicates: bool = False) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    b = _load_batch(conn, batch_id)
    headers = (b["options"] or {}).get("headers") or []
    sts = ["ERROR", "BLOCKED"] + (["DUPLICATE"] if include_duplicates else [])
    rows = conn.execute(
        """SELECT row_no, status, error_code, error_message, raw_data FROM import_rows
           WHERE batch_id=%s AND status = ANY(%s) ORDER BY row_no""", (batch_id, sts)).fetchall()
    wb = Workbook()
    ws = wb.active
    ws.title = "Errors"
    head = ["ROW_NO", "STATUS", "ERROR_CODE", "ERROR_DETAIL"] + headers
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="FDE9E7")
    for r in rows:
        raw = r["raw_data"] or {}

        def cell(v):
            return v if isinstance(v, (int, float)) or v is None else str(v)
        ws.append([r["row_no"], r["status"], r["error_code"], r["error_message"]] + [cell(raw.get(h)) for h in headers])
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- templates


def save_template(conn, user_id: int, body: dict) -> int:
    name = (body.get("name") or "").strip()
    if not name:
        raise BadParams("ต้องระบุชื่อรูปแบบ", field="name")
    dt = body.get("doc_type")
    if dt not in IMPORT_DOC_TYPES:
        raise BadParams("ประเภทเอกสารไม่ถูกต้อง", field="doc_type")
    opt = normalize_options(body.get("settings"), dt)
    mapping = {k: v for k, v in (body.get("mapping") or {}).items() if k in TARGETS and v}
    with conn.transaction():
        row = conn.execute(
            """INSERT INTO import_templates (name, doc_type, sheet_name, header_row, match_by, settings, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (name) DO UPDATE SET doc_type=EXCLUDED.doc_type, sheet_name=EXCLUDED.sheet_name,
                  header_row=EXCLUDED.header_row, match_by=EXCLUDED.match_by, settings=EXCLUDED.settings
               RETURNING template_id""",
            (name[:150], dt, body.get("sheet_name"), int(body.get("header_row") or 1), opt["match_by"],
             Jsonb(opt), user_id)).fetchone()
        tid = row["template_id"]
        conn.execute("DELETE FROM import_mappings WHERE template_id=%s", (tid,))
        for target, col in mapping.items():
            conn.execute("INSERT INTO import_mappings (template_id, target_field, source_column) VALUES (%s,%s,%s)",
                         (tid, target, str(col)[:150]))
        engine.audit(conn.cursor(), user_id, "SAVE_IMPORT_TEMPLATE", "import_template", tid, new={"name": name})
    return tid


def list_templates(conn) -> list[dict]:
    ts = conn.execute("SELECT * FROM import_templates ORDER BY name").fetchall()
    maps = defaultdict(dict)
    for m in conn.execute("SELECT template_id, target_field, source_column FROM import_mappings").fetchall():
        maps[m["template_id"]][m["target_field"]] = m["source_column"]
    return [{"template_id": t["template_id"], "name": t["name"], "doc_type": t["doc_type"],
             "sheet_name": t["sheet_name"], "header_row": t["header_row"], "match_by": t["match_by"],
             "settings": t["settings"], "mapping": maps.get(t["template_id"], {})} for t in ts]

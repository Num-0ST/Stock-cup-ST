"""Read a photo of a sales bill (printed or handwritten) with Claude vision and turn it into a draft document.

Nothing is posted here: the result is a *draft* the user reviews and corrects on screen before saving.
Flow: image -> Claude (forced tool call = structured JSON) -> match products / units / customer / warehouse
      -> draft + warnings. The image and the raw AI result are kept in bill_scans for audit.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from .engine import StockError

API_URL = os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/tmp/wms_uploads"))


class AINotConfigured(StockError):
    status, code = 503, "AI_NOT_CONFIGURED"


class AIFailed(StockError):
    status, code = 502, "AI_FAILED"


class BadImage(StockError):
    status, code = 422, "BAD_IMAGE"


def api_key() -> str | None:
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None


def model() -> str:
    return (os.environ.get("BILL_AI_MODEL") or "").strip() or DEFAULT_MODEL


def sniff_image(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    raise BadImage("รองรับเฉพาะรูป JPG, PNG, WEBP หรือ GIF", code_hint="UNSUPPORTED_IMAGE")


def save_image(data: bytes, mime: str) -> tuple[str, str]:
    sha = hashlib.sha256(data).hexdigest()
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}[mime]
    d = UPLOAD_DIR / "bills" / date.today().strftime("%Y%m")
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sha}.{ext}"
    if not path.exists():
        path.write_bytes(data)
    return str(path), sha


# ---------------------------------------------------------------- AI call

BILL_TOOL = {
    "name": "record_bill",
    "description": "Record the contents of one sales bill / invoice / delivery note exactly as written on the image.",
    "input_schema": {
        "type": "object",
        "properties": {
            "doc_no": {"type": ["string", "null"], "description": "Bill / invoice number, e.g. HS680012, IV-0045, CN0003"},
            "doc_date": {"type": ["string", "null"],
                         "description": "Bill date as YYYY-MM-DD in the Gregorian calendar (convert Buddhist-era years: 2568 -> 2025)"},
            "doc_kind": {"type": "string", "enum": ["SALE", "CUSTOMER_RETURN", "RECEIVING", "UNKNOWN"],
                         "description": "SALE for sales bills/invoices, CUSTOMER_RETURN for credit notes / returns"},
            "customer_code": {"type": ["string", "null"]},
            "customer_name": {"type": ["string", "null"]},
            "salesman": {"type": ["string", "null"], "description": "Salesman / van / route code if shown"},
            "is_handwritten": {"type": "boolean"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product_code": {"type": ["string", "null"], "description": "Item code / SKU as printed"},
                        "barcode": {"type": ["string", "null"]},
                        "description": {"type": ["string", "null"], "description": "Item name as written"},
                        "qty": {"type": ["number", "null"]},
                        "unit": {"type": ["string", "null"], "description": "Unit exactly as written (ลัง, แพ็ค, ชิ้น, CT, PK, PC ...)"},
                        "unit_price": {"type": ["number", "null"]},
                        "amount": {"type": ["number", "null"]},
                        "deposit_qty": {"type": ["number", "null"],
                                        "description": "Quantity of this line the customer left with the seller (marked ฝาก / ฝากไว้ / ค้างส่ง), same unit as qty. null if not marked"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["qty", "confidence"],
                },
            },
            "total_amount": {"type": ["number", "null"]},
            "notes": {"type": ["string", "null"], "description": "Anything unclear, crossed out or hard to read"},
        },
        "required": ["items", "doc_kind", "is_handwritten"],
    },
}

PROMPT = """You are reading a photo of a sales document from a distributor in Thailand / Laos.
The bill may be printed or handwritten, in Thai, Lao or English. Read it carefully and call record_bill once.
Rules:
- Copy codes exactly (keep letters, digits and dashes). Do not invent values: use null when unreadable.
- One entry per product line. Skip subtotal, discount, VAT and total lines.
- qty is the number of units on that line, as a number. Keep the unit as written.
- If a line says some goods are left with the seller (ฝาก, ฝากไว้, ค้างส่ง, ສົ່ງພາຍຫຼັງ), put that quantity in deposit_qty.
- Set confidence to low for any line you are unsure about (smudged, crossed out, ambiguous digits).
- Dates: return YYYY-MM-DD in the Gregorian calendar. Thai/Lao Buddhist years minus 543. Day comes before month."""


def call_ai(data: bytes, mime: str) -> dict:
    key = api_key()
    if not key:
        raise AINotConfigured("ยังไม่ได้ตั้งค่า ANTHROPIC_API_KEY ในไฟล์ .env จึงยังอ่านบิลจากรูปไม่ได้")
    body = {
        "model": model(),
        "max_tokens": 4096,
        "tools": [BILL_TOOL],
        "tool_choice": {"type": "tool", "name": "record_bill"},
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}},
            {"type": "text", "text": PROMPT},
        ]}],
    }
    try:
        r = httpx.post(API_URL, json=body, timeout=120, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    except httpx.HTTPError as e:
        raise AIFailed(f"เชื่อมต่อบริการ AI ไม่ได้: {type(e).__name__}", code_hint="AI_UNREACHABLE")
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = r.text[:200]
        hint = {401: "AI_BAD_KEY", 403: "AI_BAD_KEY", 429: "AI_RATE_LIMIT", 529: "AI_OVERLOADED"}.get(r.status_code, "AI_ERROR")
        raise AIFailed(f"บริการ AI ตอบกลับผิดพลาด ({r.status_code}) {msg}".strip(), code_hint=hint)
    for block in r.json().get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "record_bill":
            return block.get("input") or {}
    raise AIFailed("AI ไม่ได้ส่งข้อมูลบิลกลับมา", code_hint="AI_NO_RESULT")


# ---------------------------------------------------------------- matching

UNIT_WORDS = {
    "CT": ["ct", "ctn", "carton", "case", "ลัง", "หีบ", "กล่องใหญ่", "ແກັດ", "ລັງ", "ເປົາ"],
    "PK": ["pk", "pack", "pac", "แพ็ค", "แพ็ก", "แพค", "แพก", "ห่อ", "มัด", "ແພັກ", "ຫໍ່"],
    "PC": ["pc", "pcs", "piece", "ea", "ชิ้น", "อัน", "ขวด", "ซอง", "กระป๋อง", "ถุง", "ใบ", "ຊິ້ນ", "ອັນ", "ຕຸກ", "ກະປ໋ອງ"],
}
DOC_PREFIX_RULES = [("cn", "CUSTOMER_RETURN"), ("hsh", "SALE"), ("hs", "SALE"), ("iv", "SALE")]


def unit_code_of(text: str | None) -> str | None:
    if not text:
        return None
    t = re.sub(r"[\s.]", "", str(text)).lower()
    for code, words in UNIT_WORDS.items():
        if t in words or t == code.lower():
            return code
    return None


def _num(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v).replace(",", ""))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _parse_date(s) -> date | None:
    """ISO from the AI; also accepts D/M/Y as printed on Thai/Lao bills (Buddhist or Gregorian, 2- or 4-digit year)."""
    if not s:
        return None
    txt = str(s).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2}).*", txt) or None
    try:
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})", txt)
            if not m:
                return None
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:                       # 68 -> 2568 (BE) ; 25 -> 2025
                y = 2500 + y if y >= 40 else 2000 + y
        if y > 2400:                          # Buddhist era
            y -= 543
        return date(y, mo, d)
    except ValueError:
        return None


def build_draft(conn, ai: dict, can_use_warehouse) -> dict:
    warnings: list[dict] = []
    cur = conn.cursor()

    # document type / number / date
    doc_no = (ai.get("doc_no") or "").strip()[:50] or None
    doc_type = ai.get("doc_kind") if ai.get("doc_kind") in ("SALE", "CUSTOMER_RETURN", "RECEIVING") else "SALE"
    if doc_no:
        rule = next((t for p, t in DOC_PREFIX_RULES if doc_no.lower().startswith(p)), None)
        if rule:
            doc_type = rule
    else:
        warnings.append({"code": "NO_DOC_NO"})
    doc_date = _parse_date(ai.get("doc_date"))
    if not doc_date:
        warnings.append({"code": "NO_DATE"})
        doc_date = date.today()
    elif doc_date > date.today():
        warnings.append({"code": "FUTURE_DATE", "date": doc_date.isoformat()})

    # warehouse: salesman code that equals a warehouse code, else the salesman map saved in import templates
    salesman = (ai.get("salesman") or "").strip()
    warehouse_id = None
    if salesman:
        cur.execute("SELECT warehouse_id FROM warehouses WHERE upper(warehouse_code)=upper(%s) AND is_active "
                    "AND warehouse_type='PHYSICAL'", (salesman,))
        row = cur.fetchone()
        if row:
            warehouse_id = row["warehouse_id"]
        else:
            cur.execute("SELECT settings FROM import_templates ORDER BY template_id DESC")
            for t in cur.fetchall():
                wid = ((t["settings"] or {}).get("warehouse_map") or {}).get(salesman)
                if wid:
                    warehouse_id = int(wid)
                    break
    if warehouse_id is not None and not can_use_warehouse(warehouse_id):
        warehouse_id = None

    # customer
    customer, suggestion = None, None
    code, name = (ai.get("customer_code") or "").strip(), (ai.get("customer_name") or "").strip()
    if code:
        cur.execute("SELECT customer_id, customer_code, name FROM customers WHERE upper(customer_code)=upper(%s)", (code,))
        customer = cur.fetchone()
    if not customer and name:
        cur.execute("SELECT customer_id, customer_code, name FROM customers WHERE lower(name)=lower(%s) LIMIT 1", (name,))
        customer = cur.fetchone()
    if not customer and (code or name):
        suggestion = {"code": (code or "")[:30] or None, "name": name or code}
        warnings.append({"code": "CUSTOMER_NOT_FOUND", "name": f"{code} {name}".strip()})

    # products
    cur.execute("SELECT product_id, product_code, name, barcode, base_unit_id FROM products WHERE is_active")
    products = cur.fetchall()
    by_code = {p["product_code"].upper(): p for p in products}
    by_barcode = {p["barcode"]: p for p in products if p["barcode"]}
    cur.execute("""SELECT pc.barcode, pc.product_id, u.unit_code FROM product_unit_conversions pc JOIN units u USING (unit_id)
                   WHERE pc.barcode IS NOT NULL""")
    unit_barcodes = {r["barcode"]: r for r in cur.fetchall()}
    cur.execute("""SELECT pc.product_id, u.unit_code FROM product_unit_conversions pc JOIN units u USING (unit_id)""")
    conv: dict[int, set] = {}
    for r in cur.fetchall():
        conv.setdefault(r["product_id"], set()).add(r["unit_code"])
    names = [(_norm(p["name"]), p) for p in products]

    items = []
    for n, ln in enumerate(ai.get("items") or [], start=1):
        w: list[str] = []
        qty = _num(ln.get("qty"))
        if qty is None or qty <= 0:
            w.append({"code": "NO_QTY"})
        dep = _num(ln.get("deposit_qty"))
        code_txt = (ln.get("product_code") or "").strip()
        bc_txt = (ln.get("barcode") or "").strip()
        desc = (ln.get("description") or "").strip()
        p, match, unit = None, None, unit_code_of(ln.get("unit"))
        for key in (code_txt, bc_txt):
            if not key:
                continue
            if key.upper() in by_code:
                p, match = by_code[key.upper()], "code"
            elif key in by_barcode:
                p, match = by_barcode[key], "barcode"
            elif key in unit_barcodes:
                ub = unit_barcodes[key]
                p, match = next(x for x in products if x["product_id"] == ub["product_id"]), "barcode"
                unit = unit or ub["unit_code"]
            if p:
                break
        if not p and desc:
            target = _norm(desc)
            best, score = None, 0.0
            for nm, cand in names:
                s = difflib.SequenceMatcher(None, target, nm).ratio()
                if target and (target in nm or nm in target):
                    s = max(s, 0.85)
                if s > score:
                    best, score = cand, s
            if best and score >= 0.6:
                p, match = best, "name"
                w.append({"code": "NAME_MATCH", "score": int(score * 100)})
        if not p:
            w.append({"code": "PRODUCT_NOT_FOUND"})
        if ln.get("unit") and not unit_code_of(ln.get("unit")) and not (unit and match == "barcode"):
            w.append({"code": "UNKNOWN_UNIT", "unit": str(ln.get("unit"))})
        unit = unit or "PC"
        if p and unit != "PC" and unit not in conv.get(p["product_id"], set()):
            w.append({"code": "UNIT_NOT_SET", "unit": unit})
        if dep is not None and (dep < 0 or (qty is not None and dep > qty)):
            w.append({"code": "DEPOSIT_TOO_BIG"})
            dep = None
        if ln.get("confidence") == "low":
            w.append({"code": "LOW_CONFIDENCE"})
        items.append({
            "line": n, "product_id": p["product_id"] if p else None, "product_code": p["product_code"] if p else None,
            "product_name": p["name"] if p else None, "match": match, "unit_code": unit,
            "qty": str(qty) if qty is not None else None,
            "deposit_qty": str(dep) if dep not in (None, Decimal(0)) else None,
            "source": {"code": code_txt or None, "barcode": bc_txt or None, "description": desc or None,
                       "unit": ln.get("unit"), "unit_price": ln.get("unit_price"), "amount": ln.get("amount")},
            "confidence": ln.get("confidence") or "medium", "warnings": w,
        })
    if not items:
        warnings.append({"code": "NO_ITEMS"})
    if doc_type != "SALE" and any(i["deposit_qty"] for i in items):
        for i in items:
            i["deposit_qty"] = None

    # duplicate bill number
    if doc_no:
        cur.execute("""SELECT d.document_id, d.doc_no, d.status, w.warehouse_code FROM stock_documents d JOIN warehouses w USING (warehouse_id)
                       WHERE d.doc_no=%s AND d.doc_type=%s AND d.reverses_document_id IS NULL""", (doc_no, doc_type))
        for d in cur.fetchall():
            warnings.append({"code": "DUPLICATE_DOC", "document_id": d["document_id"], "doc_no": d["doc_no"],
                             "warehouse": d["warehouse_code"], "status": d["status"]})

    return {
        "doc_type": doc_type, "doc_no": doc_no, "doc_date": doc_date.isoformat(), "warehouse_id": warehouse_id,
        "salesman": salesman or None,
        "customer_id": customer["customer_id"] if customer else None,
        "customer_label": f"{customer['customer_code']} {customer['name']}" if customer else None,
        "customer_suggestion": suggestion, "items": items, "warnings": warnings,
        "is_handwritten": bool(ai.get("is_handwritten")), "notes": ai.get("notes"), "total_amount": ai.get("total_amount"),
    }

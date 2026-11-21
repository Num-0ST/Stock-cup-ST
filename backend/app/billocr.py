"""Free, offline bill reading with Tesseract OCR (Thai / Lao / English).

Tesseract only returns text, so the structure (bill no., date, customer, lines) is recovered with rules that use what
the system already knows: product codes and barcodes, customer codes, warehouse (salesman) codes. Codes are compared
through a "skeleton" that folds look-alike characters (O/0, I/1, S/5, B/8 ...), the usual OCR mistakes.

Two OCR passes run on the same, cleaned-up image: Thai+English (names, units, labels) and English only (codes and
numbers are read much better without the Thai model). Words from both passes are aligned by their position on the line.

Works well on printed bills; handwriting is read poorly - the draft screen shows everything for the user to fix.
The output has the same shape as the AI reader so billscan.build_draft() handles both.
"""
from __future__ import annotations

import difflib
import io
import os
import re
import shutil
import subprocess
import tempfile
from statistics import mean

from .engine import StockError

OCR_TIMEOUT = 90


class OCRUnavailable(StockError):
    status, code = 503, "OCR_UNAVAILABLE"


class OCRFailed(StockError):
    status, code = 502, "OCR_FAILED"


def tesseract_path() -> str | None:
    return shutil.which(os.environ.get("TESSERACT_CMD", "tesseract"))


def available_langs() -> set[str]:
    exe = tesseract_path()
    if not exe:
        return set()
    try:
        out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {ln.strip() for ln in out.splitlines()[1:] if ln.strip()}


def ocr_langs() -> str:
    wanted = (os.environ.get("BILL_OCR_LANGS") or "tha+eng").split("+")
    have = available_langs()
    langs = [lg for lg in wanted if lg in have] or (["eng"] if "eng" in have else [])
    return "+".join(langs)


# ---------------------------------------------------------------- image + tesseract


def preprocess(data: bytes) -> bytes:
    """Upright, grey, contrast-stretched and large enough (Tesseract likes ~30 px tall characters)."""
    from PIL import Image, ImageOps
    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im)
    except Exception:
        raise OCRFailed("เปิดไฟล์รูปไม่ได้", code_hint="BAD_IMAGE_DATA")
    g = ImageOps.autocontrast(ImageOps.grayscale(im), cutoff=1)
    long_side = max(g.size)
    if long_side < 2400:
        k = 2400 / long_side
        g = g.resize((int(g.width * k), int(g.height * k)), Image.LANCZOS)
    elif long_side > 4000:
        k = 4000 / long_side
        g = g.resize((int(g.width * k), int(g.height * k)), Image.LANCZOS)
    buf = io.BytesIO()
    g.save(buf, "PNG")
    return buf.getvalue()


def run_tesseract(png: bytes, langs: str) -> list[dict]:
    """Returns words: {text, conf, left, top, width, height, line}."""
    exe = tesseract_path()
    if not exe or not langs:
        raise OCRUnavailable("ไม่พบโปรแกรม Tesseract OCR บนเซิร์ฟเวอร์", code_hint="OCR_NOT_INSTALLED")
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        f.write(png)
        f.flush()
        try:
            r = subprocess.run([exe, f.name, "stdout", "-l", langs, "--psm", "6", "tsv"],
                               capture_output=True, text=True, timeout=OCR_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise OCRFailed("อ่านรูปนานเกินไป ลองถ่ายใหม่ให้ชัดขึ้น", code_hint="OCR_TIMEOUT")
    if r.returncode != 0:
        raise OCRFailed("OCR ทำงานผิดพลาด", code_hint="OCR_ERROR")
    words = []
    for row in r.stdout.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or c[0] != "5" or not c[11].strip():
            continue
        words.append({"text": c[11].strip(), "conf": float(c[10]), "left": int(c[6]), "top": int(c[7]),
                      "width": int(c[8]), "height": int(c[9]), "line": (int(c[2]), int(c[3]), int(c[4]))})
    return words


# Thai / Lao vowel and tone marks never start a word: a space before one is an OCR artefact
MARK_SPACE = re.compile(r"\s+([\u0E31-\u0E3A\u0E47-\u0E4E\u0EB1-\u0EBC\u0EC8-\u0ECD])")


def join_words(ws: list[dict]) -> str:
    """Tesseract splits Thai/Lao into small pieces; put a space only where the gap on the image is wide."""
    if not ws:
        return ""
    h = sorted(w["height"] for w in ws)[len(ws) // 2] or 1
    out = ws[0]["text"]
    for prev, w in zip(ws, ws[1:]):
        gap = w["left"] - (prev["left"] + prev["width"])
        out += (" " if gap > 0.35 * h else "") + w["text"]
    return MARK_SPACE.sub(r"\1", out)


def group_lines(words: list[dict]) -> list[dict]:
    lines: dict[tuple, dict] = {}
    for w in words:
        ln = lines.setdefault(w["line"], {"words": []})
        ln["words"].append(w)
    out = []
    for ln in lines.values():
        ws = sorted(ln["words"], key=lambda w: w["left"])
        top = min(w["top"] for w in ws)
        bottom = max(w["top"] + w["height"] for w in ws)
        out.append({"words": ws, "top": top, "bottom": bottom, "text": join_words(ws),
                    "conf": mean(w["conf"] for w in ws if w["conf"] >= 0) if any(w["conf"] >= 0 for w in ws) else 0.0,
                    "eng": []})
    return sorted(out, key=lambda x: x["top"])


def attach_english(lines: list[dict], eng_words: list[dict]) -> None:
    """Give every Thai-pass line the English-pass words that sit on it."""
    for w in eng_words:
        cy = w["top"] + w["height"] / 2
        best, dist = None, None
        for ln in lines:
            if ln["top"] - 5 <= cy <= ln["bottom"] + 5:
                d = abs(cy - (ln["top"] + ln["bottom"]) / 2)
                if dist is None or d < dist:
                    best, dist = ln, d
        if best is not None:
            best["eng"].append(w)
    for ln in lines:
        ln["eng"].sort(key=lambda w: w["left"])
        ln["eng_text"] = join_words(ln["eng"])


# ---------------------------------------------------------------- parsing helpers

FOLD = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1", "!": "1", "Z": "2",
                      "S": "5", "B": "8", "G": "6", "T": "7"})
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙໐໑໒໓໔໕໖໗໘໙", "01234567890123456789")


def skeleton(s: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", s.upper()).translate(FOLD)


def tokens(text: str) -> list[str]:
    return [t.strip(".,;:()[]{}'\"") for t in re.split(r"\s+", text.translate(THAI_DIGITS)) if t.strip(".,;:()[]{}'\"")]


UNIT_RX = (r"(ลัง|หีบ|กล่อง|แพ็ค|แพ็ก|แพค|แพก|ห่อ|มัด|ชิ้น|อัน|ขวด|ซอง|กระป๋อง|ถุง|ใบ|โหล|"
           r"ແກັດ|ລັງ|ເປົາ|ແພັກ|ຫໍ່|ຊິ້ນ|ອັນ|ຕຸກ|ກະປ໋ອງ|"
           r"ctn|ct|carton|case|pk|pack|pcs|pc|ea|doz)")
QTY_UNIT = re.compile(r"(?<![\w\-/.])(\d+(?:[.,]\d+)?)\s*" + UNIT_RX + r"(?![a-z])", re.I)
DEPOSIT = re.compile(r"(?:ฝาก|ຝາກ|ค้างส่ง)\D{0,6}?(\d+(?:[.,]\d+)?)\s*" + UNIT_RX + "?", re.I)
CODE_TOKEN = re.compile(r"^[A-Z]{1,4}-?\d{2,}[A-Z0-9\-]*$")
DATE = re.compile(r"\b(\d{1,2}[/.\-]\d{1,2}[/.\-](?:\d{4}|\d{2}))\b")
DOC_LABEL = re.compile(r"(เลขที่|เลขท|ເລກທີ|no\.?|inv|bill)", re.I)
DOCNO_TOKEN = re.compile(r"^[A-Z]{1,4}-?\d[\dA-Z\-/]{2,}$")
SKIP_LINE = re.compile(r"(รวม|ยอด|ภาษี|vat|ส่วนลด|total|ທັງໝົດ|ລວມ|ส่วนลด|ทอน|เงินสด)", re.I)
CUSTOMER_LABEL = re.compile(r"(ลูกค้า|ລູກຄ້າ|customer|นาม)", re.I)
STOP_AFTER_NAME = re.compile(r"\s(พนักงาน|ພະນັກງານ|salesman|sale|โทร|tel|ที่อยู่|เลขที่|วันที่)", re.I)


def _known(conn) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT product_code, barcode FROM products WHERE is_active")
    prods = cur.fetchall()
    cur.execute("SELECT pc.barcode, p.product_code FROM product_unit_conversions pc JOIN products p USING (product_id) "
                "WHERE pc.barcode IS NOT NULL AND p.is_active")
    unit_bc = cur.fetchall()
    cur.execute("SELECT customer_code FROM customers WHERE is_active")
    custs = [r["customer_code"] for r in cur.fetchall()]
    cur.execute("SELECT warehouse_code FROM warehouses WHERE is_active AND warehouse_type='PHYSICAL'")
    whs = [r["warehouse_code"] for r in cur.fetchall()]

    def index(pairs):
        d: dict[str, str] = {}
        for sk, val in pairs:
            if sk and len(sk) >= 3:
                d.setdefault(sk, val)
        return d
    return {
        # skeleton -> (kind, canonical text)
        "product": index([(skeleton(p["product_code"]), ("code", p["product_code"])) for p in prods]
                         + [(skeleton(p["barcode"]), ("barcode", p["barcode"])) for p in prods if p["barcode"]]
                         + [(skeleton(b["barcode"]), ("barcode", b["barcode"])) for b in unit_bc]),
        "customer": index([(skeleton(c), c) for c in custs]),
        "warehouse": index([(skeleton(w), w) for w in whs]),
    }


def _find_known(toks: list[str], idx: dict) -> tuple[str | None, object]:
    for t in toks:
        sk = skeleton(t)
        if sk in idx:
            return t, idx[sk]
    return None, None


def _find_close(toks: list[str], idx: dict) -> tuple[str | None, object]:
    """One character missing / extra (e.g. P00099 read as 00099): only for code-like tokens of 5+ characters."""
    keys = list(idx)
    for t in toks:
        sk = skeleton(t)
        if len(sk) < 5 or not re.search(r"\d", sk):
            continue
        m = difflib.get_close_matches(sk, keys, n=2, cutoff=0.88)
        if len(m) == 1 or (len(m) == 2 and difflib.SequenceMatcher(None, sk, m[0]).ratio() > difflib.SequenceMatcher(None, sk, m[1]).ratio()):
            return t, idx[m[0]]
    return None, None


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------- main entry


def read_bill(conn, data: bytes) -> dict:
    langs = ocr_langs()
    if not tesseract_path() or not langs:
        raise OCRUnavailable("ไม่พบโปรแกรม Tesseract OCR บนเซิร์ฟเวอร์", code_hint="OCR_NOT_INSTALLED")
    png = preprocess(data)
    lines = group_lines(run_tesseract(png, langs))
    if "eng" in langs.split("+") and langs != "eng":
        attach_english(lines, run_tesseract(png, "eng"))
    else:
        for ln in lines:
            ln["eng"], ln["eng_text"] = list(ln["words"]), ln["text"]
    known = _known(conn)
    res = {"doc_no": None, "doc_date": None, "doc_kind": "SALE", "customer_code": None, "customer_name": None,
           "salesman": None, "is_handwritten": False, "items": [], "total_amount": None,
           "notes": None, "engine": "tesseract", "ocr_text": "\n".join(ln["text"] for ln in lines)}
    for ln in lines:
        th, en = ln["text"].translate(THAI_DIGITS), ln.get("eng_text", "").translate(THAI_DIGITS)
        toks = tokens(en) + tokens(th)
        is_item = False

        # ---- product line?
        raw, hit = _find_known(toks, known["product"])
        fuzzy = False
        if not hit:
            raw, hit = _find_close(toks, known["product"])
            fuzzy = hit is not None
        qu = QTY_UNIT.search(th) or QTY_UNIT.search(en)
        looks_header = bool(DATE.search(th) or DOC_LABEL.search(th) or CUSTOMER_LABEL.search(th))
        if hit or (qu and not SKIP_LINE.search(th) and not looks_header):
            is_item = True
        if is_item:
            kind, canon = hit if hit else (None, None)
            dep = DEPOSIT.search(th)
            qty, unit = None, None
            if qu:
                # the first qty+unit that is not the deposit note
                for m in QTY_UNIT.finditer(th if QTY_UNIT.search(th) else en):
                    if dep and dep.start() <= m.start() < dep.end():
                        continue
                    qty, unit = _num(m.group(1)), m.group(2)
                    break
            if qty is None:
                nums = [t for t in tokens(en or th) if re.fullmatch(r"\d+(?:\.\d+)?", t) and (not raw or skeleton(t) != skeleton(raw))]
                qty = _num(nums[0]) if nums else None
            desc = th
            for rx in (DEPOSIT, QTY_UNIT):
                desc = rx.sub(" ", desc)
            if raw:
                desc = re.sub(re.escape(raw), " ", desc)
            parts = re.sub(r"[()\[\]]", " ", desc).split()
            if parts and re.fullmatch(r"\d{1,3}\.?", parts[0]):          # line number
                parts = parts[1:]
            code_sk = skeleton(hit[1]) if hit else (skeleton(raw) if raw else "")
            parts = [re.sub(r"^[A-Za-z]{1,4}-\d+", "", t) for t in parts
                     if not (code_sk and len(skeleton(t)) >= 4
                             and difflib.SequenceMatcher(None, skeleton(t), code_sk).ratio() >= 0.8)]
            parts = [t for t in parts
                     if re.search(r"\w", t)
                     and not re.fullmatch(r"[\d,]*\d[.,]\d{2}|\d{1,3}(?:,\d{3})+|\d{8,}", t)   # prices, amounts, barcodes
                     and not CODE_TOKEN.match(t.upper())]
            desc = " ".join(parts) or None
            conf = "high" if hit and ln["conf"] >= 75 else ("low" if ln["conf"] < 55 else "medium")
            if fuzzy:
                conf = "low"
            res["items"].append({
                "product_code": canon if kind == "code" else (None if hit else next(
                    (t.upper() for t in tokens(en) + tokens(th) if CODE_TOKEN.match(t.upper())), None)),
                "barcode": canon if kind == "barcode" else None,
                "description": desc, "qty": qty, "unit": unit,
                "deposit_qty": _num(dep.group(1)) if dep else None, "confidence": conf,
            })
            continue

        # ---- header facts
        if not res["doc_date"]:
            m = DATE.search(en) or DATE.search(th)
            if m:
                res["doc_date"] = m.group(1)
        if not res["doc_no"]:
            cands = [t for t in tokens(en) if DOCNO_TOKEN.match(t.upper()) and not DATE.fullmatch(t)]
            if cands and (DOC_LABEL.search(th) or re.match(r"^(HSH|HS|IV|CN|INV)", cands[0].upper())):
                res["doc_no"] = cands[0].upper()
            elif DOC_LABEL.search(th):
                after = DOC_LABEL.split(th, maxsplit=1)[-1]
                m = re.search(r"[A-Za-z0-9][A-Za-z0-9\-/]{4,}", after)
                if m and not DATE.fullmatch(m.group(0)):
                    res["doc_no"] = m.group(0).upper()
        if not res["customer_code"]:
            _, c = _find_known(toks, known["customer"])
            if c:
                res["customer_code"] = c
        if not res["customer_name"] and CUSTOMER_LABEL.search(th):
            after = CUSTOMER_LABEL.split(th, maxsplit=1)[-1]
            after = STOP_AFTER_NAME.split(" " + after, maxsplit=1)[0]
            name = " ".join(t for t in after.split() if not re.search(r"\d", t)).strip(" :：-")
            name = re.sub(r"^(ชื่อ|ຊື່|name)\s*[:：]?\s*", "", name, flags=re.I)
            res["customer_name"] = name or None
        if not res["salesman"]:
            _, w = _find_known(toks, known["warehouse"])
            if w:
                res["salesman"] = w
        if SKIP_LINE.search(th) and not res["total_amount"]:
            amounts = [t for t in tokens(en or th) if re.fullmatch(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?", t)]
            if amounts:
                res["total_amount"] = _num(amounts[-1].replace(",", ""))
    if res["doc_no"] and res["doc_no"].upper().startswith("CN"):
        res["doc_kind"] = "CUSTOMER_RETURN"
    avg = mean(ln["conf"] for ln in lines) if lines else 0
    res["is_handwritten"] = avg < 50
    return res

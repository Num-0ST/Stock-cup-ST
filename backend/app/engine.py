"""
Stock Engine — the only place that is allowed to change stock.

Guarantees (all enforced inside one DB transaction per operation):
  * Ledger first: every change writes an append-only row in stock_transactions
    and updates stock_balances in the same transaction.
  * Row locking: stock_balances rows are locked (SELECT ... FOR UPDATE) in
    ascending product_id order, so concurrent operations serialize and cannot deadlock.
  * No negative stock: checked in code (nice error) and again by a DB trigger.
  * No double deduction: a document can only be posted from DRAFT, and a unique
    index allows each document line to hit the ledger once.
  * Reversal instead of edit/delete.
  * All quantities are stored in base units (PC); the conversion factor used is
    snapshotted on each document line.
"""
from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

# ---------------------------------------------------------------- errors


class StockError(Exception):
    status = 400
    code = "STOCK_ERROR"

    def __init__(self, message: str, **detail: Any):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


class NotFound(StockError):
    status, code = 404, "NOT_FOUND"


class ValidationFailed(StockError):
    status, code = 422, "VALIDATION_ERROR"


class DuplicateDocument(StockError):
    status, code = 409, "DUPLICATE_DOCUMENT"


class DocumentStateError(StockError):
    status, code = 409, "DOCUMENT_STATE"


class InsufficientStock(StockError):
    status, code = 409, "INSUFFICIENT_STOCK"


class IdempotencyConflict(StockError):
    status, code = 409, "IDEMPOTENCY_CONFLICT"


# ---------------------------------------------------------------- constants

DOC_TYPES = ("RECEIVING", "SALE", "WITHDRAWAL", "CUSTOMER_RETURN", "SUPPLIER_RETURN")
INCREASES = {"RECEIVING", "CUSTOMER_RETURN"}
DECREASES = {"SALE", "WITHDRAWAL", "SUPPLIER_RETURN"}
PREFIX = {
    "RECEIVING": "RCV", "SALE": "SAL", "WITHDRAWAL": "WTH",
    "CUSTOMER_RETURN": "CRT", "SUPPLIER_RETURN": "SRT",
}

# ---------------------------------------------------------------- helpers


def audit(cur, user_id, action, entity_type, entity_id, warehouse_id=None, old=None, new=None, ip=None):
    cur.execute(
        """INSERT INTO audit_logs (user_id, action, entity_type, entity_id, warehouse_id, old_data, new_data, ip_address)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (user_id, action, entity_type, str(entity_id), warehouse_id,
         Jsonb(old) if old is not None else None, Jsonb(new) if new is not None else None, ip),
    )


def _auto_doc_no(doc_type: str, doc_date: date) -> str:
    return f"{PREFIX[doc_type]}-{doc_date:%Y%m%d}-{secrets.token_hex(2).upper()}"


def _physical_warehouse(cur, warehouse_id: int) -> dict:
    cur.execute("SELECT * FROM warehouses WHERE warehouse_id=%s", (warehouse_id,))
    wh = cur.fetchone()
    if not wh or not wh["is_active"]:
        raise ValidationFailed("ไม่พบคลังสินค้า หรือคลังถูกปิดใช้งาน", field="warehouse_id", code_hint="WAREHOUSE_NOT_FOUND")
    if wh["warehouse_type"] != "PHYSICAL":
        raise ValidationFailed("ไม่สามารถทำเอกสารในคลังระหว่างทาง (TRANSIT) ได้", field="warehouse_id", code_hint="TRANSIT_WAREHOUSE")
    return wh


def resolve_product(cur, item: dict) -> dict:
    if item.get("product_id"):
        cur.execute("SELECT * FROM products WHERE product_id=%s", (item["product_id"],))
    elif item.get("product_code"):
        cur.execute(
            """SELECT p.* FROM products p WHERE p.product_code=%s
               UNION
               SELECT p.* FROM products p WHERE p.barcode=%s
               UNION
               SELECT p.* FROM products p JOIN product_unit_conversions c USING (product_id) WHERE c.barcode=%s
               LIMIT 1""",
            (item["product_code"],) * 3,
        )
    else:
        raise ValidationFailed("ต้องระบุ product_id หรือ product_code", field="product", code_hint="PRODUCT_NOT_FOUND")
    p = cur.fetchone()
    if not p or not p["is_active"]:
        raise ValidationFailed(
            f"ไม่พบสินค้า {item.get('product_code') or item.get('product_id')}", code_hint="PRODUCT_NOT_FOUND")
    return p


def resolve_unit_and_factor(cur, product: dict, item: dict) -> tuple[int, int]:
    """Return (unit_id, factor_to_base). Defaults to the product's base unit."""
    if item.get("unit_id"):
        cur.execute("SELECT unit_id FROM units WHERE unit_id=%s", (item["unit_id"],))
    elif item.get("unit_code"):
        cur.execute("SELECT unit_id FROM units WHERE unit_code=%s", (str(item["unit_code"]).upper(),))
    else:
        return product["base_unit_id"], 1
    u = cur.fetchone()
    if not u:
        raise ValidationFailed(f"ไม่พบหน่วยนับ {item.get('unit_code') or item.get('unit_id')}", code_hint="UNIT_NOT_FOUND")
    if u["unit_id"] == product["base_unit_id"]:
        return u["unit_id"], 1
    cur.execute(
        "SELECT factor_to_base FROM product_unit_conversions WHERE product_id=%s AND unit_id=%s",
        (product["product_id"], u["unit_id"]),
    )
    c = cur.fetchone()
    if not c:
        raise ValidationFailed(
            f"สินค้า {product['product_code']} ยังไม่ได้กำหนดอัตราแปลงสำหรับหน่วยนี้", code_hint="UNIT_NOT_DEFINED")
    return u["unit_id"], c["factor_to_base"]


def _lock_balances(cur, warehouse_id: int, product_ids: list[int]) -> dict[int, int]:
    """Ensure rows exist, then lock them in ascending product_id order. Returns {product_id: qty_on_hand}."""
    ids = sorted(set(product_ids))
    cur.execute(
        """INSERT INTO stock_balances (warehouse_id, product_id, qty_on_hand)
           SELECT %s, x, 0 FROM unnest(%s::bigint[]) AS x ON CONFLICT DO NOTHING""",
        (warehouse_id, ids),
    )
    cur.execute(
        """SELECT product_id, qty_on_hand FROM stock_balances
           WHERE warehouse_id=%s AND product_id = ANY(%s) ORDER BY product_id FOR UPDATE""",
        (warehouse_id, ids),
    )
    return {r["product_id"]: r["qty_on_hand"] for r in cur.fetchall()}


def _write_balances(cur, warehouse_id: int, balances: dict[int, int]) -> None:
    for pid, qty in sorted(balances.items()):
        cur.execute(
            "UPDATE stock_balances SET qty_on_hand=%s, updated_at=now() WHERE warehouse_id=%s AND product_id=%s",
            (qty, warehouse_id, pid),
        )


def _shortage_error(cur, shortages: list[tuple[int, int, int]], message: str) -> InsufficientStock:
    cur.execute("SELECT product_id, product_code, name FROM products WHERE product_id = ANY(%s)",
                ([s[0] for s in shortages],))
    names = {r["product_id"]: r for r in cur.fetchall()}
    items = [{
        "product_id": pid, "product_code": names[pid]["product_code"], "name": names[pid]["name"],
        "available": avail, "required": need,
    } for pid, avail, need in shortages]
    return InsufficientStock(message, items=items)


# ---------------------------------------------------------------- documents


def create_document(conn: psycopg.Connection, data: dict, user_id: int) -> int:
    """Create a DRAFT document. Does not touch stock."""
    doc_type = data.get("doc_type")
    if doc_type not in DOC_TYPES:
        raise ValidationFailed("ประเภทเอกสารไม่ถูกต้อง", field="doc_type", allowed=list(DOC_TYPES), code_hint="BAD_DOC_TYPE")
    items_in = data.get("items") or []
    if not items_in:
        raise ValidationFailed("เอกสารต้องมีอย่างน้อย 1 รายการ", field="items", code_hint="NO_ITEMS")
    doc_date = data.get("doc_date") or date.today()
    if isinstance(doc_date, str):
        doc_date = date.fromisoformat(doc_date)

    with conn.transaction():
        cur = conn.cursor()
        _physical_warehouse(cur, data["warehouse_id"])
        doc_no = (data.get("doc_no") or "").strip() or _auto_doc_no(doc_type, doc_date)

        try:
            with conn.transaction():  # savepoint so a duplicate doesn't poison the outer tx
                cur.execute(
                    """INSERT INTO stock_documents
                         (doc_type, doc_no, doc_date, warehouse_id, customer_id, supplier_id, status,
                          source, import_batch_id, remark, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,'DRAFT',%s,%s,%s,%s) RETURNING document_id""",
                    (doc_type, doc_no, doc_date, data["warehouse_id"], data.get("customer_id"),
                     data.get("supplier_id"), data.get("source", "MANUAL"), data.get("import_batch_id"),
                     data.get("remark"), user_id),
                )
                document_id = cur.fetchone()["document_id"]
        except psycopg.errors.UniqueViolation:
            raise DuplicateDocument(f"เลขที่เอกสาร {doc_no} ถูกใช้แล้ว", doc_no=doc_no)

        for line_no, item in enumerate(items_in, start=1):
            product = resolve_product(cur, item)
            unit_id, factor = resolve_unit_and_factor(cur, product, item)
            try:
                qty = Decimal(str(item.get("qty")))
            except (InvalidOperation, TypeError):
                raise ValidationFailed(f"บรรทัด {line_no}: จำนวนไม่ถูกต้อง", line=line_no, code_hint="BAD_QTY")
            if qty <= 0:
                raise ValidationFailed(f"บรรทัด {line_no}: จำนวนต้องมากกว่า 0", line=line_no, code_hint="BAD_QTY")
            base = qty * factor
            if base != base.to_integral_value():
                raise ValidationFailed(
                    f"บรรทัด {line_no}: จำนวนหลังแปลงเป็น PC ต้องเป็นจำนวนเต็ม ({base})", line=line_no, code_hint="NOT_WHOLE_PC")
            cur.execute(
                """INSERT INTO stock_document_items
                     (document_id, line_no, product_id, unit_id, qty, conversion_factor, base_qty)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (document_id, line_no, product["product_id"], unit_id, qty, factor, int(base)),
            )
        audit(cur, user_id, "CREATE_DOC", "stock_document", document_id, data["warehouse_id"],
              new={"doc_type": doc_type, "doc_no": doc_no, "lines": len(items_in)})
        return document_id


def delete_draft(conn: psycopg.Connection, document_id: int, user_id: int) -> None:
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT * FROM stock_documents WHERE document_id=%s FOR UPDATE", (document_id,))
        doc = cur.fetchone()
        if not doc:
            raise NotFound("ไม่พบเอกสาร")
        if doc["status"] != "DRAFT":
            raise DocumentStateError("ลบได้เฉพาะเอกสารฉบับร่าง (DRAFT) เอกสารที่ POST แล้วต้องใช้การกลับรายการ")
        cur.execute("DELETE FROM stock_documents WHERE document_id=%s", (document_id,))
        audit(cur, user_id, "DELETE_DRAFT", "stock_document", document_id, doc["warehouse_id"],
              old={"doc_no": doc["doc_no"], "doc_type": doc["doc_type"]})


def post_document(conn: psycopg.Connection, document_id: int, user_id: int) -> int:
    """DRAFT -> POSTED, writing the ledger and balances atomically."""
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT * FROM stock_documents WHERE document_id=%s FOR UPDATE", (document_id,))
        doc = cur.fetchone()
        if not doc:
            raise NotFound("ไม่พบเอกสาร")
        if doc["status"] != "DRAFT":
            raise DocumentStateError(
                f"เอกสาร {doc['doc_no']} อยู่ในสถานะ {doc['status']} ไม่สามารถ POST ซ้ำได้", status=doc["status"])
        if doc["doc_type"] not in DOC_TYPES:
            raise DocumentStateError("ประเภทเอกสารนี้ POST ผ่านช่องทางนี้ไม่ได้")
        _physical_warehouse(cur, doc["warehouse_id"])

        cur.execute("SELECT * FROM stock_document_items WHERE document_id=%s ORDER BY line_no", (document_id,))
        items = cur.fetchall()
        if not items:
            raise ValidationFailed("เอกสารไม่มีรายการ")

        wh = doc["warehouse_id"]
        balances = _lock_balances(cur, wh, [i["product_id"] for i in items])
        sign = 1 if doc["doc_type"] in INCREASES else -1

        # simulate every line first so we can report all shortages at once
        sim = dict(balances)
        planned = []
        need: dict[int, int] = {}
        for it in items:
            sim[it["product_id"]] += sign * it["base_qty"]
            need[it["product_id"]] = need.get(it["product_id"], 0) + it["base_qty"]
            planned.append((it, sim[it["product_id"]]))
        if sign < 0:
            shortages = [(pid, balances[pid], need[pid]) for pid, q in sim.items() if q < 0]
            if shortages:
                raise _shortage_error(cur, shortages, "สต็อกไม่พอ ไม่สามารถ POST เอกสารได้")

        for it, bal_after in planned:
            cur.execute(
                """INSERT INTO stock_transactions
                     (txn_date, warehouse_id, product_id, txn_type, qty_in, qty_out, balance_after,
                      document_id, document_item_id, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (doc["doc_date"], wh, it["product_id"], doc["doc_type"],
                 it["base_qty"] if sign > 0 else 0, it["base_qty"] if sign < 0 else 0,
                 bal_after, document_id, it["item_id"], user_id),
            )
        _write_balances(cur, wh, sim)
        cur.execute(
            "UPDATE stock_documents SET status='POSTED', posted_by=%s, posted_at=now() WHERE document_id=%s",
            (user_id, document_id),
        )
        audit(cur, user_id, "POST_DOC", "stock_document", document_id, wh,
              old={"status": "DRAFT"}, new={"status": "POSTED", "doc_no": doc["doc_no"], "doc_type": doc["doc_type"]})
        return document_id


def reverse_document(conn: psycopg.Connection, document_id: int, user_id: int, reason: str | None = None) -> int:
    """Undo a POSTED document with opposite ledger rows. Returns the id of the reversal document."""
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT * FROM stock_documents WHERE document_id=%s FOR UPDATE", (document_id,))
        doc = cur.fetchone()
        if not doc:
            raise NotFound("ไม่พบเอกสาร")
        if doc["reverses_document_id"] is not None:
            raise DocumentStateError("ไม่สามารถกลับรายการของเอกสารกลับรายการได้")
        if doc["status"] != "POSTED":
            raise DocumentStateError(
                f"กลับรายการได้เฉพาะเอกสารที่ POSTED (ปัจจุบัน {doc['status']})", status=doc["status"])

        cur.execute(
            """SELECT txn_id, product_id, qty_in, qty_out FROM stock_transactions
               WHERE document_id=%s AND reverses_txn_id IS NULL ORDER BY txn_id""", (document_id,))
        txns = cur.fetchall()
        wh = doc["warehouse_id"]
        balances = _lock_balances(cur, wh, [t["product_id"] for t in txns])

        sim = dict(balances)
        planned = []
        need: dict[int, int] = {}
        for t in txns:
            delta = t["qty_out"] - t["qty_in"]  # reversal flips the sign of the original effect
            sim[t["product_id"]] += delta
            if delta < 0:
                need[t["product_id"]] = need.get(t["product_id"], 0) - delta
            planned.append((t, sim[t["product_id"]]))
        shortages = [(pid, balances[pid], need.get(pid, 0)) for pid, q in sim.items() if q < 0]
        if shortages:
            raise _shortage_error(cur, shortages,
                                  "ไม่สามารถกลับรายการได้ เพราะจะทำให้สต็อกติดลบ (สินค้าถูกใช้ไปแล้ว)")

        cur.execute(
            """INSERT INTO stock_documents
                 (doc_type, doc_no, doc_date, warehouse_id, customer_id, supplier_id, status, source,
                  reverses_document_id, remark, created_by, posted_by, posted_at)
               VALUES (%s,%s,%s,%s,%s,%s,'POSTED','MANUAL',%s,%s,%s,%s,now()) RETURNING document_id""",
            (doc["doc_type"], f"REV-{doc['doc_no']}", date.today(), wh, doc["customer_id"], doc["supplier_id"],
             document_id, reason or f"กลับรายการเอกสาร {doc['doc_no']}", user_id, user_id),
        )
        rev_id = cur.fetchone()["document_id"]
        cur.execute(
            """INSERT INTO stock_document_items (document_id, line_no, product_id, unit_id, qty, conversion_factor, base_qty)
               SELECT %s, line_no, product_id, unit_id, qty, conversion_factor, base_qty
               FROM stock_document_items WHERE document_id=%s""", (rev_id, document_id))

        for t, bal_after in planned:
            cur.execute(
                """INSERT INTO stock_transactions
                     (txn_date, warehouse_id, product_id, txn_type, qty_in, qty_out, balance_after,
                      document_id, reverses_txn_id, created_by)
                   VALUES (%s,%s,%s,'REVERSAL',%s,%s,%s,%s,%s,%s)""",
                (date.today(), wh, t["product_id"], t["qty_out"], t["qty_in"], bal_after, rev_id,
                 t["txn_id"], user_id),
            )
        _write_balances(cur, wh, sim)
        cur.execute("UPDATE stock_documents SET status='REVERSED' WHERE document_id=%s", (document_id,))
        audit(cur, user_id, "REVERSE_DOC", "stock_document", document_id, wh,
              old={"status": "POSTED"}, new={"status": "REVERSED", "reversal_document_id": rev_id, "reason": reason})
        return rev_id


# ---------------------------------------------------------------- stock count & adjustment


def create_count(conn: psycopg.Connection, warehouse_id: int, product_id: int, counted_qty: int,
                 user_id: int, count_date: date | None = None) -> int:
    if counted_qty is None or int(counted_qty) < 0:
        raise ValidationFailed("จำนวนที่นับได้ต้องไม่ติดลบ", field="counted_qty", code_hint="BAD_QTY")
    with conn.transaction():
        cur = conn.cursor()
        _physical_warehouse(cur, warehouse_id)
        cur.execute("SELECT 1 FROM products WHERE product_id=%s AND is_active", (product_id,))
        if not cur.fetchone():
            raise ValidationFailed("ไม่พบสินค้า", field="product_id", code_hint="PRODUCT_NOT_FOUND")
        cur.execute("SELECT qty_on_hand FROM stock_balances WHERE warehouse_id=%s AND product_id=%s",
                    (warehouse_id, product_id))
        row = cur.fetchone()
        count_date = count_date or date.today()
        cur.execute(
            """INSERT INTO stock_counts (count_no, warehouse_id, product_id, count_date, system_qty, counted_qty, counted_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING count_id""",
            (f"CNT-{count_date:%Y%m%d}-{secrets.token_hex(2).upper()}", warehouse_id, product_id, count_date,
             row["qty_on_hand"] if row else 0, int(counted_qty), user_id),
        )
        cid = cur.fetchone()["count_id"]
        audit(cur, user_id, "CREATE_COUNT", "stock_count", cid, warehouse_id,
              new={"product_id": product_id, "counted_qty": int(counted_qty)})
        return cid


def apply_count(conn: psycopg.Connection, count_id: int, user_id: int, reason: str | None = None) -> int | None:
    """Adjust stock to the counted quantity. Returns adjustment_id, or None when there is no difference."""
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT * FROM stock_counts WHERE count_id=%s FOR UPDATE", (count_id,))
        cnt = cur.fetchone()
        if not cnt:
            raise NotFound("ไม่พบใบตรวจนับ")
        if cnt["status"] != "OPEN":
            raise DocumentStateError(f"ใบตรวจนับนี้อยู่ในสถานะ {cnt['status']} ปรับยอดซ้ำไม่ได้", status=cnt["status"])
        wh, pid = cnt["warehouse_id"], cnt["product_id"]
        balances = _lock_balances(cur, wh, [pid])
        current = balances[pid]
        diff = cnt["counted_qty"] - current
        if diff == 0:
            cur.execute("UPDATE stock_counts SET status='ADJUSTED' WHERE count_id=%s", (count_id,))
            audit(cur, user_id, "APPLY_COUNT_NO_DIFF", "stock_count", count_id, wh)
            return None
        cur.execute(
            """INSERT INTO stock_adjustments (adjust_no, count_id, warehouse_id, product_id, diff_qty, reason, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING adjustment_id""",
            (f"ADJ-{date.today():%Y%m%d}-{secrets.token_hex(2).upper()}", count_id, wh, pid, diff,
             reason or f"ปรับยอดตามใบตรวจนับ {cnt['count_no']}", user_id),
        )
        adj_id = cur.fetchone()["adjustment_id"]
        cur.execute(
            """INSERT INTO stock_transactions
                 (txn_date, warehouse_id, product_id, txn_type, qty_in, qty_out, balance_after, adjustment_id, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (date.today(), wh, pid, "ADJUST_IN" if diff > 0 else "ADJUST_OUT",
             diff if diff > 0 else 0, -diff if diff < 0 else 0, cnt["counted_qty"], adj_id, user_id),
        )
        _write_balances(cur, wh, {pid: cnt["counted_qty"]})
        cur.execute("UPDATE stock_counts SET status='ADJUSTED' WHERE count_id=%s", (count_id,))
        audit(cur, user_id, "APPLY_COUNT", "stock_adjustment", adj_id, wh,
              old={"qty_on_hand": current}, new={"qty_on_hand": cnt["counted_qty"], "diff": diff})
        return adj_id


# ---------------------------------------------------------------- idempotency


def run_idempotent(conn: psycopg.Connection, key: str | None, user_id: int, action: str,
                   request_hash: str, fn: Callable[[], int]) -> tuple[int, bool]:
    """
    Run fn() at most once per key. Returns (result_ref, replayed).
    Key + work share one transaction: if fn fails nothing (including the key) is stored, so the client may retry.
    A concurrent request with the same key waits on the unique index, then gets the stored result.
    """
    if not key:
        return fn(), False
    with conn.transaction():
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO idempotency_keys (idem_key, user_id, action, request_hash)
               VALUES (%s,%s,%s,%s) ON CONFLICT (idem_key) DO NOTHING RETURNING idem_key""",
            (key, user_id, action, request_hash),
        )
        if cur.fetchone() is None:
            cur.execute("SELECT * FROM idempotency_keys WHERE idem_key=%s", (key,))
            old = cur.fetchone()
            if old["request_hash"] != request_hash or old["action"] != action:
                raise IdempotencyConflict("Idempotency-Key นี้ถูกใช้กับคำขออื่นแล้ว")
            if old["status"] == "DONE" and old["response_ref"] is not None:
                return int(old["response_ref"]), True
            raise IdempotencyConflict("คำขอนี้กำลังประมวลผลอยู่")
        result = fn()
        cur.execute("UPDATE idempotency_keys SET status='DONE', response_ref=%s WHERE idem_key=%s",
                    (str(result), key))
        return result, False

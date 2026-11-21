"""Customer deposits ("ฝากคลัง").

A customer buys goods but takes only part of them; the rest stays in our warehouse until a later delivery.

Accounting rule (chosen by the business):
  * Stock is deducted when the SALE is posted. Deposited goods are therefore NOT in qty_on_hand
    and can never be sold to someone else.
  * customer_deposits tracks what we still owe each customer. Physical stock = qty_on_hand + open deposits.
  * Delivering a deposit does not touch the stock ledger (the goods were already deducted); it only
    reduces the outstanding deposit. Cancelling with "return to stock" posts a CUSTOMER_RETURN document.

A deposit is created either together with the sale (deposit qty on each sale line) or later, referencing
an already posted sale. Each sale line can be deposited at most up to its sold quantity.
Every operation locks the sale document row (create) or the deposit row (deliver / cancel) first.
"""
from __future__ import annotations

import secrets
from datetime import date

import psycopg

from . import engine
from .engine import DocumentStateError, NotFound, ValidationFailed, audit

ACTIVE = ("OPEN", "PARTIAL")


def _no(prefix: str, d: date) -> str:
    return f"{prefix}-{d:%Y%m%d}-{secrets.token_hex(3).upper()}"


def held_qty(cur, warehouse_id: int, product_ids: list[int] | None = None) -> dict[int, int]:
    """Outstanding deposits {product_id: qty_base} in one warehouse."""
    cur.execute(
        """SELECT di.product_id, sum(di.qty_base - di.qty_delivered_base - di.qty_cancelled_base)::bigint AS q
           FROM customer_deposit_items di JOIN customer_deposits d USING (deposit_id)
           WHERE d.status IN ('OPEN','PARTIAL') AND d.warehouse_id=%s
             AND (%s::bigint[] IS NULL OR di.product_id = ANY(%s::bigint[]))
           GROUP BY di.product_id""", (warehouse_id, product_ids, product_ids))
    return {r["product_id"]: r["q"] for r in cur.fetchall() if r["q"]}


def _status_of(items: list[dict]) -> str:
    total = sum(i["qty_base"] for i in items)
    delivered = sum(i["qty_delivered_base"] for i in items)
    cancelled = sum(i["qty_cancelled_base"] for i in items)
    if delivered + cancelled >= total:
        return "COMPLETED" if delivered > 0 else "CANCELLED"
    return "PARTIAL" if delivered > 0 else "OPEN"


def _deposited_per_line(cur, sale_document_id: int) -> dict[int, int]:
    """Quantity of each sale line already promised as deposit (cancelled parts are freed again)."""
    cur.execute(
        """SELECT di.sale_item_id, sum(di.qty_base - di.qty_cancelled_base)::bigint AS q
           FROM customer_deposit_items di JOIN customer_deposits d USING (deposit_id)
           WHERE d.sale_document_id=%s GROUP BY di.sale_item_id""", (sale_document_id,))
    return {r["sale_item_id"]: r["q"] for r in cur.fetchall()}


def _insert_deposit(cur, sale: dict, lines: list[tuple[dict, int]], user_id: int, deposit_date: date,
                    due_date: date | None, remark: str | None, customer_name: str | None) -> int:
    """lines = [(sale_item_row, qty_base)]. Caller holds the lock on the sale document."""
    if not sale["customer_id"] and not (customer_name or "").strip():
        raise ValidationFailed("ของฝากต้องระบุลูกค้า (เลือกลูกค้าในบิลขาย หรือกรอกชื่อผู้ฝาก)",
                               field="customer", code_hint="DEPOSIT_NEEDS_CUSTOMER")
    cur.execute(
        """INSERT INTO customer_deposits (deposit_no, sale_document_id, warehouse_id, customer_id, customer_name,
                                          deposit_date, due_date, remark, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING deposit_id""",
        (_no("DEP", deposit_date), sale["document_id"], sale["warehouse_id"], sale["customer_id"],
         (customer_name or "").strip()[:200] or None, deposit_date, due_date, remark, user_id))
    dep_id = cur.fetchone()["deposit_id"]
    for it, q in lines:
        cur.execute(
            """INSERT INTO customer_deposit_items (deposit_id, sale_item_id, product_id, unit_id, conversion_factor, qty_base)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (dep_id, it["item_id"], it["product_id"], it["unit_id"], it["conversion_factor"], q))
    audit(cur, user_id, "CREATE_DEPOSIT", "customer_deposit", dep_id, sale["warehouse_id"],
          new={"sale_doc_no": sale["doc_no"], "lines": [{"product_id": it["product_id"], "qty_base": q} for it, q in lines]})
    return dep_id


def create_on_post(cur, doc: dict, items: list[dict], user_id: int) -> int | None:
    """Called by engine.post_document for SALE documents (same transaction, document row already locked)."""
    lines = [(it, it["deposit_qty_base"]) for it in items if it.get("deposit_qty_base")]
    if not lines:
        return None
    return _insert_deposit(cur, doc, lines, user_id, doc["doc_date"], None,
                           f"ฝากจากบิลขาย {doc['doc_no']}", None)


def _to_base(cur, product_id: int, qty, unit_code: str | None, what: str) -> int:
    cur.execute("SELECT * FROM products WHERE product_id=%s", (product_id,))
    p = cur.fetchone()
    _, factor = engine.resolve_unit_and_factor(cur, p, {"unit_code": unit_code} if unit_code else {})
    try:
        q = int(qty) * factor if float(qty) == int(qty) else None
    except (TypeError, ValueError):
        q = None
    if q is None or q <= 0:
        raise ValidationFailed(f"{what}: จำนวนต้องเป็นจำนวนเต็มมากกว่า 0", code_hint="BAD_QTY")
    return q


def create_deposit(conn: psycopg.Connection, sale_document_id: int, lines_in: list[dict], user_id: int,
                   deposit_date: date | None = None, due_date: date | None = None, remark: str | None = None,
                   customer_name: str | None = None) -> int:
    """Create a deposit later for an already posted sale. lines_in = [{sale_item_id, qty, unit_code?}]."""
    if not lines_in:
        raise ValidationFailed("ต้องเลือกอย่างน้อย 1 รายการ", code_hint="NO_ITEMS")
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT * FROM stock_documents WHERE document_id=%s FOR UPDATE", (sale_document_id,))
        sale = cur.fetchone()
        if not sale:
            raise NotFound("ไม่พบบิลขาย")
        if sale["doc_type"] != "SALE" or sale["reverses_document_id"] is not None:
            raise DocumentStateError("ฝากคลังได้เฉพาะบิลขาย", code_hint="NOT_A_SALE")
        if sale["status"] != "POSTED":
            raise DocumentStateError(f"บิลขาย {sale['doc_no']} อยู่ในสถานะ {sale['status']} ฝากคลังไม่ได้",
                                     status=sale["status"])
        cur.execute("SELECT * FROM stock_document_items WHERE document_id=%s", (sale_document_id,))
        sale_items = {r["item_id"]: r for r in cur.fetchall()}
        used = _deposited_per_line(cur, sale_document_id)
        lines, seen = [], set()
        for ln in lines_in:
            it = sale_items.get(int(ln.get("sale_item_id") or 0))
            if not it:
                raise ValidationFailed("รายการไม่อยู่ในบิลขายนี้", code_hint="BAD_LINE")
            if it["item_id"] in seen:
                raise ValidationFailed("เลือกรายการซ้ำ", code_hint="DUPLICATE_LINE")
            seen.add(it["item_id"])
            q = _to_base(cur, it["product_id"], ln.get("qty"), ln.get("unit_code"), f"บรรทัด {it['line_no']}")
            free = it["base_qty"] - used.get(it["item_id"], 0)
            if q > free:
                raise ValidationFailed(f"บรรทัด {it['line_no']}: ฝากได้ไม่เกิน {free} PC (ขาย {it['base_qty']} PC)",
                                       code_hint="DEPOSIT_EXCEEDS_SALE", line=it["line_no"], available=free)
            lines.append((it, q))
        return _insert_deposit(cur, sale, lines, user_id, deposit_date or date.today(), due_date, remark, customer_name)


def _load(cur, deposit_id: int) -> tuple[dict, list[dict]]:
    cur.execute("SELECT * FROM customer_deposits WHERE deposit_id=%s FOR UPDATE", (deposit_id,))
    d = cur.fetchone()
    if not d:
        raise NotFound("ไม่พบใบฝากสินค้า")
    cur.execute("SELECT * FROM customer_deposit_items WHERE deposit_id=%s ORDER BY deposit_item_id", (deposit_id,))
    return d, cur.fetchall()


def deliver(conn: psycopg.Connection, deposit_id: int, lines_in: list[dict] | None, user_id: int,
            delivery_date: date | None = None, remark: str | None = None) -> int:
    """Hand over deposited goods. lines_in = [{deposit_item_id, qty, unit_code?}] or None for everything left."""
    with conn.transaction():
        cur = conn.cursor()
        dep, items = _load(cur, deposit_id)
        if dep["status"] not in ACTIVE:
            raise DocumentStateError(f"ใบฝาก {dep['deposit_no']} อยู่ในสถานะ {dep['status']} ส่งของไม่ได้",
                                     status=dep["status"])
        by_id = {i["deposit_item_id"]: i for i in items}
        plan: dict[int, int] = {}
        if lines_in is None:
            plan = {i["deposit_item_id"]: i["qty_base"] - i["qty_delivered_base"] - i["qty_cancelled_base"]
                    for i in items}
            plan = {k: v for k, v in plan.items() if v > 0}
        else:
            for ln in lines_in:
                it = by_id.get(int(ln.get("deposit_item_id") or 0))
                if not it:
                    raise ValidationFailed("รายการไม่อยู่ในใบฝากนี้", code_hint="BAD_LINE")
                q = _to_base(cur, it["product_id"], ln.get("qty"), ln.get("unit_code"), "ส่งของ")
                plan[it["deposit_item_id"]] = plan.get(it["deposit_item_id"], 0) + q
        if not plan:
            raise ValidationFailed("ไม่มีรายการที่จะส่ง", code_hint="NO_ITEMS")
        for iid, q in plan.items():
            it = by_id[iid]
            left = it["qty_base"] - it["qty_delivered_base"] - it["qty_cancelled_base"]
            if q > left:
                raise ValidationFailed(f"ส่งเกินยอดฝากคงเหลือ (เหลือ {left} PC)", code_hint="DELIVERY_EXCEEDS_DEPOSIT",
                                       deposit_item_id=iid, remaining=left)
        dd = delivery_date or date.today()
        cur.execute(
            """INSERT INTO deposit_deliveries (delivery_no, deposit_id, delivery_date, remark, created_by)
               VALUES (%s,%s,%s,%s,%s) RETURNING delivery_id""", (_no("DLV", dd), deposit_id, dd, remark, user_id))
        did = cur.fetchone()["delivery_id"]
        for iid, q in sorted(plan.items()):
            cur.execute("INSERT INTO deposit_delivery_items (delivery_id, deposit_item_id, qty_base) VALUES (%s,%s,%s)",
                        (did, iid, q))
            cur.execute("UPDATE customer_deposit_items SET qty_delivered_base = qty_delivered_base + %s WHERE deposit_item_id=%s",
                        (q, iid))
            by_id[iid]["qty_delivered_base"] += q
        status = _status_of(list(by_id.values()))
        cur.execute("UPDATE customer_deposits SET status=%s, closed_at = CASE WHEN %s IN ('COMPLETED','CANCELLED') THEN now() END "
                    "WHERE deposit_id=%s", (status, status, deposit_id))
        audit(cur, user_id, "DELIVER_DEPOSIT", "customer_deposit", deposit_id, dep["warehouse_id"],
              old={"status": dep["status"]}, new={"status": status, "delivery_id": did, "lines": plan})
        return did


def cancel(conn: psycopg.Connection, deposit_id: int, user_id: int, reason: str | None,
           return_to_stock: bool) -> dict:
    """Cancel everything still outstanding.
    return_to_stock=True  -> customer gives the goods back: post a CUSTOMER_RETURN for the remaining quantity.
    return_to_stock=False -> only close the deposit (e.g. it was recorded by mistake / goods already taken)."""
    if not (reason or "").strip():
        raise ValidationFailed("กรุณาระบุเหตุผลการยกเลิก", field="reason", code_hint="REASON_REQUIRED")
    with conn.transaction():
        cur = conn.cursor()
        dep, items = _load(cur, deposit_id)
        if dep["status"] not in ACTIVE:
            raise DocumentStateError(f"ใบฝาก {dep['deposit_no']} อยู่ในสถานะ {dep['status']} ยกเลิกไม่ได้",
                                     status=dep["status"])
        remaining = {i["deposit_item_id"]: i["qty_base"] - i["qty_delivered_base"] - i["qty_cancelled_base"] for i in items}
        return_doc = None
        if return_to_stock:
            per_product: dict[int, int] = {}
            for i in items:
                if remaining[i["deposit_item_id"]] > 0:
                    per_product[i["product_id"]] = per_product.get(i["product_id"], 0) + remaining[i["deposit_item_id"]]
            return_doc = engine.create_document(conn, {
                "doc_type": "CUSTOMER_RETURN", "doc_no": f"DPR-{dep['deposit_no']}", "doc_date": date.today(),
                "warehouse_id": dep["warehouse_id"], "customer_id": dep["customer_id"],
                "remark": f"คืนของฝาก {dep['deposit_no']}: {reason}",
                "items": [{"product_id": p, "qty": q} for p, q in sorted(per_product.items())]}, user_id)
            engine.post_document(conn, return_doc, user_id)
        for iid, q in remaining.items():
            if q > 0:
                cur.execute("UPDATE customer_deposit_items SET qty_cancelled_base = qty_cancelled_base + %s "
                            "WHERE deposit_item_id=%s", (q, iid))
        for i in items:
            i["qty_cancelled_base"] += max(remaining[i["deposit_item_id"]], 0)
        status = _status_of(items)
        cur.execute("""UPDATE customer_deposits SET status=%s, closed_at=now(), cancelled_by=%s, cancel_reason=%s,
                              return_document_id=%s WHERE deposit_id=%s""",
                    (status, user_id, reason, return_doc, deposit_id))
        audit(cur, user_id, "CANCEL_DEPOSIT", "customer_deposit", deposit_id, dep["warehouse_id"],
              old={"status": dep["status"]},
              new={"status": status, "reason": reason, "return_document_id": return_doc,
                   "cancelled": {k: v for k, v in remaining.items() if v > 0}})
        return {"status": status, "return_document_id": return_doc}


def cancel_for_reversed_sale(cur, sale_document_id: int, user_id: int) -> list[int]:
    """The sale itself was reversed (all goods back in stock): close what is still outstanding on its deposits."""
    cur.execute("""SELECT deposit_id FROM customer_deposits WHERE sale_document_id=%s AND status IN ('OPEN','PARTIAL')
                   ORDER BY deposit_id FOR UPDATE""", (sale_document_id,))
    ids = [r["deposit_id"] for r in cur.fetchall()]
    for dep_id in ids:
        cur.execute("""UPDATE customer_deposit_items SET qty_cancelled_base = qty_base - qty_delivered_base
                       WHERE deposit_id=%s""", (dep_id,))
        cur.execute("SELECT * FROM customer_deposit_items WHERE deposit_id=%s", (dep_id,))
        status = _status_of(cur.fetchall())
        cur.execute("""UPDATE customer_deposits SET status=%s, closed_at=now(), cancelled_by=%s,
                              cancel_reason='กลับรายการบิลขาย' WHERE deposit_id=%s""", (status, user_id, dep_id))
        audit(cur, user_id, "CANCEL_DEPOSIT", "customer_deposit", dep_id, None, new={"status": status, "reason": "SALE_REVERSED"})
    return ids


def detail(conn, deposit_id: int) -> dict | None:
    d = conn.execute(
        """SELECT d.*, w.warehouse_code, w.name AS warehouse_name, s.doc_no AS sale_doc_no, s.doc_date AS sale_doc_date,
                  s.status AS sale_status, COALESCE(c.name, d.customer_name) AS customer_display, c.customer_code,
                  usr.full_name AS created_by_name, rd.doc_no AS return_doc_no
           FROM customer_deposits d JOIN warehouses w USING (warehouse_id)
           JOIN stock_documents s ON s.document_id = d.sale_document_id
           LEFT JOIN customers c ON c.customer_id = d.customer_id
           LEFT JOIN stock_documents rd ON rd.document_id = d.return_document_id
           JOIN users usr ON usr.user_id = d.created_by
           WHERE d.deposit_id=%s""", (deposit_id,)).fetchone()
    if not d:
        return None
    d["items"] = conn.execute(
        """SELECT di.*, p.product_code, p.name AS product_name, un.unit_code,
                  di.qty_base - di.qty_delivered_base - di.qty_cancelled_base AS qty_remaining_base,
                  COALESCE((SELECT json_agg(json_build_object('unit_code', u2.unit_code, 'factor_to_base', pc.factor_to_base)
                              ORDER BY pc.factor_to_base DESC)
                            FROM product_unit_conversions pc JOIN units u2 USING (unit_id)
                            WHERE pc.product_id = di.product_id), '[]'::json) AS conversions
           FROM customer_deposit_items di JOIN products p USING (product_id) JOIN units un USING (unit_id)
           WHERE di.deposit_id=%s ORDER BY di.deposit_item_id""", (deposit_id,)).fetchall()
    d["deliveries"] = conn.execute(
        """SELECT dl.*, usr.full_name AS created_by_name,
                  (SELECT json_agg(json_build_object('deposit_item_id', x.deposit_item_id, 'qty_base', x.qty_base))
                   FROM deposit_delivery_items x WHERE x.delivery_id = dl.delivery_id) AS lines
           FROM deposit_deliveries dl JOIN users usr ON usr.user_id = dl.created_by
           WHERE dl.deposit_id=%s ORDER BY dl.delivery_id""", (deposit_id,)).fetchall()
    return d

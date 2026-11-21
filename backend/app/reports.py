"""Phase 5 — dashboard, reports (JSON / Excel / PDF), fast/slow movers and the integrity check.

All figures come straight from the ledger (`stock_transactions`) and balances, scoped to the warehouses the
caller may view. Reversals are netted out of sales/returns so a reversed sale is not counted as sold.
"""
from __future__ import annotations

import glob
import io
import math
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, Callable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

MAX_EXPORT_ROWS = 50_000

# ------------------------------------------------------------------ labels (th / en / lo)

L = {
    "en": {
        "stock_balance": "Stock balance", "low_stock": "Low stock", "movements": "Stock movements",
        "sales_summary": "Sales summary", "movers": "Fast / slow moving", "transfers": "Transfers", "audit": "Audit log",
        "warehouse": "Warehouse", "code": "Code", "product": "Product", "category": "Category", "qty": "Qty (PC)",
        "reorder": "Reorder level", "status": "Status", "date": "Date", "type": "Type", "doc_no": "Doc no.", "in": "In",
        "out": "Out", "balance": "Balance", "user": "User", "sold": "Sold (PC)", "returned": "Returned (PC)",
        "net": "Net sold (PC)", "docs": "Documents", "avg_daily": "Avg / day", "on_hand": "On hand", "cover": "Days of cover",
        "class": "Class", "no": "No.", "mode": "Mode", "from": "From", "to": "To", "items": "Items", "sent": "Sent",
        "received": "Received", "returned_src": "Returned to source", "outstanding": "Outstanding", "time": "Time",
        "action": "Action", "entity": "Entity", "ip": "IP", "detail": "Detail",
        "OK": "OK", "LOW": "Low", "OUT": "Out of stock", "FAST": "Fast", "NORMAL": "Normal", "SLOW": "Slow", "DEAD": "No sales",
        "deposits": "Customer deposits", "deposit_qty": "Deposited (PC)", "held": "Customer deposits (PC)", "physical": "In warehouse (PC)", "customer": "Customer", "deposit_no": "Deposit no.", "due": "Due date", "sale_no": "Sale bill", "delivered": "Delivered", "remaining": "Remaining", "OPEN": "Waiting", "PARTIAL": "Partly delivered", "COMPLETED": "Delivered", "CANCELLED": "Cancelled",
        "generated": "Generated", "page": "Page", "filters": "Filters", "rows": "rows", "truncated": "(truncated)",
    },
    "th": {
        "stock_balance": "ยอดคงเหลือสินค้า", "low_stock": "สินค้าใกล้หมด", "movements": "การเคลื่อนไหวสต็อก",
        "sales_summary": "สรุปยอดขาย", "movers": "สินค้าขายเร็ว / ขายช้า", "transfers": "การโอนสต็อก", "audit": "บันทึกการใช้งาน (Audit)",
        "warehouse": "คลัง", "code": "รหัส", "product": "สินค้า", "category": "หมวดหมู่", "qty": "จำนวน (PC)",
        "reorder": "จุดสั่งซื้อ", "status": "สถานะ", "date": "วันที่", "type": "ประเภท", "doc_no": "เลขที่เอกสาร", "in": "เข้า",
        "out": "ออก", "balance": "คงเหลือ", "user": "ผู้ทำรายการ", "sold": "ขาย (PC)", "returned": "รับคืน (PC)",
        "net": "ขายสุทธิ (PC)", "docs": "จำนวนเอกสาร", "avg_daily": "เฉลี่ย/วัน", "on_hand": "คงเหลือ", "cover": "ขายได้อีก (วัน)",
        "class": "กลุ่ม", "no": "เลขที่", "mode": "รูปแบบ", "from": "ต้นทาง", "to": "ปลายทาง", "items": "รายการ", "sent": "ส่ง",
        "received": "รับแล้ว", "returned_src": "คืนต้นทาง", "outstanding": "ค้างรับ", "time": "เวลา",
        "action": "การกระทำ", "entity": "ข้อมูล", "ip": "IP", "detail": "รายละเอียด",
        "OK": "ปกติ", "LOW": "ใกล้หมด", "OUT": "หมด", "FAST": "ขายเร็ว", "NORMAL": "ปานกลาง", "SLOW": "ขายช้า", "DEAD": "ไม่มียอดขาย",
        "deposits": "ของฝากลูกค้า (ฝากคลัง)", "deposit_qty": "ฝาก (PC)", "held": "ของฝากลูกค้า (PC)", "physical": "ของในคลังจริง (PC)", "customer": "ลูกค้า", "deposit_no": "เลขที่ใบฝาก", "due": "กำหนดส่ง", "sale_no": "บิลขาย", "delivered": "ส่งแล้ว", "remaining": "คงค้าง", "OPEN": "รอส่ง", "PARTIAL": "ส่งบางส่วน", "COMPLETED": "ส่งครบ", "CANCELLED": "ยกเลิก",
        "generated": "สร้างเมื่อ", "page": "หน้า", "filters": "เงื่อนไข", "rows": "แถว", "truncated": "(ตัดทอน)",
    },
    "lo": {
        "stock_balance": "ຍອດຄົງເຫຼືອສິນຄ້າ", "low_stock": "ສິນຄ້າໃກ້ໝົດ", "movements": "ການເຄື່ອນໄຫວສະຕັອກ",
        "sales_summary": "ສະຫຼຸບຍອດຂາຍ", "movers": "ສິນຄ້າຂາຍໄວ / ຂາຍຊ້າ", "transfers": "ການໂອນສະຕັອກ", "audit": "ບັນທຶກການໃຊ້ງານ (Audit)",
        "warehouse": "ສາງ", "code": "ລະຫັດ", "product": "ສິນຄ້າ", "category": "ໝວດໝູ່", "qty": "ຈຳນວນ (PC)",
        "reorder": "ຈຸດສັ່ງຊື້", "status": "ສະຖານະ", "date": "ວັນທີ", "type": "ປະເພດ", "doc_no": "ເລກທີເອກະສານ", "in": "ເຂົ້າ",
        "out": "ອອກ", "balance": "ຄົງເຫຼືອ", "user": "ຜູ້ເຮັດລາຍການ", "sold": "ຂາຍ (PC)", "returned": "ຮັບຄືນ (PC)",
        "net": "ຂາຍສຸດທິ (PC)", "docs": "ຈຳນວນເອກະສານ", "avg_daily": "ສະເລ່ຍ/ມື້", "on_hand": "ຄົງເຫຼືອ", "cover": "ຂາຍໄດ້ອີກ (ມື້)",
        "class": "ກຸ່ມ", "no": "ເລກທີ", "mode": "ຮູບແບບ", "from": "ຕົ້ນທາງ", "to": "ປາຍທາງ", "items": "ລາຍການ", "sent": "ສົ່ງ",
        "received": "ຮັບແລ້ວ", "returned_src": "ສົ່ງຄືນຕົ້ນທາງ", "outstanding": "ຄ້າງຮັບ", "time": "ເວລາ",
        "action": "ການກະທຳ", "entity": "ຂໍ້ມູນ", "ip": "IP", "detail": "ລາຍລະອຽດ",
        "OK": "ປົກກະຕິ", "LOW": "ໃກ້ໝົດ", "OUT": "ໝົດ", "FAST": "ຂາຍໄວ", "NORMAL": "ປານກາງ", "SLOW": "ຂາຍຊ້າ", "DEAD": "ບໍ່ມີຍອດຂາຍ",
        "deposits": "ເຄື່ອງຝາກລູກຄ້າ (ຝາກສາງ)", "deposit_qty": "ຝາກ (PC)", "held": "ເຄື່ອງຝາກລູກຄ້າ (PC)", "physical": "ຂອງໃນສາງຕົວຈິງ (PC)", "customer": "ລູກຄ້າ", "deposit_no": "ເລກທີໃບຝາກ", "due": "ກຳນົດສົ່ງ", "sale_no": "ບິນຂາຍ", "delivered": "ສົ່ງແລ້ວ", "remaining": "ຄົງຄ້າງ", "OPEN": "ລໍຖ້າສົ່ງ", "PARTIAL": "ສົ່ງບາງສ່ວນ", "COMPLETED": "ສົ່ງຄົບ", "CANCELLED": "ຍົກເລີກ",
        "generated": "ສ້າງເມື່ອ", "page": "ໜ້າ", "filters": "ເງື່ອນໄຂ", "rows": "ແຖວ", "truncated": "(ຕັດທອນ)",
    },
}


def lab(lang: str, key: str) -> str:
    return L.get(lang, L["en"]).get(key) or L["en"].get(key, key)


class ReportError(Exception):
    pass


# ------------------------------------------------------------------ SQL fragments

# net sold / returned per ledger row, with reversals netted against the transaction they reverse
NET_SALES_SQL = """
    SELECT t.warehouse_id, t.product_id, COALESCE(o.txn_date, t.txn_date) AS txn_date, t.document_id, d.status AS doc_status,
           CASE WHEN t.txn_type='SALE' THEN t.qty_out
                WHEN t.txn_type='REVERSAL' AND o.txn_type='SALE' THEN -t.qty_in ELSE 0 END AS sold,
           CASE WHEN t.txn_type='CUSTOMER_RETURN' THEN t.qty_in
                WHEN t.txn_type='REVERSAL' AND o.txn_type='CUSTOMER_RETURN' THEN -t.qty_out ELSE 0 END AS returned
    FROM stock_transactions t LEFT JOIN stock_transactions o ON o.txn_id = t.reverses_txn_id
    LEFT JOIN stock_documents d ON d.document_id = t.document_id
    WHERE t.warehouse_id = ANY(%(whs)s) AND COALESCE(o.txn_date, t.txn_date) BETWEEN %(df)s AND %(dto)s
      AND (t.txn_type IN ('SALE','CUSTOMER_RETURN') OR (t.txn_type='REVERSAL' AND o.txn_type IN ('SALE','CUSTOMER_RETURN')))
"""   # a reversal is booked against the date of the transaction it undoes; docs counted only while still POSTED


# customer deposits still waiting for delivery, per warehouse/product (already sold, still physically here)
HELD_SQL = """SELECT d.warehouse_id, di.product_id, SUM(di.qty_base - di.qty_delivered_base - di.qty_cancelled_base) AS q
              FROM customer_deposit_items di JOIN customer_deposits d USING (deposit_id)
              WHERE d.status IN ('OPEN','PARTIAL') GROUP BY d.warehouse_id, di.product_id"""


def _scope(whs: list[int], warehouse_id: int | None) -> list[int]:
    return [w for w in whs if w == warehouse_id] if warehouse_id else whs


def _dates(p: dict, default_days: int = 30) -> tuple[date, date]:
    dto = p.get("date_to") or date.today()
    df = p.get("date_from") or (dto - timedelta(days=default_days - 1))
    if df > dto:
        raise ReportError("วันที่เริ่มต้องไม่เกินวันที่สิ้นสุด")
    if (dto - df).days > 366 * 3:
        raise ReportError("ช่วงวันที่ยาวเกินไป (สูงสุด 3 ปี)")
    return df, dto


# ------------------------------------------------------------------ report queries
# each returns (rows as list[dict], applied-filters description dict)


def r_stock_balance(conn, whs, p, low_only=False):
    scope = _scope(whs, p.get("warehouse_id"))
    rows = conn.execute(
        f"""SELECT w.warehouse_code, p.product_code, p.name AS product_name, COALESCE(c.name,'') AS category,
                  COALESCE(b.qty_on_hand,0) AS qty, COALESCE(h.q,0)::bigint AS deposit_qty,
                  (COALESCE(b.qty_on_hand,0) + COALESCE(h.q,0))::bigint AS physical, p.reorder_level,
                  CASE WHEN COALESCE(b.qty_on_hand,0) <= 0 THEN 'OUT'
                       WHEN p.reorder_level > 0 AND b.qty_on_hand <= p.reorder_level THEN 'LOW' ELSE 'OK' END AS status
           FROM products p CROSS JOIN warehouses w
           LEFT JOIN stock_balances b ON b.product_id=p.product_id AND b.warehouse_id=w.warehouse_id
           LEFT JOIN categories c ON c.category_id=p.category_id
           LEFT JOIN ({HELD_SQL}) h ON h.product_id=p.product_id AND h.warehouse_id=w.warehouse_id
           WHERE p.is_active AND w.warehouse_type='PHYSICAL' AND w.warehouse_id = ANY(%(whs)s)
             AND (COALESCE(b.qty_on_hand,0) <> 0 OR p.reorder_level > 0 OR COALESCE(h.q,0) <> 0)
             AND (%(cat)s::int IS NULL OR p.category_id = %(cat)s)
             AND (%(q)s::text IS NULL OR p.product_code ILIKE %(like)s OR p.name ILIKE %(like)s OR p.barcode = %(q)s)
           ORDER BY w.warehouse_code, p.product_code""",
        {"whs": scope, "cat": p.get("category_id"), "q": p.get("q") or None, "like": f"%{p.get('q') or ''}%"}).fetchall()
    if low_only or p.get("low_only"):
        rows = [r for r in rows if r["status"] != "OK"]
    return rows


def r_low_stock(conn, whs, p):
    return r_stock_balance(conn, whs, p, low_only=True)


def r_movements(conn, whs, p):
    df, dto = _dates(p, 7)
    return conn.execute(
        """SELECT t.txn_date, w.warehouse_code, p.product_code, p.name AS product_name,
                  COALESCE(d.doc_type, t.txn_type) AS type,
                  COALESCE(d.doc_no, a.adjust_no, tr.transfer_no, '') AS doc_no,
                  t.qty_in, t.qty_out, t.balance_after, usr.full_name AS user_name
           FROM stock_transactions t JOIN products p USING (product_id) JOIN warehouses w USING (warehouse_id)
           JOIN users usr ON usr.user_id=t.created_by
           LEFT JOIN stock_documents d ON d.document_id=t.document_id
           LEFT JOIN stock_adjustments a ON a.adjustment_id=t.adjustment_id
           LEFT JOIN stock_transfers tr ON tr.transfer_id=t.transfer_id
           WHERE t.warehouse_id = ANY(%(whs)s) AND t.txn_date BETWEEN %(df)s AND %(dto)s
             AND (%(pid)s::bigint IS NULL OR t.product_id=%(pid)s)
             AND (%(typ)s::text IS NULL OR t.txn_type=%(typ)s OR d.doc_type=%(typ)s)
           ORDER BY t.txn_id DESC LIMIT %(lim)s""",
        {"whs": _scope(whs, p.get("warehouse_id")), "df": df, "dto": dto, "pid": p.get("product_id"),
         "typ": p.get("txn_type") or None, "lim": MAX_EXPORT_ROWS + 1}).fetchall()


def r_sales_summary(conn, whs, p):
    df, dto = _dates(p, 30)
    return conn.execute(
        f"""WITH n AS ({NET_SALES_SQL})
            SELECT p.product_code, p.name AS product_name, COALESCE(c.name,'') AS category,
                   COALESCE(SUM(n.sold),0)::bigint AS sold, COALESCE(SUM(n.returned),0)::bigint AS returned,
                   COALESCE(SUM(n.sold) - SUM(n.returned),0)::bigint AS net,
                   COUNT(DISTINCT n.document_id) FILTER (WHERE n.sold > 0 AND n.doc_status='POSTED') AS docs
            FROM n JOIN products p USING (product_id) LEFT JOIN categories c ON c.category_id=p.category_id
            GROUP BY p.product_code, p.name, c.name
            HAVING SUM(n.sold) <> 0 OR SUM(n.returned) <> 0
            ORDER BY net DESC, p.product_code""",
        {"whs": _scope(whs, p.get("warehouse_id")), "df": df, "dto": dto}).fetchall()


def classify_movers(items: list[dict], fast_pct: float = 0.2, slow_pct: float = 0.2) -> None:
    """In place: FAST = top 20 % of sold qty among products that sold, SLOW = bottom 20 %, DEAD = stock but no sales."""
    sellers = sorted((i for i in items if i["sold"] > 0), key=lambda i: (-i["sold"], i["product_code"]))
    n = len(sellers)
    nf = math.ceil(n * fast_pct) if n else 0
    ns = math.ceil(n * slow_pct) if n >= 3 else 0
    for idx, i in enumerate(sellers):
        i["class"] = "FAST" if idx < nf else ("SLOW" if idx >= n - ns else "NORMAL")
    for i in items:
        if i["sold"] <= 0:
            i["class"] = "DEAD"


def r_movers(conn, whs, p):
    days = 30 if p.get("days") is None else int(p["days"])
    if not 1 <= days <= 365:
        raise ReportError("จำนวนวันต้องอยู่ระหว่าง 1–365")
    dto = date.today()
    df = dto - timedelta(days=days - 1)
    scope = _scope(whs, p.get("warehouse_id"))
    rows = conn.execute(
        f"""WITH n AS ({NET_SALES_SQL}),
                 s AS (SELECT product_id, (SUM(sold) - SUM(returned))::bigint AS sold FROM n GROUP BY product_id),
                 oh AS (SELECT b.product_id, SUM(b.qty_on_hand)::bigint AS on_hand FROM stock_balances b
                        JOIN warehouses w USING (warehouse_id)
                        WHERE b.warehouse_id = ANY(%(whs)s) AND w.warehouse_type='PHYSICAL' GROUP BY b.product_id)
            SELECT p.product_code, p.name AS product_name, COALESCE(s.sold,0) AS sold, COALESCE(oh.on_hand,0) AS on_hand
            FROM products p LEFT JOIN s USING (product_id) LEFT JOIN oh USING (product_id)
            WHERE p.is_active AND (COALESCE(s.sold,0) > 0 OR COALESCE(oh.on_hand,0) > 0)""",
        {"whs": scope, "df": df, "dto": dto}).fetchall()
    for r in rows:
        r["sold"] = max(int(r["sold"]), 0)
        r["avg_daily"] = round(r["sold"] / days, 2)
        r["cover"] = round(r["on_hand"] / (r["sold"] / days), 1) if r["sold"] > 0 else None
    classify_movers(rows)
    order = {"FAST": 0, "NORMAL": 1, "SLOW": 2, "DEAD": 3}
    rows.sort(key=lambda r: (order[r["class"]], -r["sold"], r["product_code"]))
    if p.get("class"):
        rows = [r for r in rows if r["class"] == p["class"]]
    return rows


def r_transfers(conn, whs, p):
    df, dto = _dates(p, 90)
    return conn.execute(
        """SELECT t.transfer_no AS no, t.transfer_mode AS mode, f.warehouse_code AS from_wh, d.warehouse_code AS to_wh,
                  t.transfer_date AS date, t.status,
                  (SELECT count(*) FROM stock_transfer_items WHERE transfer_id=t.transfer_id) AS items,
                  (SELECT COALESCE(sum(qty_sent_base),0) FROM stock_transfer_items WHERE transfer_id=t.transfer_id) AS sent,
                  (SELECT COALESCE(sum(qty_received_base),0) FROM stock_transfer_items WHERE transfer_id=t.transfer_id) AS received,
                  (SELECT COALESCE(sum(qty_cancelled_base),0) FROM stock_transfer_items WHERE transfer_id=t.transfer_id) AS returned_src
           FROM stock_transfers t JOIN warehouses f ON f.warehouse_id=t.from_warehouse_id JOIN warehouses d ON d.warehouse_id=t.to_warehouse_id
           WHERE (t.from_warehouse_id = ANY(%(whs)s) OR t.to_warehouse_id = ANY(%(whs)s))
             AND t.transfer_date BETWEEN %(df)s AND %(dto)s AND (%(st)s::text IS NULL OR t.status=%(st)s)
           ORDER BY t.transfer_id DESC LIMIT %(lim)s""",
        {"whs": _scope(whs, p.get("warehouse_id")), "df": df, "dto": dto, "st": p.get("status") or None, "lim": MAX_EXPORT_ROWS + 1}).fetchall()


def r_deposits(conn, whs, p):
    st = p.get("status") or None
    return conn.execute(
        """SELECT d.deposit_no, d.deposit_date, d.due_date AS due, COALESCE(c.name, d.customer_name, '') AS customer,
                  w.warehouse_code, s.doc_no AS sale_no, p.product_code, p.name AS product_name,
                  di.qty_base AS deposit_qty, di.qty_delivered_base AS delivered,
                  (di.qty_base - di.qty_delivered_base - di.qty_cancelled_base) AS remaining, d.status
           FROM customer_deposits d JOIN customer_deposit_items di USING (deposit_id)
           JOIN products p ON p.product_id = di.product_id JOIN warehouses w ON w.warehouse_id = d.warehouse_id
           JOIN stock_documents s ON s.document_id = d.sale_document_id LEFT JOIN customers c ON c.customer_id = d.customer_id
           WHERE d.warehouse_id = ANY(%(whs)s)
             AND (CASE WHEN %(st)s::text IS NULL THEN d.status IN ('OPEN','PARTIAL') ELSE d.status = %(st)s END)
             AND (%(df)s::date IS NULL OR d.deposit_date >= %(df)s) AND (%(dto)s::date IS NULL OR d.deposit_date <= %(dto)s)
             AND (%(pid)s::bigint IS NULL OR di.product_id = %(pid)s)
             AND (%(q)s::text IS NULL OR COALESCE(c.name, d.customer_name, '') ILIKE %(like)s OR d.deposit_no ILIKE %(like)s
                  OR s.doc_no ILIKE %(like)s OR p.product_code ILIKE %(like)s OR p.name ILIKE %(like)s)
           ORDER BY d.due_date NULLS LAST, d.deposit_date, d.deposit_no, p.product_code LIMIT %(lim)s""",
        {"whs": _scope(whs, p.get("warehouse_id")), "st": st, "df": p.get("date_from"), "dto": p.get("date_to"),
         "pid": p.get("product_id"), "q": p.get("q") or None, "like": f"%{p.get('q') or ''}%", "lim": MAX_EXPORT_ROWS + 1}).fetchall()


def r_audit(conn, whs, p):
    df, dto = _dates(p, 7)
    return conn.execute(
        """SELECT a.occurred_at AS time, COALESCE(u.username,'') AS user_name, a.action, COALESCE(a.entity_type,'') AS entity,
                  COALESCE(a.entity_id,'') AS entity_id, COALESCE(host(a.ip_address),'') AS ip,
                  COALESCE(left(a.new_data::text, 300),'') AS detail
           FROM audit_logs a LEFT JOIN users u ON u.user_id=a.user_id
           WHERE a.occurred_at >= %(df)s::date AND a.occurred_at < (%(dto)s::date + 1)
             AND (%(act)s::text IS NULL OR a.action=%(act)s)
           ORDER BY a.audit_id DESC LIMIT %(lim)s""",
        {"df": df, "dto": dto, "act": p.get("action") or None, "lim": MAX_EXPORT_ROWS + 1}).fetchall()


# name -> (title key, [(row key, label key, kind)], fn, admin_only)
REPORTS: dict[str, tuple[str, list[tuple[str, str, str]], Callable, bool]] = {
    "stock_balance": ("stock_balance", [("warehouse_code", "warehouse", "t"), ("product_code", "code", "t"), ("product_name", "product", "t"),
                                         ("category", "category", "t"), ("qty", "qty", "n"), ("deposit_qty", "held", "n"), ("physical", "physical", "n"),
                                         ("reorder_level", "reorder", "n"), ("status", "status", "s")], r_stock_balance, False),
    "low_stock": ("low_stock", [("warehouse_code", "warehouse", "t"), ("product_code", "code", "t"), ("product_name", "product", "t"),
                                ("qty", "qty", "n"), ("reorder_level", "reorder", "n"), ("status", "status", "s")], r_low_stock, False),
    "movements": ("movements", [("txn_date", "date", "d"), ("warehouse_code", "warehouse", "t"), ("product_code", "code", "t"), ("product_name", "product", "t"),
                                ("type", "type", "t"), ("doc_no", "doc_no", "t"), ("qty_in", "in", "n"), ("qty_out", "out", "n"),
                                ("balance_after", "balance", "n"), ("user_name", "user", "t")], r_movements, False),
    "sales_summary": ("sales_summary", [("product_code", "code", "t"), ("product_name", "product", "t"), ("category", "category", "t"), ("sold", "sold", "n"),
                                         ("returned", "returned", "n"), ("net", "net", "n"), ("docs", "docs", "n")], r_sales_summary, False),
    "movers": ("movers", [("product_code", "code", "t"), ("product_name", "product", "t"), ("sold", "sold", "n"), ("avg_daily", "avg_daily", "f"),
                          ("on_hand", "on_hand", "n"), ("cover", "cover", "f"), ("class", "class", "s")], r_movers, False),
    "transfers": ("transfers", [("no", "no", "t"), ("mode", "mode", "t"), ("from_wh", "from", "t"), ("to_wh", "to", "t"), ("date", "date", "d"), ("status", "status", "t"),
                                ("items", "items", "n"), ("sent", "sent", "n"), ("received", "received", "n"), ("returned_src", "returned_src", "n")], r_transfers, False),
    "deposits": ("deposits", [("deposit_no", "deposit_no", "t"), ("deposit_date", "date", "d"), ("due", "due", "d"), ("customer", "customer", "t"),
                              ("warehouse_code", "warehouse", "t"), ("sale_no", "sale_no", "t"), ("product_code", "code", "t"),
                              ("product_name", "product", "t"), ("deposit_qty", "deposit_qty", "n"), ("delivered", "delivered", "n"),
                              ("remaining", "remaining", "n"), ("status", "status", "s")], r_deposits, False),
    "audit": ("audit", [("time", "time", "dt"), ("user_name", "user", "t"), ("action", "action", "t"), ("entity", "entity", "t"), ("entity_id", "no", "t"),
                        ("ip", "ip", "t"), ("detail", "detail", "t")], r_audit, True),
}


def run_report(conn, name: str, whs: list[int], params: dict) -> tuple[list[dict], int, bool]:
    """Returns (rows, total_before_cap, truncated)."""
    if name not in REPORTS:
        raise ReportError("ไม่พบรายงาน")
    rows = REPORTS[name][2](conn, whs, params)
    truncated = len(rows) > MAX_EXPORT_ROWS
    return (rows[:MAX_EXPORT_ROWS], len(rows), truncated)


def display(row: dict, key: str, kind: str, lang: str) -> Any:
    v = row.get(key)
    if v is None:
        return ""
    if kind == "s":
        return lab(lang, v) if v in L["en"] else v
    if kind == "d":
        return v.isoformat() if hasattr(v, "isoformat") else str(v)
    if kind == "dt":
        return v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else str(v)
    return v


# ------------------------------------------------------------------ exports


def to_xlsx(name: str, rows: list[dict], lang: str, filters: str) -> bytes:
    title_key, cols, _, _ = REPORTS[name]
    wb = Workbook()
    ws = wb.active
    ws.title = lab(lang, title_key)[:31]
    ws.append([lab(lang, title_key)])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"{lab(lang, 'generated')}: {datetime.now():%Y-%m-%d %H:%M}   {filters}"])
    ws.append([])
    ws.append([lab(lang, c[1]) for c in cols])
    for c in ws[4]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F5FBF")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    widths = [len(lab(lang, c[1])) + 2 for c in cols]
    for r in rows:
        vals = [display(r, k, kind, lang) for k, _, kind in cols]
        ws.append([float(v) if hasattr(v, "as_tuple") else v for v in vals])
        for i, v in enumerate(vals):
            widths[i] = min(max(widths[i], len(str(v)) + 2), 60)
    for i, (k, _, kind) in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths[i - 1]
        if kind in ("n", "f"):
            for cell in ws.iter_rows(min_row=5, min_col=i, max_col=i):
                cell[0].number_format = "#,##0" if kind == "n" else "#,##0.00"
                cell[0].alignment = Alignment(horizontal="right")
    ws.freeze_panes = "A5"
    if rows:
        ws.auto_filter.ref = f"A4:{get_column_letter(len(cols))}{4 + len(rows)}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _find(patterns: list[str]) -> str | None:
    for pat in patterns:
        hit = sorted(glob.glob(pat, recursive=True))
        if hit:
            return hit[0]
    return None


@lru_cache(maxsize=1)
def pdf_fonts() -> tuple[str | None, list[str]]:
    """(latin font, [fallback fonts for Thai / Lao]). Install fonts-dejavu-core fonts-tlwg-loma-ttf fonts-lao (see Dockerfile)."""
    latin = _find(["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/**/DejaVuSans.ttf", "/usr/share/fonts/**/NotoSans-Regular.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"])
    thai = _find(["/usr/share/fonts/**/NotoSansThai-Regular.ttf", "/usr/share/fonts/**/NotoSansThai[[]*.ttf", "/usr/share/fonts/truetype/tlwg/Loma.ttf",
                  "/usr/share/fonts/truetype/tlwg/Garuda.ttf", "/usr/share/fonts/truetype/freefont/FreeSerif.ttf"])
    lao = _find(["/usr/share/fonts/**/NotoSansLao-Regular.ttf", "/usr/share/fonts/**/NotoSansLao[[]*.ttf", "/usr/share/fonts/truetype/lao/Phetsarath_OT.ttf",
                 "/usr/share/fonts/**/Phetsarath*.ttf"])
    return latin, [f for f in (thai, lao) if f]


def to_pdf(name: str, rows: list[dict], lang: str, filters: str, truncated: bool = False) -> bytes:
    from fpdf import FPDF
    title_key, cols, _, _ = REPORTS[name]
    latin, fallbacks = pdf_fonts()
    if not latin:
        raise ReportError("ไม่พบฟอนต์สำหรับสร้าง PDF บนเซิร์ฟเวอร์")

    class Doc(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font("F", size=8)
            self.cell(0, 8, f"{lab(lang, 'page')} {self.page_no()}/{{nb}}", align="C")

    pdf = Doc(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(True, margin=14)
    pdf.add_font("F", "", latin)
    fb = []
    for i, f in enumerate(fallbacks):
        pdf.add_font(f"FB{i}", "", f)
        fb.append(f"FB{i}")
    pdf.set_fallback_fonts(fb)
    try:
        pdf.set_text_shaping(True)                 # proper Thai / Lao vowel & tone-mark placement (needs uharfbuzz)
    except Exception:
        pass
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_font("F", size=15)
    pdf.cell(0, 9, lab(lang, title_key), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("F", size=8)
    sub = f"{lab(lang, 'generated')}: {datetime.now():%Y-%m-%d %H:%M}  |  {len(rows):,} {lab(lang, 'rows')} {lab(lang, 'truncated') if truncated else ''}"
    pdf.cell(0, 5, sub, new_x="LMARGIN", new_y="NEXT")
    if filters:
        pdf.cell(0, 5, f"{lab(lang, 'filters')}: {filters}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    total_w = pdf.w - pdf.l_margin - pdf.r_margin
    weights = []
    for k, lk, kind in cols:
        longest = max([len(str(display(r, k, kind, lang))) for r in rows[:200]] + [len(lab(lang, lk))])
        weights.append(min(max(longest, 5), 40 if kind == "t" else 14))
    scale = total_w / sum(weights)
    widths = [w * scale for w in weights]

    def header():
        pdf.set_font("F", size=8)
        pdf.set_fill_color(31, 95, 191)
        pdf.set_text_color(255, 255, 255)
        for (k, lk, kind), w in zip(cols, widths):
            pdf.cell(w, 7, lab(lang, lk), border=1, align="C", fill=True)
        pdf.ln()
        pdf.set_text_color(0, 0, 0)

    header()
    pdf.set_font("F", size=8)
    for i, r in enumerate(rows):
        if pdf.get_y() > pdf.h - 20:
            pdf.add_page()
            header()
            pdf.set_font("F", size=8)
        pdf.set_fill_color(246, 248, 252) if i % 2 else pdf.set_fill_color(255, 255, 255)
        for (k, lk, kind), w in zip(cols, widths):
            v = display(r, k, kind, lang)
            if isinstance(v, (int, float)) or hasattr(v, "as_tuple"):
                v = f"{v:,.2f}" if kind == "f" else f"{int(v):,}"
                align = "R"
            else:
                v = str(v)
                align = "L"
            pdf.cell(w, 6, v, border=1, align=align, fill=True)
        pdf.ln()
    return bytes(pdf.output())


# ------------------------------------------------------------------ dashboard


def dashboard(conn, whs: list[int], warehouse_id: int | None, days: int) -> dict:
    days = max(1, min(int(days), 90))
    scope = _scope(whs, warehouse_id)
    today = date.today()
    df = today - timedelta(days=days - 1)
    one = lambda sql, p: conn.execute(sql, p).fetchone()
    base = {"whs": scope}
    stock = one("""SELECT COALESCE(SUM(b.qty_on_hand),0)::bigint AS total_qty,
                          COUNT(*) FILTER (WHERE b.qty_on_hand > 0) AS sku_in_stock
                   FROM stock_balances b JOIN warehouses w USING (warehouse_id)
                   WHERE b.warehouse_id = ANY(%(whs)s) AND w.warehouse_type='PHYSICAL'""", base)
    low = conn.execute(
        """SELECT w.warehouse_code, p.product_code, p.name AS product_name, COALESCE(b.qty_on_hand,0) AS qty, p.reorder_level
           FROM products p CROSS JOIN warehouses w LEFT JOIN stock_balances b ON b.product_id=p.product_id AND b.warehouse_id=w.warehouse_id
           WHERE p.is_active AND p.reorder_level > 0 AND w.warehouse_type='PHYSICAL' AND w.warehouse_id = ANY(%(whs)s)
             AND COALESCE(b.qty_on_hand,0) <= p.reorder_level
           ORDER BY (COALESCE(b.qty_on_hand,0)::numeric / p.reorder_level), p.product_code""", base).fetchall()
    sales = conn.execute(
        f"""WITH n AS ({NET_SALES_SQL})
            SELECT txn_date, (SUM(sold) - SUM(returned))::bigint AS net_sold FROM n GROUP BY txn_date""", {**base, "df": df, "dto": today}).fetchall()
    sales_by_day = {r["txn_date"]: int(r["net_sold"]) for r in sales}
    flow = conn.execute(
        """SELECT txn_date, SUM(qty_in)::bigint AS qin, SUM(qty_out)::bigint AS qout FROM stock_transactions
           WHERE warehouse_id = ANY(%(whs)s) AND txn_date BETWEEN %(df)s AND %(dto)s
             AND txn_type IN ('RECEIVING','CUSTOMER_RETURN','SUPPLIER_RETURN','WITHDRAWAL','SALE','ADJUST_IN','ADJUST_OUT')
           GROUP BY txn_date""", {**base, "df": df, "dto": today}).fetchall()
    flow_by_day = {r["txn_date"]: r for r in flow}
    series = []
    for i in range(days):
        d = df + timedelta(days=i)
        f = flow_by_day.get(d)
        series.append({"date": d.isoformat(), "sold": sales_by_day.get(d, 0), "in": int(f["qin"]) if f else 0, "out": int(f["qout"]) if f else 0})
    top = conn.execute(
        f"""WITH n AS ({NET_SALES_SQL})
            SELECT p.product_code, p.name AS product_name, (SUM(n.sold) - SUM(n.returned))::bigint AS sold
            FROM n JOIN products p USING (product_id) GROUP BY p.product_code, p.name
            HAVING SUM(n.sold) - SUM(n.returned) > 0 ORDER BY sold DESC, p.product_code LIMIT 10""", {**base, "df": df, "dto": today}).fetchall()
    by_wh = conn.execute(
        """SELECT w.warehouse_id, w.warehouse_code, w.name, COALESCE(SUM(b.qty_on_hand),0)::bigint AS qty,
                  COUNT(*) FILTER (WHERE b.qty_on_hand > 0) AS skus
           FROM warehouses w LEFT JOIN stock_balances b USING (warehouse_id)
           WHERE w.warehouse_type='PHYSICAL' AND w.warehouse_id = ANY(%(whs)s) GROUP BY w.warehouse_id, w.warehouse_code, w.name
           ORDER BY w.warehouse_code""", base).fetchall()
    today_row = one(
        f"""WITH n AS ({NET_SALES_SQL}) SELECT COALESCE(SUM(sold)-SUM(returned),0)::bigint AS qty, COUNT(DISTINCT document_id) FILTER (WHERE sold>0 AND doc_status='POSTED') AS docs FROM n""",
        {**base, "df": today, "dto": today})
    pending = one(
        """SELECT COUNT(*) FILTER (WHERE status IN ('IN_TRANSIT','PARTIAL')) AS in_transit, COUNT(*) FILTER (WHERE status='DRAFT') AS draft
           FROM stock_transfers WHERE from_warehouse_id = ANY(%(whs)s) OR to_warehouse_id = ANY(%(whs)s)""", base)
    draft_docs = one("SELECT COUNT(*) AS n FROM stock_documents WHERE status='DRAFT' AND warehouse_id = ANY(%(whs)s)", base)["n"]
    dep = one("""SELECT COUNT(DISTINCT d.deposit_id) AS n,
                        COALESCE(SUM(di.qty_base - di.qty_delivered_base - di.qty_cancelled_base),0)::bigint AS qty,
                        COUNT(DISTINCT d.deposit_id) FILTER (WHERE d.due_date < CURRENT_DATE) AS overdue
                 FROM customer_deposits d JOIN customer_deposit_items di USING (deposit_id)
                 WHERE d.status IN ('OPEN','PARTIAL') AND d.warehouse_id = ANY(%(whs)s)""", base)
    return {
        "days": days, "date_from": df.isoformat(), "date_to": today.isoformat(),
        "kpi": {"total_qty": int(stock["total_qty"]), "sku_in_stock": stock["sku_in_stock"], "low_stock": len(low),
                "out_of_stock": sum(1 for r in low if r["qty"] <= 0), "sold_today": int(today_row["qty"]), "sale_docs_today": today_row["docs"],
                "sold_period": sum(s["sold"] for s in series), "transfers_in_transit": pending["in_transit"],
                "transfers_draft": pending["draft"], "draft_documents": draft_docs,
                "deposits_open": dep["n"], "deposit_qty": int(dep["qty"]), "deposits_overdue": dep["overdue"]},
        "series": series, "top_products": top, "by_warehouse": by_wh, "low_stock_items": low[:10],
    }


# ------------------------------------------------------------------ integrity check


def integrity_check(conn) -> dict:
    """Re-derive every balance from the ledger and look for anything inconsistent. Read-only."""
    checks: list[dict] = []

    def add(name: str, rows: list[dict]):
        checks.append({"name": name, "ok": not rows, "count": len(rows), "sample": rows[:5]})

    add("balance_equals_ledger", conn.execute(
        """SELECT b.warehouse_id, b.product_id, b.qty_on_hand, COALESCE(sum(t.qty_in - t.qty_out),0) AS ledger
           FROM stock_balances b LEFT JOIN stock_transactions t USING (warehouse_id, product_id)
           GROUP BY b.warehouse_id, b.product_id, b.qty_on_hand HAVING b.qty_on_hand <> COALESCE(sum(t.qty_in - t.qty_out),0)""").fetchall())
    add("ledger_without_balance_row", conn.execute(
        """SELECT t.warehouse_id, t.product_id FROM stock_transactions t LEFT JOIN stock_balances b USING (warehouse_id, product_id)
           WHERE b.product_id IS NULL GROUP BY t.warehouse_id, t.product_id""").fetchall())
    add("negative_physical_stock", conn.execute(
        """SELECT b.warehouse_id, b.product_id, b.qty_on_hand FROM stock_balances b JOIN warehouses w USING (warehouse_id)
           WHERE b.qty_on_hand < 0""").fetchall())
    last = conn.execute(
        """SELECT DISTINCT ON (t.warehouse_id, t.product_id) t.warehouse_id, t.product_id, t.balance_after, b.qty_on_hand
           FROM stock_transactions t JOIN stock_balances b USING (warehouse_id, product_id)
           ORDER BY t.warehouse_id, t.product_id, t.txn_id DESC""").fetchall()
    add("running_balance_matches", [r for r in last if r["balance_after"] != r["qty_on_hand"]])
    add("posted_items_without_ledger", conn.execute(
        """SELECT d.doc_no, i.item_id FROM stock_documents d JOIN stock_document_items i USING (document_id)
           WHERE d.status IN ('POSTED','REVERSED') AND d.reverses_document_id IS NULL AND NOT EXISTS (SELECT 1 FROM stock_transactions t WHERE t.document_item_id=i.item_id AND t.reverses_txn_id IS NULL)""").fetchall())
    add("draft_documents_with_ledger", conn.execute(
        """SELECT d.doc_no FROM stock_documents d WHERE d.status='DRAFT' AND EXISTS (SELECT 1 FROM stock_transactions t WHERE t.document_id=d.document_id)""").fetchall())
    add("transit_equals_open_transfers", conn.execute(
        """WITH open AS (SELECT i.product_id, SUM(i.qty_sent_base - i.qty_received_base - i.qty_cancelled_base) AS q
                         FROM stock_transfer_items i JOIN stock_transfers t USING (transfer_id)
                         WHERE t.status IN ('IN_TRANSIT','PARTIAL') GROUP BY i.product_id),
                tr AS (SELECT b.product_id, SUM(b.qty_on_hand) AS q FROM stock_balances b JOIN warehouses w USING (warehouse_id)
                       WHERE w.warehouse_type='TRANSIT' GROUP BY b.product_id)
           SELECT COALESCE(open.product_id, tr.product_id) AS product_id, COALESCE(open.q,0) AS open_qty, COALESCE(tr.q,0) AS transit_qty
           FROM open FULL JOIN tr USING (product_id) WHERE COALESCE(open.q,0) <> COALESCE(tr.q,0)""").fetchall())
    add("closed_transfers_fully_settled", conn.execute(
        """SELECT t.transfer_no FROM stock_transfers t JOIN stock_transfer_items i USING (transfer_id)
           WHERE t.status IN ('COMPLETED','CANCELLED') GROUP BY t.transfer_no
           HAVING SUM(i.qty_sent_base - i.qty_received_base - i.qty_cancelled_base) <> 0""").fetchall())
    add("deposits_within_sold_qty", conn.execute(
        """SELECT i.item_id, i.base_qty, SUM(di.qty_base - di.qty_cancelled_base) AS deposited
           FROM customer_deposit_items di JOIN stock_document_items i ON i.item_id = di.sale_item_id
           GROUP BY i.item_id, i.base_qty HAVING SUM(di.qty_base - di.qty_cancelled_base) > i.base_qty""").fetchall())
    add("deposits_only_on_posted_sales", conn.execute(
        """SELECT d.deposit_no, s.doc_no, s.status FROM customer_deposits d JOIN stock_documents s ON s.document_id = d.sale_document_id
           WHERE d.status IN ('OPEN','PARTIAL') AND (s.status <> 'POSTED' OR s.doc_type <> 'SALE')""").fetchall())
    add("deposit_deliveries_match", conn.execute(
        """SELECT di.deposit_item_id, di.qty_delivered_base, COALESCE(SUM(x.qty_base),0) AS delivered
           FROM customer_deposit_items di LEFT JOIN deposit_delivery_items x USING (deposit_item_id)
           GROUP BY di.deposit_item_id, di.qty_delivered_base HAVING di.qty_delivered_base <> COALESCE(SUM(x.qty_base),0)""").fetchall())
    add("deposit_status_consistent", conn.execute(
        """SELECT d.deposit_no, d.status FROM customer_deposits d JOIN customer_deposit_items di USING (deposit_id)
           GROUP BY d.deposit_id, d.deposit_no, d.status
           HAVING (d.status IN ('OPEN','PARTIAL')) <> (SUM(di.qty_base - di.qty_delivered_base - di.qty_cancelled_base) > 0)""").fetchall())
    add("reversed_once_only", conn.execute(
        """SELECT reverses_txn_id FROM stock_transactions WHERE reverses_txn_id IS NOT NULL GROUP BY reverses_txn_id HAVING count(*) > 1""").fetchall())
    return {"ok": all(c["ok"] for c in checks), "checked_at": datetime.now().isoformat(timespec="seconds"), "checks": checks}

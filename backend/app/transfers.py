"""Stock transfers between warehouses.

INSTANT   : source OUT + destination IN in one transaction  -> COMPLETED.
APPROVAL  : DRAFT -> (approve/ship) IN_TRANSIT: source OUT + TRANSIT IN
            -> receive (partial allowed) PARTIAL/COMPLETED: TRANSIT OUT + destination IN
            -> cancel: remaining qty goes back TRANSIT OUT + source IN.
Every operation locks the transfer row first, then balance rows in ascending (warehouse_id, product_id) order,
and runs in one transaction, so nothing is half-applied and nothing is applied twice.
"""
from __future__ import annotations

import secrets
from datetime import date

import psycopg

from .engine import (DocumentStateError, NotFound, ValidationFailed, _lock_balances, _physical_warehouse,
                     _shortage_error, _write_balances, audit, resolve_product, resolve_unit_and_factor)


def _move(cur, wh: int, deltas: dict[int, int], txn_in: str, txn_out: str, txn_date, tid: int, user_id: int,
          shortage_msg: str = "สต็อกไม่พอสำหรับการโอน") -> None:
    """Apply signed deltas {product_id: +in/-out} to one warehouse and write the ledger rows."""
    if not deltas:
        return
    bal = _lock_balances(cur, wh, list(deltas))
    sim = {p: bal[p] + d for p, d in deltas.items()}
    short = [(p, bal[p], -d) for p, d in deltas.items() if sim[p] < 0]
    if short:
        raise _shortage_error(cur, short, shortage_msg)
    for pid in sorted(deltas):
        d = deltas[pid]
        cur.execute(
            """INSERT INTO stock_transactions (txn_date, warehouse_id, product_id, txn_type, qty_in, qty_out,
                                               balance_after, transfer_id, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (txn_date, wh, pid, txn_in if d > 0 else txn_out, max(d, 0), max(-d, 0), sim[pid], tid, user_id))
    _write_balances(cur, wh, sim)


def _apply_ops(cur, ops: list[tuple[int, dict[int, int], str, str]], txn_date, tid, user_id) -> None:
    """ops = [(warehouse_id, deltas, txn_in, txn_out)]; executed in ascending warehouse_id (deadlock-free)."""
    for wh, deltas, tin, tout in sorted(ops, key=lambda o: o[0]):
        _move(cur, wh, deltas, tin, tout, txn_date, tid, user_id)


def _transit_id(cur) -> int:
    cur.execute("SELECT warehouse_id FROM warehouses WHERE warehouse_type='TRANSIT' AND is_active ORDER BY warehouse_id LIMIT 1")
    r = cur.fetchone()
    if not r:
        raise ValidationFailed("ไม่พบคลังระหว่างทาง (TRANSIT)", code_hint="NO_TRANSIT_WAREHOUSE")
    return r["warehouse_id"]


def _load(cur, tid: int) -> dict:
    cur.execute("SELECT * FROM stock_transfers WHERE transfer_id=%s FOR UPDATE", (tid,))
    t = cur.fetchone()
    if not t:
        raise NotFound("ไม่พบใบโอน")
    return t


def _items(cur, tid: int) -> list[dict]:
    cur.execute("SELECT * FROM stock_transfer_items WHERE transfer_id=%s ORDER BY item_id", (tid,))
    return cur.fetchall()


def _state_error(t: dict, what: str) -> DocumentStateError:
    return DocumentStateError(f"ใบโอน {t['transfer_no']} อยู่ในสถานะ {t['status']} ไม่สามารถ{what}ได้", status=t["status"])


def create_transfer(conn: psycopg.Connection, data: dict, user_id: int) -> int:
    mode = data.get("mode", "APPROVAL")
    if mode not in ("INSTANT", "APPROVAL"):
        raise ValidationFailed("รูปแบบการโอนไม่ถูกต้อง", field="mode", code_hint="BAD_TRANSFER_MODE")
    src, dst = data["from_warehouse_id"], data["to_warehouse_id"]
    if src == dst:
        raise ValidationFailed("คลังต้นทางและปลายทางต้องไม่เหมือนกัน", code_hint="SAME_WAREHOUSE")
    if not data.get("items"):
        raise ValidationFailed("ใบโอนไม่มีรายการ", code_hint="NO_ITEMS")
    tdate = data.get("transfer_date") or date.today()
    with conn.transaction():
        cur = conn.cursor()
        _physical_warehouse(cur, src)
        _physical_warehouse(cur, dst)
        no = (data.get("transfer_no") or "").strip() or f"TF-{tdate:%Y%m%d}-{secrets.token_hex(4).upper()}"
        cur.execute("SELECT 1 FROM stock_transfers WHERE transfer_no=%s", (no,))
        if cur.fetchone():
            raise ValidationFailed(f"เลขที่ใบโอน {no} ถูกใช้แล้ว", code_hint="DUPLICATE_DOCUMENT")
        transit = _transit_id(cur) if mode == "APPROVAL" else None
        cur.execute(
            """INSERT INTO stock_transfers (transfer_no, transfer_mode, from_warehouse_id, to_warehouse_id,
                      transit_warehouse_id, transfer_date, remark, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING transfer_id""",
            (no, mode, src, dst, transit, tdate, data.get("remark"), user_id))
        tid = cur.fetchone()["transfer_id"]
        seen = set()
        for it in data["items"]:
            p = resolve_product(cur, it)
            if p["product_id"] in seen:
                raise ValidationFailed(f"สินค้า {p['product_code']} ซ้ำในใบโอน", code_hint="DUP_PRODUCT")
            seen.add(p["product_id"])
            qty = it.get("qty")
            if qty is None or qty <= 0 or qty != int(qty):
                raise ValidationFailed("จำนวนต้องเป็นจำนวนเต็มมากกว่า 0", field="qty", code_hint="BAD_QTY")
            unit_id, factor = resolve_unit_and_factor(cur, p, it)
            cur.execute(
                """INSERT INTO stock_transfer_items (transfer_id, product_id, unit_id, conversion_factor, qty_sent_base)
                   VALUES (%s,%s,%s,%s,%s)""", (tid, p["product_id"], unit_id, factor, int(qty) * factor))
        audit(cur, user_id, "TRANSFER_CREATE", "stock_transfer", tid, src, new={"no": no, "mode": mode, "to": dst})
        if mode == "INSTANT":
            _instant(cur, tid, user_id)
        return tid


def _instant(cur, tid: int, user_id: int) -> None:
    t = _load(cur, tid)
    items = _items(cur, tid)
    out = {i["product_id"]: -i["qty_sent_base"] for i in items}
    inn = {i["product_id"]: i["qty_sent_base"] for i in items}
    _apply_ops(cur, [(t["from_warehouse_id"], out, "TRANSFER_IN", "TRANSFER_OUT"),
                     (t["to_warehouse_id"], inn, "TRANSFER_IN", "TRANSFER_OUT")], t["transfer_date"], tid, user_id)
    cur.execute("UPDATE stock_transfer_items SET qty_received_base=qty_sent_base WHERE transfer_id=%s", (tid,))
    cur.execute("UPDATE stock_transfers SET status='COMPLETED', shipped_at=now(), completed_at=now() WHERE transfer_id=%s", (tid,))
    audit(cur, user_id, "TRANSFER_INSTANT", "stock_transfer", tid, t["from_warehouse_id"], new={"status": "COMPLETED"})


def ship_transfer(conn: psycopg.Connection, tid: int, user_id: int) -> int:
    """Approve + ship: DRAFT -> IN_TRANSIT (source OUT, TRANSIT IN)."""
    with conn.transaction():
        cur = conn.cursor()
        t = _load(cur, tid)
        if t["status"] != "DRAFT":
            raise _state_error(t, "อนุมัติ")
        _physical_warehouse(cur, t["from_warehouse_id"])
        items = _items(cur, tid)
        out = {i["product_id"]: -i["qty_sent_base"] for i in items}
        inn = {i["product_id"]: i["qty_sent_base"] for i in items}
        _apply_ops(cur, [(t["from_warehouse_id"], out, "TRANSFER_IN", "TRANSFER_OUT"),
                         (t["transit_warehouse_id"], inn, "TRANSIT_IN", "TRANSIT_OUT")], t["transfer_date"], tid, user_id)
        cur.execute("UPDATE stock_transfers SET status='IN_TRANSIT', shipped_at=now() WHERE transfer_id=%s", (tid,))
        audit(cur, user_id, "TRANSFER_SHIP", "stock_transfer", tid, t["from_warehouse_id"],
              old={"status": "DRAFT"}, new={"status": "IN_TRANSIT"})
        return tid


def receive_transfer(conn: psycopg.Connection, tid: int, user_id: int, lines: list[dict] | None = None) -> int:
    """Receive at destination. lines=None -> everything still outstanding. Partial receipts allowed."""
    with conn.transaction():
        cur = conn.cursor()
        t = _load(cur, tid)
        if t["status"] not in ("IN_TRANSIT", "PARTIAL"):
            raise _state_error(t, "รับสินค้า")
        _physical_warehouse(cur, t["to_warehouse_id"])
        items = {i["product_id"]: i for i in _items(cur, tid)}
        remaining = {p: i["qty_sent_base"] - i["qty_received_base"] - i["qty_cancelled_base"] for p, i in items.items()}
        take: dict[int, int] = {}
        if lines is None:
            take = {p: r for p, r in remaining.items() if r > 0}
        else:
            for ln in lines:
                p = resolve_product(cur, ln)
                pid = p["product_id"]
                if pid not in items:
                    raise ValidationFailed(f"สินค้า {p['product_code']} ไม่อยู่ในใบโอนนี้", code_hint="NOT_IN_TRANSFER")
                if pid in take:
                    raise ValidationFailed(f"สินค้า {p['product_code']} ซ้ำ", code_hint="DUP_PRODUCT")
                q = ln.get("qty")
                if q is None or q <= 0 or q != int(q):
                    raise ValidationFailed("จำนวนต้องเป็นจำนวนเต็มมากกว่า 0", field="qty", code_hint="BAD_QTY")
                if not ln.get("unit_code") and not ln.get("unit_id"):
                    factor = items[pid]["conversion_factor"]          # default: the unit used on the transfer
                else:
                    _, factor = resolve_unit_and_factor(cur, p, ln)
                take[pid] = int(q) * factor
        if not take:
            raise ValidationFailed("ไม่มีรายการให้รับ", code_hint="NOTHING_TO_RECEIVE")
        for pid, q in take.items():
            if q > remaining[pid]:
                cur.execute("SELECT product_code FROM products WHERE product_id=%s", (pid,))
                raise ValidationFailed(
                    f"รับเกินจำนวนที่ค้างอยู่ของสินค้า {cur.fetchone()['product_code']} (ค้างรับ {remaining[pid]} PC)",
                    code_hint="OVER_RECEIVE", remaining=remaining[pid], product_id=pid)
        _apply_ops(cur, [(t["transit_warehouse_id"], {p: -q for p, q in take.items()}, "TRANSIT_IN", "TRANSIT_OUT"),
                         (t["to_warehouse_id"], dict(take), "TRANSFER_IN", "TRANSFER_OUT")],
                   date.today(), tid, user_id)
        for pid, q in take.items():
            cur.execute("UPDATE stock_transfer_items SET qty_received_base = qty_received_base + %s "
                        "WHERE transfer_id=%s AND product_id=%s", (q, tid, pid))
        left = sum(remaining.values()) - sum(take.values())
        cur.execute("UPDATE stock_transfers SET status=%s, completed_at=CASE WHEN %s THEN now() ELSE NULL END WHERE transfer_id=%s",
                    ("PARTIAL" if left > 0 else "COMPLETED", left == 0, tid))
        audit(cur, user_id, "TRANSFER_RECEIVE", "stock_transfer", tid, t["to_warehouse_id"],
              new={"received_base": {str(k): v for k, v in take.items()}, "left": left})
        return tid


def cancel_transfer(conn: psycopg.Connection, tid: int, user_id: int, reason: str | None = None) -> int:
    """DRAFT: just cancel. IN_TRANSIT/PARTIAL: outstanding qty returns to the source warehouse."""
    with conn.transaction():
        cur = conn.cursor()
        t = _load(cur, tid)
        if t["status"] not in ("DRAFT", "IN_TRANSIT", "PARTIAL"):
            raise _state_error(t, "ยกเลิก")
        returned = {}
        if t["status"] != "DRAFT":
            items = _items(cur, tid)
            returned = {i["product_id"]: i["qty_sent_base"] - i["qty_received_base"] - i["qty_cancelled_base"] for i in items}
            returned = {p: q for p, q in returned.items() if q > 0}
            _apply_ops(cur, [(t["transit_warehouse_id"], {p: -q for p, q in returned.items()}, "TRANSIT_IN", "TRANSIT_OUT"),
                             (t["from_warehouse_id"], dict(returned), "TRANSFER_IN", "TRANSFER_OUT")],
                       date.today(), tid, user_id)
            for pid, q in returned.items():
                cur.execute("UPDATE stock_transfer_items SET qty_cancelled_base = qty_cancelled_base + %s "
                            "WHERE transfer_id=%s AND product_id=%s", (q, tid, pid))
        cur.execute("SELECT COALESCE(sum(qty_received_base),0) AS r FROM stock_transfer_items WHERE transfer_id=%s", (tid,))
        any_received = cur.fetchone()["r"] > 0
        final = "COMPLETED" if any_received else "CANCELLED"     # partly received then remainder returned = done
        cur.execute("""UPDATE stock_transfers SET status=%s, cancelled_by=%s, cancelled_at=now(),
                       completed_at=CASE WHEN %s='COMPLETED' THEN now() ELSE completed_at END,
                       remark = COALESCE(remark || E'\n', '') || %s WHERE transfer_id=%s""",
                    (final, user_id, final, f"[ยกเลิก] {reason}" if reason else "[ยกเลิก]", tid))
        audit(cur, user_id, "TRANSFER_CANCEL", "stock_transfer", tid, t["from_warehouse_id"],
              old={"status": t["status"]}, new={"status": final, "returned_base": {str(k): v for k, v in returned.items()}})
        return tid

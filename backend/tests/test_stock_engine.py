"""Stock Engine tests — run against a real PostgreSQL through the real HTTP API."""
import os
import threading

import psycopg
import pytest
from psycopg.rows import dict_row

from app import engine


def q(sql, params=None):
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row) as c:
        return c.execute(sql, params).fetchall()


def assert_ledger_consistent():
    """Invariant: balance == sum(in) - sum(out) for every warehouse/product, never negative,
    and the last balance_after in the ledger equals the balance."""
    bad = q("""SELECT b.warehouse_id, b.product_id, b.qty_on_hand,
                      COALESCE(sum(t.qty_in - t.qty_out), 0) AS ledger
               FROM stock_balances b LEFT JOIN stock_transactions t USING (warehouse_id, product_id)
               GROUP BY b.warehouse_id, b.product_id, b.qty_on_hand
               HAVING b.qty_on_hand <> COALESCE(sum(t.qty_in - t.qty_out), 0) OR b.qty_on_hand < 0""")
    assert not bad, bad
    bad = q("""SELECT DISTINCT ON (warehouse_id, product_id) warehouse_id, product_id, balance_after
               FROM stock_transactions ORDER BY warehouse_id, product_id, txn_id DESC""")
    for r in bad:
        bal = q("SELECT qty_on_hand FROM stock_balances WHERE warehouse_id=%s AND product_id=%s",
                (r["warehouse_id"], r["product_id"]))[0]["qty_on_hand"]
        assert bal == r["balance_after"]


# ------------------------------------------------------------------ units & directions


def test_unit_conversion_and_snapshot(client, w):
    wh, pid = w.warehouse(), w.product()
    doc_id = w.receive(wh, pid, 2, "CT")               # 2 CT x 24
    assert w.balance(wh, pid) == 48
    item = client.get(f"/api/documents/{doc_id}").json()["items"][0]
    assert (item["conversion_factor"], item["base_qty"], item["unit_code"]) == (24, 48, "CT")

    # change the rate afterwards: the old document keeps its snapshot, new documents use the new rate
    client.put(f"/api/products/{pid}", json={"product_code": "P1", "name": "Product",
               "conversions": [{"unit_code": "CT", "factor_to_base": 12}, {"unit_code": "PK", "factor_to_base": 6}]})
    item = client.get(f"/api/documents/{doc_id}").json()["items"][0]
    assert (item["conversion_factor"], item["base_qty"]) == (24, 48)
    w.receive(wh, pid, 1, "CT")
    assert w.balance(wh, pid) == 60

    r = w.doc("SALE", wh, [{"product_id": pid, "unit_code": "PK", "qty": 2}])  # 2 PK = 12 PC
    assert r.status_code == 201
    assert w.balance(wh, pid) == 48
    assert_ledger_consistent()


def test_all_document_directions(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    w.receive(wh, pid, 100)
    for dtype, delta in [("SALE", -10), ("WITHDRAWAL", -5), ("CUSTOMER_RETURN", +3), ("SUPPLIER_RETURN", -7)]:
        before = w.balance(wh, pid)
        r = w.doc(dtype, wh, [{"product_id": pid, "qty": abs(delta)}])
        assert r.status_code == 201, r.text
        assert w.balance(wh, pid) == before + delta, dtype
    assert w.balance(wh, pid) == 81
    assert_ledger_consistent()


def test_barcode_and_code_matching(client, w):
    wh = w.warehouse()
    pid = w.product(code="ABC", barcode="885000111")
    r = w.doc("RECEIVING", wh, [{"product_code": "885000111", "qty": 5}])
    assert r.status_code == 201, r.text
    r = w.doc("RECEIVING", wh, [{"product_code": "ABC", "qty": 5}])
    assert r.status_code == 201
    assert w.balance(wh, pid) == 10


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize("item,status", [
    ({"product_id": 1, "qty": 0}, 422),
    ({"product_id": 1, "qty": -3}, 422),
    ({"product_id": 1, "qty": 0.5}, 422),                  # half a piece
    ({"product_id": 1, "unit_code": "XX", "qty": 1}, 422),  # unknown unit
    ({"product_id": 999, "qty": 1}, 422),                   # unknown product
    ({"product_code": "NOPE", "qty": 1}, 422),
])
def test_invalid_lines_rejected(client, w, item, status):
    wh = w.warehouse()
    w.product(conversions=[])
    r = w.doc("RECEIVING", wh, [item])
    assert r.status_code == status, r.text
    assert q("SELECT count(*) AS n FROM stock_documents")[0]["n"] == 0   # nothing half-created


def test_unit_without_conversion_rejected(client, w):
    wh = w.warehouse()
    w.product(conversions=[])
    r = w.doc("RECEIVING", wh, [{"product_id": 1, "unit_code": "CT", "qty": 1}])
    assert r.status_code == 422


def test_fractional_unit_that_converts_to_whole_pieces_is_ok(client, w):
    wh, pid = w.warehouse(), w.product()
    r = w.doc("RECEIVING", wh, [{"product_id": pid, "unit_code": "CT", "qty": 0.5}])  # 12 PC
    assert r.status_code == 201
    assert w.balance(wh, pid) == 12


def test_empty_document_and_bad_type_rejected(client, w):
    wh = w.warehouse()
    assert w.doc("SALE", wh, []).status_code == 422
    assert w.doc("TRANSFER", wh, [{"product_id": 1, "qty": 1}]).status_code == 422
    assert w.doc("ADJUSTMENT", wh, [{"product_id": 1, "qty": 1}]).status_code == 422


def test_duplicate_doc_no_rejected_and_nothing_posted_twice(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    assert w.doc("RECEIVING", wh, [{"product_id": pid, "qty": 5}], doc_no="IN-1").status_code == 201
    r = w.doc("RECEIVING", wh, [{"product_id": pid, "qty": 5}], doc_no="IN-1")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DUPLICATE_DOCUMENT"
    assert w.balance(wh, pid) == 5
    # the same number is fine in another warehouse
    wh2 = w.warehouse("WH2", "Second")
    assert w.doc("RECEIVING", wh2, [{"product_id": pid, "qty": 1}], doc_no="IN-1").status_code == 201


# ------------------------------------------------------------------ no negative stock


def test_sale_over_stock_is_rejected_atomically(client, w):
    wh = w.warehouse()
    a, b = w.product("A", conversions=[]), w.product("B", "Bee", conversions=[])
    w.receive(wh, a, 10)
    w.receive(wh, b, 2)
    # line 1 is fine, line 2 is short -> nothing may be deducted
    r = w.doc("SALE", wh, [{"product_id": a, "qty": 4}, {"product_id": b, "qty": 3}])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "INSUFFICIENT_STOCK"
    assert err["items"][0]["product_code"] == "B" and err["items"][0]["available"] == 2 and err["items"][0]["required"] == 3
    assert w.balance(wh, a) == 10 and w.balance(wh, b) == 2
    assert q("SELECT count(*) AS n FROM stock_documents WHERE doc_type='SALE'")[0]["n"] == 0
    assert_ledger_consistent()


def test_same_product_on_two_lines_is_summed_for_the_stock_check(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    w.receive(wh, pid, 10)
    r = w.doc("SALE", wh, [{"product_id": pid, "qty": 6}, {"product_id": pid, "qty": 6}])
    assert r.status_code == 409
    assert w.balance(wh, pid) == 10


def test_exact_stock_can_be_sold_down_to_zero(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    w.receive(wh, pid, 7)
    assert w.doc("SALE", wh, [{"product_id": pid, "qty": 7}]).status_code == 201
    assert w.balance(wh, pid) == 0
    assert w.doc("SALE", wh, [{"product_id": pid, "qty": 1}]).status_code == 409


def test_stock_is_per_warehouse(client, w):
    wh1, wh2, pid = w.warehouse(), w.warehouse("WH2", "Second"), w.product(conversions=[])
    w.receive(wh1, pid, 10)
    assert w.doc("SALE", wh2, [{"product_id": pid, "qty": 1}]).status_code == 409   # wh2 has none
    w.receive(wh2, pid, 4)
    total = client.get("/api/stock/balances", params={"view": "total"}).json()
    assert total[0]["qty_on_hand"] == 14
    assert {x["warehouse_code"]: x["qty_on_hand"] for x in total[0]["by_warehouse"]} == {"WH1": 10, "WH2": 4}


# ------------------------------------------------------------------ draft / post / double posting


def test_draft_does_not_touch_stock_and_can_be_deleted(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    r = w.doc("RECEIVING", wh, [{"product_id": pid, "qty": 5}], post=False)
    did = r.json()["document_id"]
    assert r.json()["document"]["status"] == "DRAFT"
    assert w.balance(wh, pid) == 0
    assert client.delete(f"/api/documents/{did}").status_code == 200
    assert client.get(f"/api/documents/{did}").status_code == 404


def test_posted_document_cannot_be_deleted_or_posted_again(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    did = w.doc("RECEIVING", wh, [{"product_id": pid, "qty": 5}], post=False).json()["document_id"]
    assert client.post(f"/api/documents/{did}/post").status_code == 200
    assert w.balance(wh, pid) == 5
    r = client.post(f"/api/documents/{did}/post")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENT_STATE"
    assert client.delete(f"/api/documents/{did}").status_code == 409
    assert w.balance(wh, pid) == 5
    assert_ledger_consistent()


def test_idempotency_key_replays_instead_of_double_posting(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    w.receive(wh, pid, 20)
    items = [{"product_id": pid, "qty": 5}]
    r1 = w.doc("SALE", wh, items, key="k-1")
    r2 = w.doc("SALE", wh, items, key="k-1")
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["document_id"] == r2.json()["document_id"]
    assert r2.json()["replayed"] is True and r1.json()["replayed"] is False
    assert w.balance(wh, pid) == 15                                   # deducted once
    r3 = w.doc("SALE", wh, [{"product_id": pid, "qty": 6}], key="k-1")  # same key, different payload
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert w.balance(wh, pid) == 15


def test_failed_request_does_not_burn_idempotency_key(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    items = [{"product_id": pid, "qty": 5}]
    assert w.doc("SALE", wh, items, key="k-2").status_code == 409     # no stock
    w.receive(wh, pid, 10)
    assert w.doc("SALE", wh, items, key="k-2").status_code == 201     # same key may be retried
    assert w.balance(wh, pid) == 5


# ------------------------------------------------------------------ reversal


def test_reverse_sale_restores_stock_once(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    w.receive(wh, pid, 10)
    did = w.doc("SALE", wh, [{"product_id": pid, "qty": 4}]).json()["document_id"]
    assert w.balance(wh, pid) == 6
    r = client.post(f"/api/documents/{did}/reverse", json={"reason": "wrong customer"})
    assert r.status_code == 200, r.text
    rev = r.json()["document"]
    assert rev["reverses_document_id"] == did and rev["status"] == "POSTED"
    assert client.get(f"/api/documents/{did}").json()["status"] == "REVERSED"
    assert w.balance(wh, pid) == 10
    again = client.post(f"/api/documents/{did}/reverse", json={})
    assert again.status_code == 409
    assert client.post(f"/api/documents/{rev['document_id']}/reverse", json={}).status_code == 409
    assert w.balance(wh, pid) == 10
    kinds = [x["txn_type"] for x in q("SELECT txn_type FROM stock_transactions ORDER BY txn_id")]
    assert kinds == ["RECEIVING", "SALE", "REVERSAL"]                 # history kept, nothing edited/deleted
    assert_ledger_consistent()


def test_reversing_a_receipt_that_was_already_sold_is_blocked(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    rid = w.receive(wh, pid, 10)
    w.doc("SALE", wh, [{"product_id": pid, "qty": 8}])
    r = client.post(f"/api/documents/{rid}/reverse", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert w.balance(wh, pid) == 2
    assert client.get(f"/api/documents/{rid}").json()["status"] == "POSTED"
    assert_ledger_consistent()


def test_cannot_reverse_a_draft(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    did = w.doc("RECEIVING", wh, [{"product_id": pid, "qty": 1}], post=False).json()["document_id"]
    assert client.post(f"/api/documents/{did}/reverse", json={}).status_code == 409


# ------------------------------------------------------------------ count & adjustment


def test_count_adjustment_up_down_and_no_difference(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    w.receive(wh, pid, 10)

    c1 = client.post("/api/counts", json={"warehouse_id": wh, "product_id": pid, "counted_qty": 15}).json()["count_id"]
    r = client.post(f"/api/counts/{c1}/apply", json={})
    assert r.status_code == 200 and r.json()["adjustment_id"]
    assert w.balance(wh, pid) == 15
    assert client.post(f"/api/counts/{c1}/apply", json={}).status_code == 409   # cannot apply twice

    c2 = client.post("/api/counts", json={"warehouse_id": wh, "product_id": pid, "counted_qty": 12}).json()["count_id"]
    client.post(f"/api/counts/{c2}/apply", json={})
    assert w.balance(wh, pid) == 12

    c3 = client.post("/api/counts", json={"warehouse_id": wh, "product_id": pid, "counted_qty": 12}).json()["count_id"]
    assert client.post(f"/api/counts/{c3}/apply", json={}).json()["no_difference"] is True
    assert w.balance(wh, pid) == 12

    assert client.post("/api/counts", json={"warehouse_id": wh, "product_id": pid, "counted_qty": -1}).status_code == 422
    types = [x["txn_type"] for x in q("SELECT txn_type FROM stock_transactions ORDER BY txn_id")]
    assert types == ["RECEIVING", "ADJUST_IN", "ADJUST_OUT"]
    assert_ledger_consistent()


# ------------------------------------------------------------------ movement report


def test_movement_running_balance_and_filters(client, w):
    wh1, wh2 = w.warehouse(), w.warehouse("WH2", "Second")
    pid = w.product(conversions=[])
    w.receive(wh1, pid, 10)
    w.doc("SALE", wh1, [{"product_id": pid, "qty": 3}], doc_no="S-1")
    w.doc("WITHDRAWAL", wh1, [{"product_id": pid, "qty": 2}], doc_no="W-1")
    w.receive(wh2, pid, 5)

    rows = client.get("/api/stock/movements", params={"warehouse_id": wh1, "product_id": pid}).json()
    asc = list(reversed(rows))
    assert [(r["qty_in"], r["qty_out"], r["balance_after"]) for r in asc] == [(10, 0, 10), (0, 3, 7), (0, 2, 5)]
    assert {"txn_date", "product_code", "product_name", "warehouse_name", "doc_type", "doc_no", "user_name"} <= set(rows[0])
    sales = client.get("/api/stock/movements", params={"doc_type": "SALE"}).json()
    assert [r["doc_no"] for r in sales] == ["S-1"]
    assert len(client.get("/api/stock/movements", params={"warehouse_id": wh2}).json()) == 1
    assert client.get("/api/stock/movements", params={"date_from": "2999-01-01"}).json() == []


# ------------------------------------------------------------------ permissions


def make_user(client, role, whs, username="u1"):
    r = client.post("/api/users", json={"username": username, "password": "password-1", "full_name": username,
                                        "role_code": role, "warehouses": whs})
    assert r.status_code == 201, r.text
    from fastapi.testclient import TestClient
    from app.main import app
    c2 = TestClient(app)
    tok = c2.post("/api/auth/login", json={"username": username, "password": "password-1"}).json()["token"]
    c2.headers["Authorization"] = "Bearer " + tok
    return c2


def test_warehouse_access_and_roles(client, w):
    wh1, wh2, pid = w.warehouse(), w.warehouse("WH2", "Second"), w.product(conversions=[])
    w.receive(wh1, pid, 10)
    w.receive(wh2, pid, 10)

    keeper = make_user(client, "STOREKEEPER", [{"warehouse_id": wh1, "can_view": True, "can_transact": True}], "keeper")
    item = [{"product_id": pid, "qty": 1}]
    body = {"doc_type": "SALE", "warehouse_id": wh1, "items": item, "post": True}
    assert keeper.post("/api/documents", json=body).status_code == 201
    assert keeper.post("/api/documents", json={**body, "warehouse_id": wh2}).status_code == 403   # no access to wh2
    assert [x["warehouse_id"] for x in keeper.get("/api/warehouses").json()] == [wh1]
    assert all(r["warehouse_id"] == wh1 for r in keeper.get("/api/stock/balances").json())
    assert all(r["warehouse_id"] == wh1 for r in keeper.get("/api/stock/movements").json())
    assert keeper.post("/api/users", json={}).status_code in (403, 422)

    viewer = make_user(client, "VIEWER", [{"warehouse_id": wh1, "can_view": True, "can_transact": True}], "viewer")
    assert viewer.post("/api/documents", json=body).status_code == 403
    assert viewer.get("/api/stock/balances").status_code == 200

    sales = make_user(client, "SALES", [{"warehouse_id": wh1, "can_view": True, "can_transact": True}], "sales")
    assert sales.post("/api/documents", json=body).status_code == 201
    assert sales.post("/api/documents", json={**body, "doc_type": "RECEIVING"}).status_code == 403

    view_only = make_user(client, "STOREKEEPER", [{"warehouse_id": wh1, "can_view": True, "can_transact": False}], "viewonly")
    assert view_only.post("/api/documents", json=body).status_code == 403

    did = keeper.post("/api/documents", json=body).json()["document_id"]
    assert keeper.post(f"/api/documents/{did}/reverse", json={}).status_code == 403   # reverse needs MANAGER/ADMIN


def test_unauthenticated_requests_are_rejected(client):
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app)
    assert anon.get("/api/stock/balances").status_code == 401
    assert anon.post("/api/documents", json={}).status_code == 401
    assert anon.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401


def test_cannot_post_into_transit_warehouse(client, w):
    pid = w.product(conversions=[])
    tid = q("SELECT warehouse_id FROM warehouses WHERE warehouse_type='TRANSIT'")[0]["warehouse_id"]
    assert w.doc("RECEIVING", tid, [{"product_id": pid, "qty": 1}]).status_code == 422


# ------------------------------------------------------------------ concurrency (real row locks)


def _conn():
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)


def test_concurrent_sales_never_oversell(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    w.receive(wh, pid, 10)
    admin_id = q("SELECT user_id FROM users WHERE username='admin'")[0]["user_id"]
    with _conn() as c:
        ids = [engine.create_document(c, {"doc_type": "SALE", "warehouse_id": wh,
                                          "items": [{"product_id": pid, "qty": 1}]}, admin_id) for _ in range(30)]
    results = []

    def work(did):
        with _conn() as c:
            try:
                engine.post_document(c, did, admin_id)
                results.append("ok")
            except engine.InsufficientStock:
                results.append("short")

    threads = [threading.Thread(target=work, args=(d,)) for d in ids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 10 and results.count("short") == 20
    assert w.balance(wh, pid) == 0
    assert q("SELECT count(*) AS n FROM stock_transactions WHERE txn_type='SALE'")[0]["n"] == 10
    assert_ledger_consistent()


def test_concurrent_double_post_of_same_document_deducts_once(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    w.receive(wh, pid, 100)
    admin_id = q("SELECT user_id FROM users WHERE username='admin'")[0]["user_id"]
    with _conn() as c:
        did = engine.create_document(c, {"doc_type": "SALE", "warehouse_id": wh,
                                         "items": [{"product_id": pid, "qty": 30}]}, admin_id)
    results = []

    def work():
        with _conn() as c:
            try:
                engine.post_document(c, did, admin_id)
                results.append("ok")
            except engine.DocumentStateError:
                results.append("dup")

    threads = [threading.Thread(target=work) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 1 and results.count("dup") == 11
    assert w.balance(wh, pid) == 70
    assert_ledger_consistent()


def test_concurrent_multi_product_documents_do_not_deadlock(client, w):
    wh = w.warehouse()
    pids = [w.product(f"P{i}", f"N{i}", conversions=[]) for i in range(1, 6)]
    for p in pids:
        w.receive(wh, p, 1000)
    admin_id = q("SELECT user_id FROM users WHERE username='admin'")[0]["user_id"]
    with _conn() as c:
        ids = []
        for n in range(24):
            order = pids if n % 2 == 0 else list(reversed(pids))      # opposite line order on purpose
            ids.append(engine.create_document(c, {"doc_type": "SALE", "warehouse_id": wh,
                       "items": [{"product_id": p, "qty": 1} for p in order]}, admin_id))
    errors = []

    def work(did):
        with _conn() as c:
            try:
                engine.post_document(c, did, admin_id)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

    threads = [threading.Thread(target=work, args=(d,)) for d in ids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert all(w.balance(wh, p) == 1000 - 24 for p in pids)
    assert_ledger_consistent()


def test_concurrent_reverse_and_sale_stay_consistent(client, w):
    wh, pid = w.warehouse(), w.product(conversions=[])
    rid = w.receive(wh, pid, 10)
    admin_id = q("SELECT user_id FROM users WHERE username='admin'")[0]["user_id"]
    with _conn() as c:
        sale = engine.create_document(c, {"doc_type": "SALE", "warehouse_id": wh,
                                          "items": [{"product_id": pid, "qty": 10}]}, admin_id)
    out = {}

    def do_reverse():
        with _conn() as c:
            try:
                engine.reverse_document(c, rid, admin_id)
                out["rev"] = "ok"
            except engine.StockError as e:
                out["rev"] = e.code

    def do_sale():
        with _conn() as c:
            try:
                engine.post_document(c, sale, admin_id)
                out["sale"] = "ok"
            except engine.StockError as e:
                out["sale"] = e.code

    ts = [threading.Thread(target=do_reverse), threading.Thread(target=do_sale)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    # exactly one of them can win: whichever ran second would take stock below zero
    assert sorted([out["rev"], out["sale"]]) == ["INSUFFICIENT_STOCK", "ok"], out
    assert w.balance(wh, pid) in (0,)
    assert_ledger_consistent()

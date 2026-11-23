"""Customer deposits (ฝากคลัง): sold goods kept in the warehouse until delivery."""
import os
import subprocess
import threading
from pathlib import Path

import psycopg
import pytest

from test_stock_engine import assert_ledger_consistent, make_user, q


@pytest.fixture()
def shop(client, w):
    wh = w.warehouse("HHX", "Main")
    p1 = w.product("P1", "Cup 16oz")                       # CT=24, PK=6
    p2 = w.product("P2", "Lid", conversions=[])
    w.receive(wh, p1, 100)
    w.receive(wh, p2, 50)
    cust = client.post("/api/customers", json={"code": "C001", "name": "Shop A"}).json()["id"]
    return {"wh": wh, "p1": p1, "p2": p2, "cust": cust, "w": w}


def stock_row(client, wh, pid):
    for r in client.get("/api/stock/balances", params={"warehouse_id": wh}).json():
        if r["product_id"] == pid:
            return r
    return None


def sale(s, items, **kw):
    r = s["w"].doc("SALE", s["wh"], items, customer_id=kw.pop("customer_id", s["cust"]), **kw)
    return r


def integrity_ok(client):
    r = client.get("/api/admin/integrity").json()
    assert r["ok"], [c for c in r["checks"] if not c["ok"]]


def test_deposit_created_with_sale_and_delivered_in_parts(client, shop):
    s = shop
    r = sale(s, [{"product_id": s["p1"], "unit_code": "CT", "qty": 2, "deposit_qty": 1},
                 {"product_id": s["p2"], "qty": 10}])
    assert r.status_code == 201, r.text
    doc = r.json()["document"]
    assert len(doc["deposits"]) == 1 and doc["deposits"][0]["qty_remaining_base"] == 24
    assert doc["items"][0]["deposit_qty_base"] == 24 and doc["items"][0]["deposited_base"] == 24
    # stock is deducted at sale time; the deposit is shown separately
    row = stock_row(client, s["wh"], s["p1"])
    assert (row["qty_on_hand"], row["deposit_qty"], row["physical_qty"]) == (52, 24, 76)
    dep_id = doc["deposits"][0]["deposit_id"]
    d = client.get(f"/api/deposits/{dep_id}").json()
    assert d["status"] == "OPEN" and d["customer_display"] == "Shop A" and d["sale_doc_no"] == doc["doc_no"]
    item = d["items"][0]["deposit_item_id"]

    r = client.post(f"/api/deposits/{dep_id}/deliver", json={"lines": [{"deposit_item_id": item, "qty": 1, "unit_code": "PK"}]})
    assert r.status_code == 200, r.text
    assert r.json()["deposit"]["status"] == "PARTIAL" and r.json()["deposit"]["items"][0]["qty_remaining_base"] == 18
    r = client.post(f"/api/deposits/{dep_id}/deliver", json={"lines": [{"deposit_item_id": item, "qty": 19}]})
    assert r.status_code == 422 and r.json()["error"]["code_hint"] == "DELIVERY_EXCEEDS_DEPOSIT"
    r = client.post(f"/api/deposits/{dep_id}/deliver", json={})          # everything left
    assert r.status_code == 200 and r.json()["deposit"]["status"] == "COMPLETED"
    assert len(r.json()["deposit"]["deliveries"]) == 2
    r = client.post(f"/api/deposits/{dep_id}/deliver", json={})
    assert r.status_code == 409
    row = stock_row(client, s["wh"], s["p1"])
    assert (row["qty_on_hand"], row["deposit_qty"], row["physical_qty"]) == (52, 0, 52)   # deliveries never touch stock
    assert_ledger_consistent()
    integrity_ok(client)


def test_deposit_validation(client, shop):
    s = shop
    r = sale(s, [{"product_id": s["p1"], "qty": 10, "deposit_qty": 11}])
    assert r.status_code == 422 and r.json()["error"]["code_hint"] == "DEPOSIT_EXCEEDS_SALE"
    r = sale(s, [{"product_id": s["p1"], "qty": 10, "deposit_qty": 5}], customer_id=None)
    assert r.status_code == 422 and r.json()["error"]["code_hint"] == "DEPOSIT_NEEDS_CUSTOMER"
    r = s["w"].doc("RECEIVING", s["wh"], [{"product_id": s["p1"], "qty": 10, "deposit_qty": 5}])
    assert r.status_code == 422 and r.json()["error"]["code_hint"] == "DEPOSIT_NOT_SALE"
    r = sale(s, [{"product_id": s["p1"], "unit_code": "PK", "qty": 2, "deposit_qty": "0.5"}])
    assert r.status_code == 201, r.text      # 0.5 PK = 3 PC is whole
    assert r.json()["document"]["items"][0]["deposit_qty_base"] == 3
    assert q("SELECT count(*) n FROM stock_documents WHERE doc_type='SALE'")[0]["n"] == 1   # failed ones left nothing


def test_draft_sale_creates_deposit_only_when_posted(client, shop):
    s = shop
    r = sale(s, [{"product_id": s["p1"], "qty": 10, "deposit_qty": 4}], post=False)
    did = r.json()["document_id"]
    assert q("SELECT count(*) n FROM customer_deposits")[0]["n"] == 0
    assert client.post(f"/api/documents/{did}/post").status_code == 200
    assert q("SELECT count(*) n FROM customer_deposits")[0]["n"] == 1


def test_deposit_created_later_cannot_exceed_sold(client, shop):
    s = shop
    doc = sale(s, [{"product_id": s["p1"], "unit_code": "CT", "qty": 2}]).json()["document"]
    line = doc["items"][0]["item_id"]
    r = client.post("/api/deposits", json={"sale_document_id": doc["document_id"], "due_date": "2030-01-31",
                                           "items": [{"sale_item_id": line, "qty": 30}]})
    assert r.status_code == 201, r.text
    assert r.json()["deposit"]["due_date"] == "2030-01-31"
    r = client.post("/api/deposits", json={"sale_document_id": doc["document_id"], "items": [{"sale_item_id": line, "qty": 19}]})
    assert r.status_code == 422 and r.json()["error"]["available"] == 18
    r = client.post("/api/deposits", json={"sale_document_id": doc["document_id"], "items": [{"sale_item_id": line, "qty": 18}]})
    assert r.status_code == 201
    assert stock_row(client, s["wh"], s["p1"])["deposit_qty"] == 48
    # not a sale / not posted
    rcv = q("SELECT document_id FROM stock_documents WHERE doc_type='RECEIVING' LIMIT 1")[0]["document_id"]
    r = client.post("/api/deposits", json={"sale_document_id": rcv, "items": [{"sale_item_id": 1, "qty": 1}]})
    assert r.status_code == 409
    integrity_ok(client)


def test_cancel_with_and_without_return(client, shop):
    s = shop
    d1 = sale(s, [{"product_id": s["p1"], "qty": 20, "deposit_qty": 20}]).json()["document"]["deposits"][0]["deposit_id"]
    d2 = sale(s, [{"product_id": s["p1"], "qty": 10, "deposit_qty": 10}]).json()["document"]["deposits"][0]["deposit_id"]
    assert stock_row(client, s["wh"], s["p1"])["qty_on_hand"] == 70
    item = client.get(f"/api/deposits/{d1}").json()["items"][0]["deposit_item_id"]
    client.post(f"/api/deposits/{d1}/deliver", json={"lines": [{"deposit_item_id": item, "qty": 5}]})

    sales_user = make_user(client, "SALES", [{"warehouse_id": s["wh"], "can_transact": True}], "seller")
    assert sales_user.post(f"/api/deposits/{d1}/cancel", json={"reason": "x"}).status_code == 403
    assert client.post(f"/api/deposits/{d1}/cancel", json={"reason": ""}).status_code == 422

    r = client.post(f"/api/deposits/{d1}/cancel", json={"reason": "ลูกค้าคืนของ", "return_to_stock": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "COMPLETED" and r.json()["return_document_id"]       # 5 delivered, 15 returned
    ret = client.get(f"/api/documents/{r.json()['return_document_id']}").json()
    assert ret["doc_type"] == "CUSTOMER_RETURN" and ret["status"] == "POSTED" and ret["items"][0]["base_qty"] == 15
    assert stock_row(client, s["wh"], s["p1"])["qty_on_hand"] == 85

    r = client.post(f"/api/deposits/{d2}/cancel", json={"reason": "บันทึกผิด", "return_to_stock": False})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED" and r.json()["return_document_id"] is None
    row = stock_row(client, s["wh"], s["p1"])
    assert (row["qty_on_hand"], row["deposit_qty"]) == (85, 0)
    assert client.post(f"/api/deposits/{d2}/cancel", json={"reason": "again"}).status_code == 409
    assert_ledger_consistent()
    integrity_ok(client)


def test_reversing_the_sale_closes_its_deposits(client, shop):
    s = shop
    doc = sale(s, [{"product_id": s["p1"], "qty": 30, "deposit_qty": 20}]).json()["document"]
    dep = doc["deposits"][0]["deposit_id"]
    item = client.get(f"/api/deposits/{dep}").json()["items"][0]["deposit_item_id"]
    client.post(f"/api/deposits/{dep}/deliver", json={"lines": [{"deposit_item_id": item, "qty": 5}]})
    r = client.post(f"/api/documents/{doc['document_id']}/reverse", json={"reason": "wrong bill"})
    assert r.status_code == 200, r.text
    d = client.get(f"/api/deposits/{dep}").json()
    assert d["status"] == "COMPLETED" and d["items"][0]["qty_remaining_base"] == 0 and d["cancel_reason"]
    row = stock_row(client, s["wh"], s["p1"])
    assert (row["qty_on_hand"], row["deposit_qty"]) == (100, 0)
    # a reversed sale can no longer receive deposits
    r = client.post("/api/deposits", json={"sale_document_id": doc["document_id"],
                                           "items": [{"sale_item_id": doc["items"][0]["item_id"], "qty": 1}]})
    assert r.status_code == 409
    integrity_ok(client)


def test_stock_count_includes_deposited_goods(client, shop):
    s = shop
    sale(s, [{"product_id": s["p1"], "qty": 48, "deposit_qty": 24}])      # on hand 52, deposits 24, shelf 76
    c = client.post("/api/counts", json={"warehouse_id": s["wh"], "product_id": s["p1"], "counted_qty": 76}).json()["count_id"]
    r = client.post(f"/api/counts/{c}/apply", json={})
    assert r.status_code == 200 and r.json()["no_difference"] is True
    c = client.post("/api/counts", json={"warehouse_id": s["wh"], "product_id": s["p1"], "counted_qty": 70}).json()["count_id"]
    assert q("SELECT deposit_qty FROM stock_counts WHERE count_id=%s", (c,))[0]["deposit_qty"] == 24
    r = client.post(f"/api/counts/{c}/apply", json={})
    assert r.status_code == 200 and r.json()["adjustment_id"]
    assert stock_row(client, s["wh"], s["p1"])["qty_on_hand"] == 46
    c = client.post("/api/counts", json={"warehouse_id": s["wh"], "product_id": s["p1"], "counted_qty": 20}).json()["count_id"]
    r = client.post(f"/api/counts/{c}/apply", json={})
    assert r.status_code == 422 and r.json()["error"]["code_hint"] == "COUNT_BELOW_DEPOSITS"
    assert_ledger_consistent()


def test_concurrent_deliveries_apply_once(client, shop):
    s = shop
    dep = sale(s, [{"product_id": s["p1"], "qty": 30, "deposit_qty": 30}]).json()["document"]["deposits"][0]["deposit_id"]
    codes = []

    def go():
        from fastapi.testclient import TestClient
        from app.main import app
        c = TestClient(app); c.headers.update(client.headers)
        codes.append(c.post(f"/api/deposits/{dep}/deliver", json={}).status_code)

    th = [threading.Thread(target=go) for _ in range(8)]
    [x.start() for x in th]; [x.join() for x in th]
    assert sorted(codes) == [200] + [409] * 7
    assert q("SELECT count(*) n FROM deposit_deliveries")[0]["n"] == 1
    integrity_ok(client)


def test_deliver_idempotency_and_permissions(client, shop):
    s = shop
    dep = sale(s, [{"product_id": s["p1"], "qty": 30, "deposit_qty": 30}]).json()["document"]["deposits"][0]["deposit_id"]
    item = client.get(f"/api/deposits/{dep}").json()["items"][0]["deposit_item_id"]
    body, k = {"lines": [{"deposit_item_id": item, "qty": 10}]}, {"Idempotency-Key": "dl-1"}
    r1 = client.post(f"/api/deposits/{dep}/deliver", json=body, headers=k)
    r2 = client.post(f"/api/deposits/{dep}/deliver", json=body, headers=k)
    assert r1.status_code == 200 and r2.json()["replayed"] is True and r2.json()["deposit"]["items"][0]["qty_remaining_base"] == 20

    other = s["w"].warehouse("VAN03", "Van 3")
    viewer = make_user(client, "VIEWER", [{"warehouse_id": s["wh"]}], "viewer")
    assert viewer.get(f"/api/deposits/{dep}").status_code == 200
    assert viewer.post(f"/api/deposits/{dep}/deliver", json={}).status_code == 403
    outsider = make_user(client, "STOREKEEPER", [{"warehouse_id": other, "can_transact": True}], "keeper")
    assert outsider.get(f"/api/deposits/{dep}").status_code == 403
    assert outsider.get("/api/deposits").json() == []
    keeper = make_user(client, "STOREKEEPER", [{"warehouse_id": s["wh"], "can_transact": True}], "keeper2")
    assert keeper.post(f"/api/deposits/{dep}/deliver", json={}).status_code == 200


def test_list_report_and_dashboard(client, shop):
    s = shop
    sale(s, [{"product_id": s["p1"], "qty": 30, "deposit_qty": 12}, {"product_id": s["p2"], "qty": 5, "deposit_qty": 5}])
    lst = client.get("/api/deposits", params={"q": "shop a"}).json()
    assert len(lst) == 1 and lst[0]["qty_remaining_base"] == 17 and lst[0]["line_count"] == 2
    assert client.get("/api/deposits", params={"status": "COMPLETED"}).json() == []
    rep = client.get("/api/reports/deposits").json()
    assert [r["remaining"] for r in rep["rows"]] == [12, 5] and rep["rows"][0]["customer"] == "Shop A"
    bal = {r["product_code"]: r for r in client.get("/api/reports/stock_balance").json()["rows"]}
    assert (bal["P1"]["qty"], bal["P1"]["deposit_qty"], bal["P1"]["physical"]) == (70, 12, 82)
    k = client.get("/api/dashboard").json()["kpi"]
    assert (k["deposits_open"], k["deposit_qty"]) == (1, 17)
    for fmt in ("xlsx", "pdf"):
        assert client.get("/api/reports/deposits", params={"format": fmt, "lang": "lo"}).status_code == 200
    total = client.get("/api/stock/balances", params={"view": "total"}).json()
    p1 = next(r for r in total if r["product_code"] == "P1")
    assert (p1["qty_on_hand"], p1["deposit_qty"], p1["physical_qty"]) == (70, 12, 82)


def test_migrations_upgrade_an_older_database(client):
    """An existing install (schema before deposits) is upgraded automatically on startup."""
    from app import db
    from app.migrate import run_migrations
    root = Path(__file__).resolve().parents[2]
    old = subprocess.run(["git", "show", "2afef70:db/schema.sql"], cwd=root, capture_output=True, text=True)
    if old.returncode != 0:
        pytest.skip("git history not available")
    db.close_pool()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute(old.stdout)
    applied = run_migrations()
    assert applied == ["005_security.sql", "006_deposits_billscan.sql"]
    assert run_migrations() == []
    cols = q("""SELECT column_name FROM information_schema.columns WHERE table_name='stock_document_items'""")
    assert "deposit_qty_base" in {c["column_name"] for c in cols}
    assert q("SELECT count(*) n FROM customer_deposits")[0]["n"] == 0


def test_line_split_by_unit_with_deposit_in_pieces(client, shop):
    """The form sends one item per unit (2 CT + 1 PK + 3 PC) and spreads the deposit (PC) over them."""
    s = shop
    r = sale(s, [{"product_id": s["p1"], "unit_code": "CT", "qty": 2, "deposit_qty_base": 30},
                 {"product_id": s["p1"], "unit_code": "PK", "qty": 1, "deposit_qty_base": 5},
                 {"product_id": s["p1"], "unit_code": "PC", "qty": 3}])
    assert r.status_code == 201, r.text
    doc = r.json()["document"]
    assert [i["base_qty"] for i in doc["items"]] == [48, 6, 3]
    assert doc["deposits"][0]["qty_remaining_base"] == 35
    assert stock_row(client, s["wh"], s["p1"])["qty_on_hand"] == 100 - 57
    r = sale(s, [{"product_id": s["p1"], "unit_code": "PK", "qty": 1, "deposit_qty_base": 7}])
    assert r.status_code == 422 and r.json()["error"]["code_hint"] == "DEPOSIT_EXCEEDS_SALE"
    av = client.get("/api/stock/available", params={"warehouse_id": s["wh"]}).json()
    assert av == {str(s["p1"]): 43, str(s["p2"]): 50}
    other = s["w"].warehouse("VAN03", "Van 3")
    from test_stock_engine import make_user
    u = make_user(client, "STOREKEEPER", [{"warehouse_id": other, "can_transact": True}], "keeper")
    assert u.get("/api/stock/available", params={"warehouse_id": s["wh"]}).status_code == 403

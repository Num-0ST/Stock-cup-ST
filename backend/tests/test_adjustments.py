"""Stock adjustment documents: several products at once, each line raises or lowers the stock, with a reason."""
from test_stock_engine import make_user, q


def bal(client, wh, pid):
    rows = client.get("/api/stock/balances", params={"warehouse_id": wh}).json()
    return next((int(r["qty_on_hand"]) for r in rows if r["product_id"] == pid), 0)


def test_adjust_up_and_down_with_reason_and_reverse(client, w):
    wh = w.warehouse("AJ", "Main")
    p1 = w.product("A1", "Cup")          # CT = 24
    p2 = w.product("A2", "Lid")
    w.receive(wh, p1, 100)
    w.receive(wh, p2, 10)
    r = w.doc("ADJUSTMENT", wh, [{"product_id": p1, "qty": 1, "unit_code": "CT", "direction": "IN"},
                                 {"product_id": p2, "qty": 3, "direction": "OUT"}], adjust_reason="DAMAGED", remark="แตก 3 ใบ")
    assert r.status_code == 201, r.text
    doc = r.json()["document"]
    assert doc["doc_no"].startswith("ADJ-") and doc["adjust_reason"] == "DAMAGED"
    assert [i["adj_sign"] for i in doc["items"]] == [1, -1]
    assert bal(client, wh, p1) == 124 and bal(client, wh, p2) == 7
    types = [x["txn_type"] for x in q("SELECT txn_type FROM stock_transactions WHERE document_id=%s ORDER BY txn_id", (doc["document_id"],))]
    assert types == ["ADJUST_IN", "ADJUST_OUT"]
    mv = client.get("/api/reports/movements", params={"warehouse_id": wh}).json()["rows"]
    assert {"ADJUST_IN", "ADJUST_OUT"} <= {m["type"] for m in mv}
    # undo it
    rv = client.post(f"/api/documents/{doc['document_id']}/reverse", json={"reason": "บันทึกผิด"})
    assert rv.status_code == 200, rv.text
    assert bal(client, wh, p1) == 100 and bal(client, wh, p2) == 10
    assert client.get("/api/admin/integrity").json()["ok"] is True


def test_adjust_rules(client, w):
    wh = w.warehouse("AK", "Main")
    p = w.product("B1", "Cup")
    w.receive(wh, p, 5)
    no_reason = w.doc("ADJUSTMENT", wh, [{"product_id": p, "qty": 1, "direction": "IN"}])
    assert no_reason.status_code == 422 and no_reason.json()["error"]["code_hint"] == "ADJUST_REASON_REQUIRED"
    no_dir = w.doc("ADJUSTMENT", wh, [{"product_id": p, "qty": 1}], adjust_reason="LOST")
    assert no_dir.status_code == 422 and no_dir.json()["error"]["code_hint"] == "ADJUST_DIRECTION_REQUIRED"
    too_much = w.doc("ADJUSTMENT", wh, [{"product_id": p, "qty": 6, "direction": "OUT"}], adjust_reason="LOST")
    assert too_much.status_code == 409
    # net per product: +3 then -8 on 5 in stock is fine
    ok = w.doc("ADJUSTMENT", wh, [{"product_id": p, "qty": 3, "direction": "IN"}, {"product_id": p, "qty": 8, "direction": "OUT"}],
               adjust_reason="RECOUNT")
    assert ok.status_code == 201 and bal(client, wh, p) == 0
    sales = make_user(client, "SALES", [{"warehouse_id": wh, "can_transact": True}], "sales")
    assert sales.post("/api/documents", json={"doc_type": "ADJUSTMENT", "warehouse_id": wh, "adjust_reason": "FOUND",
                                              "items": [{"product_id": p, "qty": 1, "direction": "IN"}]}).status_code == 403
    keeper = make_user(client, "STOREKEEPER", [{"warehouse_id": wh, "can_transact": True}], "keeper")
    assert keeper.post("/api/documents", json={"doc_type": "ADJUSTMENT", "warehouse_id": wh, "adjust_reason": "FOUND", "post": True,
                                               "items": [{"product_id": p, "qty": 2, "direction": "IN"}]}).status_code == 201
    assert bal(client, wh, p) == 2

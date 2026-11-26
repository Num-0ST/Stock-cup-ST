"""Cancelling a posted document puts the goods back and links both documents; document search covers names."""


def test_cancel_sale_returns_goods_and_search(client, w):
    wh = w.warehouse("CX", "Main")
    p = w.product("C1", "Cup")
    w.receive(wh, p, 100)
    cust = client.post("/api/customers", json={"code": "K1", "name": "ร้านป้าแดง"}).json()["id"]
    sale = w.doc("SALE", wh, [{"product_id": p, "qty": 30, "deposit_qty": 10}], customer_id=cust, remark="บิลด่วน").json()["document"]
    bal = lambda: next(int(r["qty_on_hand"]) for r in client.get("/api/stock/balances", params={"warehouse_id": wh}).json() if r["product_id"] == p)
    assert bal() == 70
    found = lambda q: [d["doc_no"] for d in client.get("/api/documents", params={"q": q}).json()]
    assert sale["doc_no"] in found("ป้าแดง") and sale["doc_no"] in found("ด่วน") and sale["doc_no"] in found(sale["doc_no"][-4:].lower())
    r = client.post(f"/api/documents/{sale['document_id']}/reverse", json={"reason": "ลูกค้ายกเลิกบิล"})
    assert r.status_code == 200, r.text
    assert bal() == 100                                       # everything back in the warehouse, deposit included
    orig = client.get(f"/api/documents/{sale['document_id']}").json()
    assert orig["status"] == "REVERSED" and orig["reversed_by_document_id"] == r.json()["reversal_document_id"]
    assert orig["reversal_reason"] == "ลูกค้ายกเลิกบิล"
    assert all(d["status"] == "CANCELLED" for d in orig["deposits"])
    again = client.post(f"/api/documents/{sale['document_id']}/reverse", json={"reason": "x"})
    assert again.status_code == 409

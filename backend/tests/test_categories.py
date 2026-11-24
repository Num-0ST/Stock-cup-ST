"""Product categories: add from the product form or the master-data page, rename, disable, no duplicates."""
from test_stock_engine import make_user


def test_create_rename_disable_and_duplicates(client, w):
    wh = w.warehouse("C1", "Main")
    r = client.post("/api/categories", json={"name": "  เครื่องดื่ม  "})
    assert r.status_code == 201 and r.json()["name"] == "เครื่องดื่ม"
    cid = r.json()["category_id"]
    dup = client.post("/api/categories", json={"name": "เครื่องดื่ม"})
    assert dup.status_code == 422 and dup.json()["error"]["code_hint"] == "DUPLICATE"
    other = client.post("/api/categories", json={"name": "ขนม"}).json()["category_id"]
    assert client.put(f"/api/categories/{other}", json={"name": "เครื่องดื่ม"}).json()["error"]["code_hint"] == "DUPLICATE"
    p = w.product("P1", "Cola")
    prod = client.get("/api/products", params={"q": "P1"}).json()[0]
    upd = client.put(f"/api/products/{p}", json={"product_code": "P1", "name": "Cola", "category_id": cid,
                                                 "reorder_level": 0, "is_active": True,
                                                 "conversions": [{"unit_code": c["unit_code"], "factor_to_base": c["factor_to_base"]} for c in prod["conversions"]]})
    assert upd.status_code == 200, upd.text
    r = client.put(f"/api/categories/{cid}", json={"name": "น้ำดื่ม", "is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    cats = {c["category_id"]: c for c in client.get("/api/categories").json()}
    assert cats[cid]["name"] == "น้ำดื่ม" and cats[cid]["product_count"] == 1
    assert client.get("/api/products", params={"q": "P1"}).json()[0]["category_name"] == "น้ำดื่ม"
    assert client.put("/api/categories/999999", json={"name": "x"}).status_code == 404

    keeper = make_user(client, "STOREKEEPER", [{"warehouse_id": wh, "can_transact": True}], "keeper")
    assert keeper.post("/api/categories", json={"name": "ของใช้"}).status_code == 201
    viewer = make_user(client, "VIEWER", [{"warehouse_id": wh}], "viewer")
    assert viewer.post("/api/categories", json={"name": "ห้าม"}).status_code == 403
    assert viewer.put(f"/api/categories/{cid}", json={"name": "ห้าม"}).status_code == 403


def test_product_list_filters_by_category_and_stock(client, w):
    wa, wb = w.warehouse("F1", "A"), w.warehouse("F2", "B")
    drinks = client.post("/api/categories", json={"name": "Drinks"}).json()["category_id"]
    p1, p2, p3 = w.product("D1", "Cola"), w.product("D2", "Soda"), w.product("X1", "Cup")
    for p in (p1, p2):
        prod = client.get("/api/products", params={"q": "D"}).json()
        cur = next(x for x in prod if x["product_id"] == p)
        client.put(f"/api/products/{p}", json={"product_code": cur["product_code"], "name": cur["name"], "category_id": drinks,
                                               "reorder_level": 0, "is_active": True,
                                               "conversions": [{"unit_code": c["unit_code"], "factor_to_base": c["factor_to_base"]} for c in cur["conversions"]]})
    w.receive(wa, p1, 5)
    w.receive(wb, p3, 5)
    codes = lambda **k: [x["product_code"] for x in client.get("/api/products", params=k).json()]
    assert codes(category_id=drinks) == ["D1", "D2"]
    assert "X1" in codes(category_id=0) and "D1" not in codes(category_id=0)
    assert codes(category_id=drinks, in_stock_warehouse_id=wa) == ["D1"]
    assert codes(in_stock_warehouse_id=wb) == ["X1"]
    viewer = make_user(client, "VIEWER", [{"warehouse_id": wa}], "viewer2")
    assert viewer.get("/api/products", params={"in_stock_warehouse_id": wb}).status_code == 403

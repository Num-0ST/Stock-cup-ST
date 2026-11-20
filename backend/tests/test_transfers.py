"""Phase 4 — transfers: instant, approval, in-transit, partial receive, cancel, concurrency, permissions."""
import threading

import pytest

from test_stock_engine import assert_ledger_consistent, make_user, q


def bal(wh, pid):
    r = q("SELECT qty_on_hand FROM stock_balances WHERE warehouse_id=%s AND product_id=%s", (wh, pid))
    return r[0]["qty_on_hand"] if r else 0


def transit_id():
    return q("SELECT warehouse_id FROM warehouses WHERE warehouse_type='TRANSIT'")[0]["warehouse_id"]


@pytest.fixture()
def world(client, w):
    a, b = w.warehouse("A", "Source"), w.warehouse("B", "Dest")
    p1, p2 = w.product("P1", "One"), w.product("P2", "Two", conversions=[])
    w.receive(a, p1, 100)
    w.receive(a, p2, 50)
    return a, b, p1, p2


def new(client, a, b, items, mode="APPROVAL", **kw):
    return client.post("/api/transfers", json={"mode": mode, "from_warehouse_id": a, "to_warehouse_id": b, "items": items, **kw})


def test_instant_transfer(client, world):
    a, b, p1, p2 = world
    r = new(client, a, b, [{"product_id": p1, "qty": 2, "unit_code": "CT"}, {"product_id": p2, "qty": 10}], "INSTANT")
    assert r.status_code == 201, r.text
    t = r.json()["transfer"]
    assert t["status"] == "COMPLETED" and t["transfer_mode"] == "INSTANT"
    assert (bal(a, p1), bal(b, p1), bal(a, p2), bal(b, p2)) == (52, 48, 40, 10)   # 2 CT = 48 PC
    it = [i for i in t["items"] if i["product_code"] == "P1"][0]
    assert (it["conversion_factor"], it["qty_sent_base"], it["qty_received_base"]) == (24, 48, 48)
    assert bal(transit_id(), p1) == 0
    assert_ledger_consistent()


def test_instant_insufficient_rolls_back(client, world):
    a, b, p1, p2 = world
    r = new(client, a, b, [{"product_id": p2, "qty": 10}, {"product_id": p1, "qty": 101}], "INSTANT")
    assert r.status_code == 409 and r.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert (bal(a, p1), bal(a, p2), bal(b, p1)) == (100, 50, 0)
    assert q("SELECT count(*) AS n FROM stock_transfers")[0]["n"] == 0
    assert_ledger_consistent()


def test_approval_flow_partial_receive_and_complete(client, world):
    a, b, p1, p2 = world
    t = new(client, a, b, [{"product_id": p1, "qty": 60}, {"product_id": p2, "qty": 20}]).json()
    tid = t["transfer_id"]
    assert t["transfer"]["status"] == "DRAFT" and bal(a, p1) == 100            # draft moves nothing
    # cannot receive a draft
    assert client.post(f"/api/transfers/{tid}/receive", json={}).status_code == 409
    s = client.post(f"/api/transfers/{tid}/ship")
    assert s.status_code == 200 and s.json()["transfer"]["status"] == "IN_TRANSIT"
    assert (bal(a, p1), bal(transit_id(), p1), bal(b, p1)) == (40, 60, 0)
    # shipping again is refused, never double-deducts
    assert client.post(f"/api/transfers/{tid}/ship").status_code == 409
    assert bal(a, p1) == 40
    # partial receive
    r = client.post(f"/api/transfers/{tid}/receive", json={"lines": [{"product_id": p1, "qty": 25}]})
    assert r.status_code == 200 and r.json()["transfer"]["status"] == "PARTIAL"
    assert (bal(transit_id(), p1), bal(b, p1)) == (35, 25)
    # over-receive refused
    r = client.post(f"/api/transfers/{tid}/receive", json={"lines": [{"product_id": p1, "qty": 36}]})
    assert r.status_code == 422 and r.json()["error"]["code_hint"] == "OVER_RECEIVE"
    assert (bal(transit_id(), p1), bal(b, p1)) == (35, 25)
    # receive the rest (everything outstanding)
    r = client.post(f"/api/transfers/{tid}/receive", json={})
    assert r.status_code == 200 and r.json()["transfer"]["status"] == "COMPLETED"
    assert (bal(a, p1), bal(b, p1), bal(a, p2), bal(b, p2), bal(transit_id(), p1), bal(transit_id(), p2)) == (40, 60, 30, 20, 0, 0)
    assert client.post(f"/api/transfers/{tid}/receive", json={}).status_code == 409
    assert client.post(f"/api/transfers/{tid}/cancel", json={}).status_code == 409
    assert_ledger_consistent()


def test_cancel_draft_and_in_transit_and_after_partial(client, world):
    a, b, p1, p2 = world
    d = new(client, a, b, [{"product_id": p1, "qty": 10}]).json()["transfer_id"]
    c = client.post(f"/api/transfers/{d}/cancel", json={"reason": "oops"})
    assert c.status_code == 200 and c.json()["transfer"]["status"] == "CANCELLED" and bal(a, p1) == 100

    t = new(client, a, b, [{"product_id": p1, "qty": 30}]).json()["transfer_id"]
    client.post(f"/api/transfers/{t}/ship")
    assert bal(a, p1) == 70
    c = client.post(f"/api/transfers/{t}/cancel", json={})
    assert c.json()["transfer"]["status"] == "CANCELLED"
    assert (bal(a, p1), bal(transit_id(), p1), bal(b, p1)) == (100, 0, 0)

    t = new(client, a, b, [{"product_id": p1, "qty": 30}]).json()["transfer_id"]
    client.post(f"/api/transfers/{t}/ship")
    client.post(f"/api/transfers/{t}/receive", json={"lines": [{"product_id": p1, "qty": 12}]})
    c = client.post(f"/api/transfers/{t}/cancel", json={"reason": "rest returned"})
    assert c.status_code == 200 and c.json()["transfer"]["status"] == "COMPLETED"
    it = c.json()["transfer"]["items"][0]
    assert (it["qty_received_base"], it["qty_cancelled_base"], it["qty_outstanding_base"]) == (12, 18, 0)
    assert (bal(a, p1), bal(b, p1), bal(transit_id(), p1)) == (88, 12, 0)
    assert client.post(f"/api/transfers/{t}/cancel", json={}).status_code == 409
    assert_ledger_consistent()


def test_ship_insufficient_keeps_draft(client, world):
    a, b, p1, p2 = world
    t = new(client, a, b, [{"product_id": p1, "qty": 80}]).json()["transfer_id"]
    client.post("/api/documents", json={"doc_type": "WITHDRAWAL", "warehouse_id": a, "post": True,
                                        "items": [{"product_id": p1, "qty": 50}]})
    r = client.post(f"/api/transfers/{t}/ship")
    assert r.status_code == 409 and r.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert client.get(f"/api/transfers/{t}").json()["status"] == "DRAFT"
    assert (bal(a, p1), bal(transit_id(), p1)) == (50, 0)
    assert_ledger_consistent()


def test_validation(client, world):
    a, b, p1, p2 = world
    assert new(client, a, a, [{"product_id": p1, "qty": 1}]).status_code == 422
    assert new(client, a, b, []).status_code == 422
    assert new(client, a, b, [{"product_id": p1, "qty": 0}]).status_code == 422
    assert new(client, a, b, [{"product_id": p1, "qty": 1}, {"product_id": p1, "qty": 2}]).status_code == 422
    assert new(client, a, transit_id(), [{"product_id": p1, "qty": 1}]).status_code == 422
    assert new(client, a, b, [{"product_code": "NOPE", "qty": 1}]).status_code == 422
    assert new(client, a, b, [{"product_id": p1, "qty": 1, "unit_code": "PK"}]).status_code == 201
    assert new(client, a, b, [{"product_id": p1, "qty": 1}], transfer_no="DUP-1").status_code == 201
    assert new(client, a, b, [{"product_id": p1, "qty": 1}], transfer_no="DUP-1").status_code == 422
    t = new(client, a, b, [{"product_id": p1, "qty": 5}]).json()["transfer_id"]
    client.post(f"/api/transfers/{t}/ship")
    assert client.post(f"/api/transfers/{t}/receive", json={"lines": [{"product_id": p2, "qty": 1}]}).status_code == 422


def test_idempotency_replay(client, world):
    a, b, p1, p2 = world
    k = {"Idempotency-Key": "tf-1"}
    body = {"mode": "INSTANT", "from_warehouse_id": a, "to_warehouse_id": b, "items": [{"product_id": p1, "qty": 10}]}
    r1 = client.post("/api/transfers", json=body, headers=k)
    r2 = client.post("/api/transfers", json=body, headers=k)
    assert r1.status_code == 201 and r2.status_code == 201 and r2.json()["replayed"] is True
    assert r1.json()["transfer_id"] == r2.json()["transfer_id"] and bal(a, p1) == 90 and bal(b, p1) == 10


def test_concurrent_ship_and_receive_apply_once(client, world):
    a, b, p1, p2 = world
    t = new(client, a, b, [{"product_id": p1, "qty": 40}]).json()["transfer_id"]
    codes = []

    def go(path, body=None):
        from fastapi.testclient import TestClient
        from app.main import app
        c = TestClient(app); c.headers.update(client.headers)
        codes.append((path, c.post(f"/api/transfers/{t}/{path}", json=body or {}).status_code))

    th = [threading.Thread(target=go, args=("ship",)) for _ in range(8)]
    [x.start() for x in th]; [x.join() for x in th]
    assert sorted(c for _, c in codes) == [200] + [409] * 7
    assert (bal(a, p1), bal(transit_id(), p1)) == (60, 40)
    codes.clear()
    th = [threading.Thread(target=go, args=("receive", {})) for _ in range(8)]
    [x.start() for x in th]; [x.join() for x in th]
    assert sorted(c for _, c in codes) == [200] + [409] * 7
    assert (bal(b, p1), bal(transit_id(), p1)) == (40, 0)
    assert_ledger_consistent()


def test_concurrent_opposite_transfers_no_deadlock(client, world):
    a, b, p1, p2 = world
    client.post("/api/documents", json={"doc_type": "RECEIVING", "warehouse_id": b, "post": True, "items": [{"product_id": p1, "qty": 100}]})
    res = []

    def go(src, dst):
        from fastapi.testclient import TestClient
        from app.main import app
        c = TestClient(app); c.headers.update(client.headers)
        for _ in range(10):
            r = c.post("/api/transfers", json={"mode": "INSTANT", "from_warehouse_id": src, "to_warehouse_id": dst,
                                               "items": [{"product_id": p1, "qty": 3}]})
            res.append(r.status_code if r.status_code == 201 else (r.status_code, r.text))

    th = [threading.Thread(target=go, args=(a, b)), threading.Thread(target=go, args=(b, a)),
          threading.Thread(target=go, args=(a, b)), threading.Thread(target=go, args=(b, a))]
    [x.start() for x in th]; [x.join() for x in th]
    assert set(res) == {201}
    assert bal(a, p1) + bal(b, p1) == 200
    assert_ledger_consistent()


def test_permissions(client, world):
    a, b, p1, p2 = world
    ro = lambda wh: {"warehouse_id": wh, "can_view": True, "can_transact": False}
    rw = lambda wh: {"warehouse_id": wh, "can_view": True, "can_transact": True}
    keeper_a = make_user(client, "STOREKEEPER", [rw(a), ro(b)], "keepa")
    keeper_b = make_user(client, "STOREKEEPER", [rw(b), ro(a)], "keepb")
    mgr_a = make_user(client, "MANAGER", [rw(a), ro(b)], "mgra")
    sales = make_user(client, "SALES", [rw(a)], "salesa")
    item = [{"product_id": p1, "qty": 5}]
    mk = lambda c, mode="APPROVAL": c.post("/api/transfers", json={"mode": mode, "from_warehouse_id": a, "to_warehouse_id": b, "items": item})
    assert mk(sales).status_code == 403
    assert mk(keeper_b).status_code == 403                    # no transact on source
    assert mk(keeper_a, "INSTANT").status_code == 403         # instant also needs transact on destination
    t = mk(keeper_a).json()["transfer_id"]
    assert keeper_a.post(f"/api/transfers/{t}/ship").status_code == 403     # approval is for managers
    assert mgr_a.post(f"/api/transfers/{t}/ship").status_code == 200
    assert keeper_a.post(f"/api/transfers/{t}/receive", json={}).status_code == 403   # not the destination's keeper
    assert keeper_b.post(f"/api/transfers/{t}/receive", json={"lines": [{"product_id": p1, "qty": 2}]}).status_code == 200
    assert keeper_a.post(f"/api/transfers/{t}/cancel", json={}).status_code == 403    # in transit: manager only
    assert mgr_a.post(f"/api/transfers/{t}/cancel", json={}).status_code == 200
    # a user with no access to either warehouse cannot even see it
    c_wh = client.post("/api/warehouses", json={"warehouse_code": "C", "name": "C"}).json()["warehouse_id"]
    other = make_user(client, "MANAGER", [rw(c_wh)], "otherc")
    assert other.get(f"/api/transfers/{t}").status_code == 403
    assert other.get("/api/transfers").json() == []
    # draft creator may cancel own draft
    d = mk(keeper_a).json()["transfer_id"]
    assert keeper_a.post(f"/api/transfers/{d}/cancel", json={}).status_code == 200
    assert_ledger_consistent()


def test_transfer_shows_in_movements_and_list(client, world):
    a, b, p1, p2 = world
    t = new(client, a, b, [{"product_id": p1, "qty": 7}]).json()
    client.post(f"/api/transfers/{t['transfer_id']}/ship")
    lst = client.get("/api/transfers", params={"status": "IN_TRANSIT"}).json()
    assert [x["transfer_no"] for x in lst] == [t["transfer"]["transfer_no"]] and lst[0]["qty_sent"] == 7
    mv = client.get("/api/stock/movements", params={"warehouse_id": a, "product_id": p1}).json()
    assert any(m["txn_type"] == "TRANSFER_OUT" and m["doc_no"] == t["transfer"]["transfer_no"] for m in mv)

"""Security sweep: a user of one warehouse must not see or change another warehouse's data,
roles are enforced, logins are throttled and oversized uploads are refused."""
import io

import pytest
from fastapi.testclient import TestClient

from app import billscan
from app.main import app
from test_stock_engine import make_user, q


@pytest.fixture()
def world(client, w, monkeypatch, tmp_path):
    monkeypatch.setattr(billscan, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("BILL_OCR_ENGINE", "claude")
    monkeypatch.setattr(billscan, "call_ai", lambda d, m: {"items": [], "doc_kind": "SALE", "is_handwritten": False})
    a = w.warehouse("WA", "A")
    a2 = w.warehouse("WA2", "A2")
    b = w.warehouse("WB", "B")
    p = w.product("P1", "Cup")
    w.receive(a, p, 500)
    cust = client.post("/api/customers", json={"code": "C1", "name": "Shop"}).json()["id"]
    sale = w.doc("SALE", a, [{"product_id": p, "qty": 50, "deposit_qty": 10}], customer_id=cust).json()["document"]
    draft = w.doc("RECEIVING", a, [{"product_id": p, "qty": 5}], post=False).json()["document_id"]
    tf = client.post("/api/transfers", json={"mode": "APPROVAL", "from_warehouse_id": a, "to_warehouse_id": a2,
                                             "items": [{"product_id": p, "qty": 5}]}).json()["transfer_id"]
    cnt = client.post("/api/counts", json={"warehouse_id": a, "product_id": p, "counted_qty": 400}).json()["count_id"]
    scan = client.post("/api/bill-scan", files={"file": ("b.jpg", io.BytesIO(b"\xff\xd8\xff" + b"0" * 50), "image/jpeg")}).json()["scan_id"]
    w.doc("SALE", a, [{"product_id": p, "qty": 1}], bill_scan_id=scan)
    outsider = make_user(client, "MANAGER", [{"warehouse_id": b, "can_transact": True}], "outsider")
    return {"a": a, "b": b, "p": p, "sale": sale, "draft": draft, "tf": tf, "cnt": cnt, "scan": scan,
            "dep": sale["deposits"][0]["deposit_id"], "out": outsider}


def test_other_warehouse_is_invisible_and_read_only(client, world):
    o, x = world["out"], world
    for url in (f"/api/documents/{x['sale']['document_id']}", f"/api/documents/{x['draft']}", f"/api/deposits/{x['dep']}",
                f"/api/transfers/{x['tf']}", f"/api/bill-scans/{x['scan']}/image"):
        assert o.get(url).status_code in (403, 404), url
    assert o.get("/api/stock/available", params={"warehouse_id": x["a"]}).status_code == 403
    assert o.get("/api/documents").json() == [] or all(d["warehouse_id"] == x["b"] for d in o.get("/api/documents").json())
    for url in ("/api/deposits", "/api/counts", "/api/transfers"):
        body = o.get(url).json()
        assert body == [] or all(x["a"] not in (r.get("warehouse_id"), r.get("from_warehouse_id"), r.get("to_warehouse_id")) for r in body), url
    assert o.get("/api/stock/balances", params={"warehouse_id": x["a"]}).json() == []
    mv = o.get("/api/stock/movements", params={"warehouse_id": x["a"]}).json()
    assert mv == [] or all(m.get("warehouse_id") != x["a"] for m in (mv if isinstance(mv, list) else mv.get("rows", [])))
    for rep in ("stock_balance", "movements", "deposits", "sales_summary", "transfers"):
        r = o.get(f"/api/reports/{rep}", params={"warehouse_id": x["a"]})
        assert r.status_code == 403 or r.json()["rows"] == [], rep
        assert all(row.get("warehouse_code") != "WA" for row in o.get(f"/api/reports/{rep}").json()["rows"]), rep
    k = o.get("/api/dashboard", params={"warehouse_id": x["a"]})
    assert k.status_code in (200, 403)
    if k.status_code == 200:
        assert k.json()["kpi"]["deposits_open"] == 0

    writes = [
        ("post", f"/api/documents/{x['draft']}/post", None),
        ("delete", f"/api/documents/{x['draft']}", None),
        ("post", f"/api/documents/{x['sale']['document_id']}/reverse", {"reason": "x"}),
        ("post", "/api/documents", {"doc_type": "RECEIVING", "warehouse_id": x["a"], "items": [{"product_id": x["p"], "qty": 1}]}),
        ("post", f"/api/deposits/{x['dep']}/deliver", {}),
        ("post", f"/api/deposits/{x['dep']}/cancel", {"reason": "x"}),
        ("post", "/api/deposits", {"sale_document_id": x["sale"]["document_id"], "items": [{"sale_item_id": x["sale"]["items"][0]["item_id"], "qty": 1}]}),
        ("post", f"/api/transfers/{x['tf']}/ship", {}),
        ("post", f"/api/transfers/{x['tf']}/cancel", {"reason": "x"}),
        ("post", f"/api/counts/{x['cnt']}/apply", {}),
        ("post", "/api/transfers", {"mode": "INSTANT", "from_warehouse_id": x["a"], "to_warehouse_id": x["b"], "items": [{"product_id": x["p"], "qty": 1}]}),
    ]
    for method, url, body in writes:
        r = getattr(o, method)(url, json=body) if body is not None else getattr(o, method)(url)
        assert r.status_code in (403, 404), (url, r.status_code, r.text)
    # nothing changed
    assert q("SELECT status FROM stock_documents WHERE document_id=%s", (x["draft"],))[0]["status"] == "DRAFT"
    assert q("SELECT status FROM customer_deposits WHERE deposit_id=%s", (x["dep"],))[0]["status"] == "OPEN"
    assert q("SELECT status FROM stock_transfers WHERE transfer_id=%s", (x["tf"],))[0]["status"] == "DRAFT"


def test_admin_only_and_anonymous(client, world):
    o = world["out"]
    for url in ("/api/users", "/api/admin/integrity", "/api/reports/audit"):
        assert o.get(url).status_code == 403, url
    anon = TestClient(app)
    for url in ("/api/documents", "/api/stock/balances", "/api/deposits", "/api/products", "/api/dashboard", "/api/me"):
        assert anon.get(url).status_code == 401, url
    bad = TestClient(app); bad.headers["Authorization"] = "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIiwiaWF0IjoxfQ."
    assert bad.get("/api/me").status_code == 401          # alg=none token is rejected


def test_login_throttled_per_address_and_no_username_probe(client):
    c = TestClient(app, client=("203.0.113.7", 5000))
    for n in range(30):
        r = c.post("/api/auth/login", json={"username": f"nobody{n}", "password": "x"})
        assert r.status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-123"})
    assert r.status_code == 429 and r.json()["error"]["code_hint"] == "IP_THROTTLED"
    other = TestClient(app, client=("203.0.113.8", 5000))
    assert other.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-123"}).status_code == 200


def test_oversized_upload_refused(client):
    r = client.post("/api/document-lines/parse", content=b"x" * 10, headers={"content-length": str(40 * 1024 * 1024),
                                                                           "content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413


def test_visitor_address_behind_tunnel_only(client):
    from app.main import _client_ip

    class R:
        def __init__(self, host, hdr):
            self.client = type("C", (), {"host": host})()
            self.headers = hdr
    assert _client_ip(R("172.18.0.5", {"cf-connecting-ip": "1.2.3.4"})) == "1.2.3.4"      # from the tunnel container
    assert _client_ip(R("192.168.1.20", {"cf-connecting-ip": "1.2.3.4"})) == "192.168.1.20"  # direct LAN client: ignored
    assert _client_ip(R("172.18.0.5", {"cf-connecting-ip": "junk"})) == "172.18.0.5"

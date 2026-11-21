"""Bill scanning: photo -> AI (mocked here) -> matched draft -> document."""
import io
import json

import httpx
import pytest

from app import billscan
from test_stock_engine import q

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 200


@pytest.fixture()
def shop(client, w, monkeypatch, tmp_path):
    monkeypatch.setattr(billscan, "UPLOAD_DIR", tmp_path)
    wh = w.warehouse("HHX", "Main")
    van = w.warehouse("VAN03", "Van 3")
    p1 = w.product("P00061", "แก้วพลาสติก 16 ออนซ์")
    p2 = w.product("P00099", "ฝาโดม 98 มม.", conversions=[{"unit_code": "CT", "factor_to_base": 1000, "barcode": "8850001112223"}])
    w.receive(van, p1, 1000)
    w.receive(van, p2, 5000)
    client.post("/api/customers", json={"code": "C001", "name": "ร้านป้าแดง"})
    return {"wh": wh, "van": van, "p1": p1, "p2": p2, "w": w}


AI_RESULT = {
    "doc_no": "HS6800123", "doc_date": "2568-11-20", "doc_kind": "SALE", "customer_code": "C001",
    "customer_name": "ร้านป้าแดง", "salesman": "VAN03", "is_handwritten": True,
    "items": [
        {"product_code": "P00061", "description": "แก้ว 16oz", "qty": 2, "unit": "ลัง", "deposit_qty": 1, "confidence": "high"},
        {"product_code": None, "barcode": "8850001112223", "description": "ฝาโดม", "qty": 1, "unit": None, "confidence": "high"},
        {"product_code": None, "description": "แก้วพลาสติก 16 ออนซ", "qty": 6, "unit": "ชิ้น", "confidence": "low"},
        {"product_code": "ZZZ", "description": "สินค้าไม่รู้จัก", "qty": 3, "unit": "โหล", "confidence": "medium"},
    ],
    "total_amount": 1234.5, "notes": None,
}


def upload(client, data=JPEG, name="bill.jpg"):
    return client.post("/api/bill-scan", files={"file": (name, io.BytesIO(data), "image/jpeg")})


def test_not_configured_and_bad_image(client, shop, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client.get("/api/bill-scan/status").json()["configured"] is False
    r = upload(client)
    assert r.status_code == 503 and r.json()["error"]["code"] == "AI_NOT_CONFIGURED"
    assert q("SELECT status FROM bill_scans")[0]["status"] == "FAILED"
    r = upload(client, b"%PDF-1.4 not an image", "x.pdf")
    assert r.status_code == 422 and r.json()["error"]["code"] == "BAD_IMAGE"


def test_scan_builds_matched_draft_then_document(client, shop, monkeypatch):
    s = shop
    monkeypatch.setattr(billscan, "call_ai", lambda data, mime: json.loads(json.dumps(AI_RESULT)))
    r = upload(client)
    assert r.status_code == 201, r.text
    d = r.json()["draft"]
    assert (d["doc_type"], d["doc_no"], d["doc_date"]) == ("SALE", "HS6800123", "2025-11-20")   # Buddhist year fixed
    assert d["warehouse_id"] == s["van"] and d["customer_label"] == "C001 ร้านป้าแดง"
    it = d["items"]
    assert (it[0]["product_code"], it[0]["unit_code"], it[0]["qty"], it[0]["deposit_qty"], it[0]["match"]) == \
        ("P00061", "CT", "2", "1", "code")
    assert (it[1]["product_code"], it[1]["unit_code"], it[1]["match"]) == ("P00099", "CT", "barcode")   # unit barcode -> CT
    assert it[2]["product_code"] == "P00061" and it[2]["match"] == "name" and it[2]["unit_code"] == "PC"
    codes2 = [x["code"] for x in it[2]["warnings"]]
    assert "LOW_CONFIDENCE" in codes2 and "NAME_MATCH" in codes2
    assert it[3]["product_id"] is None and {"code": "PRODUCT_NOT_FOUND"} in it[3]["warnings"]
    assert {"code": "UNKNOWN_UNIT", "unit": "โหล"} in it[3]["warnings"]

    # the user reviews, drops the unknown line and saves (with deposit)
    cust = q("SELECT customer_id FROM customers WHERE customer_code='C001'")[0]["customer_id"]
    items = [{"product_id": i["product_id"], "unit_code": i["unit_code"], "qty": i["qty"], "deposit_qty": i["deposit_qty"]}
             for i in it if i["product_id"]]
    r = s["w"].doc("SALE", d["warehouse_id"], items, doc_no=d["doc_no"], doc_date=d["doc_date"], customer_id=cust,
                   bill_scan_id=r.json()["scan_id"])
    assert r.status_code == 201, r.text
    doc = r.json()["document"]
    assert doc["bill_scan_id"] and doc["deposits"][0]["qty_remaining_base"] == 24
    assert q("SELECT status FROM bill_scans WHERE scan_id=%s", (doc["bill_scan_id"],))[0]["status"] == "USED"
    img = client.get(f"/api/bill-scans/{doc['bill_scan_id']}/image")
    assert img.status_code == 200 and img.content == JPEG and img.headers["content-type"] == "image/jpeg"

    # scanning the same photo / bill again warns about duplicates
    d2 = upload(client).json()["draft"]
    codes = {x["code"] for x in d2["warnings"]}
    assert {"IMAGE_ALREADY_USED", "DUPLICATE_DOC"} <= codes


def test_credit_note_and_unknown_customer(client, shop, monkeypatch):
    res = {"doc_no": "CN0003", "doc_date": None, "doc_kind": "SALE", "customer_code": "NEW9", "customer_name": "ร้านใหม่",
           "salesman": "X99", "is_handwritten": False,
           "items": [{"product_code": "P00061", "qty": 5, "unit": "pcs", "deposit_qty": 2, "confidence": "high"}]}
    monkeypatch.setattr(billscan, "call_ai", lambda data, mime: res)
    d = upload(client, PNG, "cn.png").json()["draft"]
    assert d["doc_type"] == "CUSTOMER_RETURN" and d["warehouse_id"] is None and d["customer_id"] is None
    assert d["customer_suggestion"] == {"code": "NEW9", "name": "ร้านใหม่"}
    assert d["items"][0]["deposit_qty"] is None                 # deposits only on sales
    codes = {x["code"] for x in d["warnings"]}
    assert {"NO_DATE", "CUSTOMER_NOT_FOUND"} <= codes


def test_warehouse_suggestion_respects_permissions(client, shop, monkeypatch):
    from test_stock_engine import make_user
    monkeypatch.setattr(billscan, "call_ai", lambda data, mime: AI_RESULT)
    other = make_user(client, "SALES", [{"warehouse_id": shop["wh"], "can_transact": True}], "seller")
    d = other.post("/api/bill-scan", files={"file": ("b.jpg", io.BytesIO(JPEG), "image/jpeg")}).json()["draft"]
    assert d["warehouse_id"] is None
    viewer = make_user(client, "VIEWER", [{"warehouse_id": shop["wh"]}], "viewer")
    assert viewer.post("/api/bill-scan", files={"file": ("b.jpg", io.BytesIO(JPEG), "image/jpeg")}).status_code == 403


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def test_call_ai_request_and_errors(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("BILL_AI_MODEL", "claude-sonnet-5-5")
    seen = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        seen.update(url=url, body=json, headers=headers)
        return FakeResp(200, {"content": [{"type": "tool_use", "name": "record_bill", "input": {"items": [], "doc_kind": "SALE"}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert billscan.call_ai(JPEG, "image/jpeg") == {"items": [], "doc_kind": "SALE"}
    b = seen["body"]
    assert b["model"] == "claude-sonnet-5-5" and b["tool_choice"] == {"type": "tool", "name": "record_bill"}
    assert b["messages"][0]["content"][0]["source"]["media_type"] == "image/jpeg"
    assert seen["headers"]["x-api-key"] == "sk-test" and seen["headers"]["anthropic-version"] == "2023-06-01"

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp(401, {"error": {"message": "invalid x-api-key"}}))
    with pytest.raises(billscan.AIFailed) as e:
        billscan.call_ai(JPEG, "image/jpeg")
    assert e.value.detail["code_hint"] == "AI_BAD_KEY"

    def boom(*a, **k):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(billscan.AIFailed) as e:
        billscan.call_ai(JPEG, "image/jpeg")
    assert e.value.detail["code_hint"] == "AI_UNREACHABLE"


def test_unit_words():
    assert [billscan.unit_code_of(x) for x in ("ลัง", "แพ็ค", "ชิ้น", "CT", "pcs", "ແພັກ", "โหล", None)] == \
        ["CT", "PK", "PC", "CT", "PC", "PK", None, None]


def test_bill_dates():
    p = billscan._parse_date
    assert [str(p(x)) for x in ("2025-11-20", "2568-11-20", "20/11/2568", "20/11/68", "5.1.2025")] == \
        ["2025-11-20", "2025-11-20", "2025-11-20", "2025-11-20", "2025-01-05"]
    assert p("31/02/2025") is None and p("abc") is None and p(None) is None

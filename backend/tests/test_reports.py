"""Phase 5 — dashboard, reports, exports, movers, integrity check."""
import io
import shutil
import subprocess
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

from app import reports
from test_stock_engine import assert_ledger_consistent, make_user, q

D = lambda n: (date.today() - timedelta(days=n)).isoformat()


@pytest.fixture()
def shop(client, w):
    a, b = w.warehouse("A", "Main"), w.warehouse("B", "Branch")
    ps = {c: w.product(c, f"Product {c}", conversions=[]) for c in ("P1", "P2", "P3", "P4", "P5")}
    client.put(f"/api/products/{ps['P1']}", json={"product_code": "P1", "name": "Product P1", "reorder_level": 50, "conversions": []})
    client.put(f"/api/products/{ps['P4']}", json={"product_code": "P4", "name": "Product P4", "reorder_level": 10, "conversions": []})
    for c, n in (("P1", 100), ("P2", 500), ("P3", 300), ("P4", 8), ("P5", 40)):
        assert w.doc("RECEIVING", a, [{"product_id": ps[c], "qty": n}], doc_date=D(20)).status_code == 201
    w.doc("RECEIVING", b, [{"product_id": ps["P2"], "qty": 60}], doc_date=D(20))
    sale = lambda wh, c, n, d, no: w.doc("SALE", wh, [{"product_id": ps[c], "qty": n}], doc_date=D(d), doc_no=no)
    assert sale(a, "P1", 60, 5, "S1").status_code == 201           # P1: 40 left -> LOW (reorder 50)
    assert sale(a, "P2", 200, 3, "S2").status_code == 201
    assert sale(a, "P2", 100, 1, "S3").status_code == 201
    assert sale(a, "P3", 10, 2, "S4").status_code == 201
    assert sale(b, "P2", 10, 1, "S5").status_code == 201
    rs = sale(a, "P3", 40, 0, "S6").json()["document_id"]           # will be reversed -> must not count as sold
    assert client.post(f"/api/documents/{rs}/reverse", json={"reason": "x"}).status_code == 200
    assert w.doc("CUSTOMER_RETURN", a, [{"product_id": ps["P2"], "qty": 20}], doc_date=D(0)).status_code == 201
    return a, b, ps


def rep(client, name, **params):
    r = client.get(f"/api/reports/{name}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_stock_balance_and_low_stock(client, shop):
    a, b, ps = shop
    rows = {(r["warehouse_code"], r["product_code"]): r for r in rep(client, "stock_balance", limit=500)["rows"]}
    assert rows[("A", "P1")]["qty"] == 40 and rows[("A", "P1")]["status"] == "LOW"
    assert rows[("A", "P2")]["qty"] == 220 and rows[("A", "P2")]["status"] == "OK"      # 500-200-100+20
    assert rows[("A", "P3")]["qty"] == 290                                               # 300-10; the reversed sale restored 40
    assert rows[("A", "P4")]["status"] == "LOW" and rows[("B", "P2")]["qty"] == 50
    assert ("B", "P1") in rows and rows[("B", "P1")]["status"] == "OUT"                  # reorder level set, never stocked
    low = rep(client, "low_stock")["rows"]
    assert {(r["warehouse_code"], r["product_code"], r["status"]) for r in low} == {("A", "P1", "LOW"), ("A", "P4", "LOW"), ("B", "P1", "OUT"), ("B", "P4", "OUT")}
    only_b = rep(client, "stock_balance", warehouse_id=b)["rows"]
    assert {r["warehouse_code"] for r in only_b} == {"B"}
    assert [r["product_code"] for r in rep(client, "stock_balance", q="P5")["rows"]] == ["P5"]


def test_sales_summary_nets_reversals_and_returns(client, shop):
    rows = {r["product_code"]: r for r in rep(client, "sales_summary", date_from=D(10), date_to=D(0))["rows"]}
    assert (rows["P2"]["sold"], rows["P2"]["returned"], rows["P2"]["net"], rows["P2"]["docs"]) == (310, 20, 290, 3)
    assert rows["P1"]["net"] == 60 and rows["P3"]["sold"] == 10                          # reversed S6 (40) not counted
    assert "P4" not in rows and "P5" not in rows
    assert [r["product_code"] for r in rep(client, "sales_summary", date_from=D(10), date_to=D(0))["rows"]][0] == "P2"
    narrow = {r["product_code"] for r in rep(client, "sales_summary", date_from=D(1), date_to=D(0))["rows"]}
    assert narrow == {"P2"}                                                              # only P2 sold within 1 day (+ return today)


def test_movers_classification(client, shop):
    rows = {r["product_code"]: r for r in rep(client, "movers", days=30)["rows"]}
    assert rows["P2"]["class"] == "FAST" and rows["P2"]["sold"] == 290 and rows["P2"]["avg_daily"] == round(290 / 30, 2)
    assert rows["P3"]["class"] == "SLOW" and rows["P1"]["class"] == "NORMAL"
    assert rows["P4"]["class"] == "DEAD" and rows["P5"]["class"] == "DEAD" and rows["P4"]["cover"] is None
    assert rows["P2"]["cover"] == round((220 + 50) / (290 / 30), 1)                      # on hand across both warehouses
    assert [r["product_code"] for r in rep(client, "movers", days=30, **{"class": "DEAD"})["rows"]] == ["P4", "P5"]
    assert client.get("/api/reports/movers", params={"days": 0}).status_code == 422


def test_classify_unit():
    items = [{"product_code": f"X{i}", "sold": s} for i, s in enumerate([100, 80, 60, 40, 20, 0])]
    reports.classify_movers(items)
    assert [i["class"] for i in items] == ["FAST", "NORMAL", "NORMAL", "NORMAL", "SLOW", "DEAD"]
    one = [{"product_code": "A", "sold": 5}]
    reports.classify_movers(one)
    assert one[0]["class"] == "FAST"


def test_dashboard(client, shop):
    d = client.get("/api/dashboard", params={"days": 7}).json()
    k = d["kpi"]
    assert k["total_qty"] == 40 + 220 + 290 + 8 + 40 + 50 and k["low_stock"] == 4 and k["out_of_stock"] == 2
    assert k["sold_today"] == -20 and k["sale_docs_today"] == 0                           # only a return today
    assert k["sold_period"] == 60 + 200 + 100 + 10 + 10 - 20
    assert len(d["series"]) == 7 and d["series"][-1]["date"] == D(0) and d["series"][-1]["sold"] == -20
    assert d["top_products"][0]["product_code"] == "P2" and int(d["top_products"][0]["sold"]) == 290
    assert {r["warehouse_code"]: int(r["qty"]) for r in d["by_warehouse"]} == {"A": 598, "B": 50}
    assert d["low_stock_items"][0]["qty"] <= d["low_stock_items"][0]["reorder_level"]
    assert client.get("/api/dashboard", params={"warehouse_id": 999}).status_code == 403
    only_b = client.get("/api/dashboard", params={"warehouse_id": shop[1]}).json()
    assert only_b["kpi"]["total_qty"] == 50 and only_b["kpi"]["sold_period"] == 10


def test_scoping_by_warehouse_access(client, shop):
    a, b, ps = shop
    user = make_user(client, "STOREKEEPER", [{"warehouse_id": b, "can_view": True, "can_transact": False}], "scoped1")
    rows = user.get("/api/reports/stock_balance").json()["rows"]
    assert {r["warehouse_code"] for r in rows} == {"B"}
    assert user.get("/api/reports/stock_balance", params={"warehouse_id": a}).status_code == 403
    assert user.get("/api/dashboard").json()["kpi"]["total_qty"] == 50
    assert {r["product_code"] for r in user.get("/api/reports/sales_summary", params={"date_from": D(10), "date_to": D(0)}).json()["rows"]} == {"P2"}
    assert user.get("/api/reports/audit").status_code == 403 and user.get("/api/admin/integrity").status_code == 403
    assert "audit" not in [x["name"] for x in user.get("/api/reports").json()]
    assert "audit" in [x["name"] for x in client.get("/api/reports").json()]
    nobody = make_user(client, "VIEWER", [], "scoped2")
    assert nobody.get("/api/reports/stock_balance").json()["rows"] == []


def test_movements_transfers_and_audit_reports(client, shop):
    a, b, ps = shop
    t = client.post("/api/transfers", json={"mode": "INSTANT", "from_warehouse_id": a, "to_warehouse_id": b, "items": [{"product_id": ps["P5"], "qty": 15}]})
    assert t.status_code == 201
    mv = rep(client, "movements", date_from=D(30), date_to=D(0), product_id=ps["P5"])["rows"]
    assert sorted((m["warehouse_code"], m["qty_in"], m["qty_out"]) for m in mv) == [("A", 0, 15), ("A", 40, 0), ("B", 15, 0)]
    tr = rep(client, "transfers")["rows"]
    assert len(tr) == 1 and (tr[0]["sent"], tr[0]["received"], tr[0]["status"]) == (15, 15, "COMPLETED")
    au = rep(client, "audit", action="POST_DOC")["rows"]
    assert au and all(r["action"] == "POST_DOC" for r in au)
    assert client.get("/api/reports/nope").status_code == 404
    assert client.get("/api/reports/movements", params={"date_from": D(0), "date_to": D(5)}).status_code == 422
    assert client.get("/api/reports/stock_balance", params={"format": "docx"}).status_code == 422


def test_excel_export_matches_json_and_is_audited(client, shop):
    r = client.get("/api/reports/sales_summary", params={"format": "xlsx", "lang": "en", "date_from": D(10), "date_to": D(0)})
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"] and ".xlsx" in r.headers["content-disposition"]
    ws = load_workbook(io.BytesIO(r.content)).active
    header = [c.value for c in ws[4]]
    assert header == ["Code", "Product", "Category", "Sold (PC)", "Returned (PC)", "Net sold (PC)", "Documents"]
    body = {row[0].value: [c.value for c in row] for row in ws.iter_rows(min_row=5)}
    assert body["P2"][3:] == [310, 20, 290, 3] and body["P1"][5] == 60
    th = load_workbook(io.BytesIO(client.get("/api/reports/stock_balance", params={"format": "xlsx", "lang": "th"}).content)).active
    assert th["A1"].value == "ยอดคงเหลือสินค้า" and "ใกล้หมด" in {row[8].value for row in th.iter_rows(min_row=5)}
    lo = load_workbook(io.BytesIO(client.get("/api/reports/stock_balance", params={"format": "xlsx", "lang": "lo"}).content)).active
    assert lo["A1"].value == "ຍອດຄົງເຫຼືອສິນຄ້າ"
    assert q("SELECT count(*) n FROM audit_logs WHERE action='EXPORT_REPORT'")[0]["n"] == 3


def pdf_text(data: bytes) -> str:
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    return subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True, check=True).stdout.decode()


def test_pdf_export_thai_and_english(client, shop):
    r = client.get("/api/reports/stock_balance", params={"format": "pdf", "lang": "th"})
    assert r.status_code == 200 and r.content[:5] == b"%PDF-" and r.headers["content-type"] == "application/pdf"
    txt = pdf_text(r.content)
    # pdftotext returns shaped Thai glyphs in visual order, so compare marks-free fragments only
    assert "แถว" in txt and "P1" in txt and "หมด" in txt and "ยอดคงเหลือสิน" in txt
    en = pdf_text(client.get("/api/reports/movers", params={"format": "pdf", "lang": "en", "days": 30}).content)
    assert "Fast / slow moving" in en and "P2" in en and "Fast" in en


def test_pdf_paginates_large_report(client, w):
    wh = w.warehouse("A", "Main")
    items = [{"product_id": w.product(f"PP{i:03d}", f"Item {i}", conversions=[]), "qty": 5} for i in range(120)]
    assert w.doc("RECEIVING", wh, items).status_code == 201
    r = client.get("/api/reports/stock_balance", params={"format": "pdf", "lang": "en"})
    assert r.status_code == 200
    txt = pdf_text(r.content)
    assert "PP000" in txt and "PP119" in txt and txt.count("\x0c") >= 3          # several pages, header repeated on each
    assert txt.count("Warehouse") >= 3 and "Page 3/" in txt


def test_integrity_check_ok_and_detects_tampering(client, shop):
    a, b, ps = shop
    client.post("/api/transfers", json={"mode": "APPROVAL", "from_warehouse_id": a, "to_warehouse_id": b, "items": [{"product_id": ps["P5"], "qty": 10}]})
    tid = q("SELECT transfer_id FROM stock_transfers")[0]["transfer_id"]
    client.post(f"/api/transfers/{tid}/ship")
    client.post(f"/api/transfers/{tid}/receive", json={"lines": [{"product_id": ps["P5"], "qty": 4}]})
    res = client.get("/api/admin/integrity").json()
    assert res["ok"] is True, [c for c in res["checks"] if not c["ok"]]
    assert {c["name"] for c in res["checks"]} >= {"balance_equals_ledger", "transit_equals_open_transfers", "negative_physical_stock"}
    assert_ledger_consistent()
    # tamper: change a balance behind the engine's back -> the check must notice
    import os, psycopg
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        c.execute("UPDATE stock_balances SET qty_on_hand = qty_on_hand + 5 WHERE warehouse_id=%s AND product_id=%s", (a, ps["P2"]))
    bad = client.get("/api/admin/integrity").json()
    failed = {c["name"] for c in bad["checks"] if not c["ok"]}
    assert bad["ok"] is False and {"balance_equals_ledger", "running_balance_matches"} <= failed

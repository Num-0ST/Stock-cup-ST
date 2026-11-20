"""Excel import engine tests — real PostgreSQL, real HTTP API, real files."""
import io
import os

import openpyxl
import pytest

from app import readers
from test_stock_engine import assert_ledger_consistent, make_user, q

HEAD = ["ORDERID", "DATE", "PRODUCT_CODE", "QTY", "UNIT", "CUSTOMER", "CUSTOMER_NAME", "SALESMAN"]
MAP = {"doc_no": "ORDERID", "doc_date": "DATE", "product_code": "PRODUCT_CODE", "qty": "QTY", "unit_code": "UNIT",
       "customer_code": "CUSTOMER", "customer_name": "CUSTOMER_NAME", "salesman": "SALESMAN"}
MAP_MIN = {"doc_no": "ORDERID", "doc_date": "DATE", "product_code": "PRODUCT_CODE", "qty": "QTY"}


# ---------------------------------------------------------------- builders


def xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def flat(rows, head=HEAD):
    return [list(head)] + [list(r) for r in rows]


def upload(client, data: bytes, name="sales.xlsx", wh=None, doc_type="SALE", c=None):
    return (c or client).post("/api/imports", files={"file": (name, data, "application/octet-stream")},
                              data={"warehouse_id": wh, "doc_type": doc_type})


def process(client, bid, mapping=MAP, options=None, **kw):
    body = {"mapping": mapping, "options": options or {}, **kw}
    return client.post(f"/api/imports/{bid}/process", json=body)


def run(client, rows, wh, mapping=MAP, options=None, head=HEAD, doc_type="SALE", name="sales.xlsx"):
    """upload + process; returns (batch_id, summary)"""
    r = upload(client, xlsx_bytes({"Sheet1": flat(rows, head)}), name, wh, doc_type)
    assert r.status_code == 201, r.text
    bid = r.json()["batch_id"]
    p = process(client, bid, mapping, options)
    assert p.status_code == 200, p.text
    return bid, p.json()["summary"]


def rows_of(client, bid, status=None):
    params = {"limit": 500, **({"status": status} if status else {})}
    return client.get(f"/api/imports/{bid}/rows", params=params).json()


def confirm(client, bid, skip=False, key=None):
    return client.post(f"/api/imports/{bid}/confirm", json={"skip_errors": skip},
                       headers={"Idempotency-Key": key} if key else {})


@pytest.fixture()
def shop(client, w):
    wh = w.warehouse()
    a = w.product("A", "Prod A", conversions=[{"unit_code": "CT", "factor_to_base": 24}])
    b = w.product("B", "Prod B", conversions=[])
    w.receive(wh, a, 1000)
    w.receive(wh, b, 100)
    return {"wh": wh, "a": a, "b": b}


# ---------------------------------------------------------------- file formats


def test_xlsx_sheet_and_header_row_selection(client, shop):
    data = xlsx_bytes({
        "Cover": [["Report", None], ["generated", "today"]],
        "Data": [["Company X sales"], [None], HEAD, ["S1", "2026-01-05", "A", 3, "PC", "C1", "Cust", "V1"]],
    })
    r = upload(client, data, wh=shop["wh"])
    assert r.status_code == 201
    j = r.json()
    assert j["sheets"] == ["Cover", "Data"] and j["file_kind"] == "xlsx"
    pv = client.get(f"/api/imports/{j['batch_id']}/preview", params={"sheet": "Data", "header_row": 3}).json()
    assert pv["sheet"] == "Data" and pv["preview"]["headers"] == HEAD
    assert pv["suggested_mapping"]["qty"] == "QTY" and pv["suggested_mapping"]["doc_no"] == "ORDERID"
    assert pv["guessed_header_row"] == 3
    p = process(client, j["batch_id"], MAP, sheet="Data", header_row=3)
    assert p.status_code == 200 and p.json()["summary"]["rows"]["valid"] == 1


def test_csv_utf8_thai_cp874_and_semicolon(client, shop):
    wh = shop["wh"]
    text = "ORDERID,DATE,PRODUCT_CODE,QTY\nS1,2026-01-05,A,3\nS2,05/01/2026,A,2\n"
    for enc in ("utf-8-sig", "cp874"):
        csv_text = text.replace("ORDERID", "ORDERID").encode(enc)
        r = upload(client, csv_text, "x.csv", wh)
        assert r.status_code == 201 and r.json()["file_kind"] == "csv"
        s = process(client, r.json()["batch_id"], MAP_MIN).json()["summary"]
        assert s["rows"]["valid"] == 2
    r = upload(client, text.replace(",", ";").encode(), "semi.csv", wh)
    assert process(client, r.json()["batch_id"], MAP_MIN).json()["summary"]["rows"]["valid"] == 2


def test_html_saved_as_xls_flat_table(client, shop):
    html = ("<html><body><table><tr><th>ORDERID</th><th>DATE</th><th>PRODUCT_CODE</th><th>QTY</th></tr>"
            "<tr><td>S1</td><td>2026-01-05</td><td>A</td><td>1,000</td></tr></table></body></html>").encode()
    r = upload(client, html, "report.xls", shop["wh"])
    assert r.status_code == 201 and r.json()["file_kind"] == "html"
    s = process(client, r.json()["batch_id"], MAP_MIN).json()["summary"]
    assert s["rows"]["valid"] == 1 and s["qty_out"] == 1000


PIVOT = """<table class="pvtTable">
<tr><th colspan="3" rowspan="3"></th><th class="pvtAxisLabel">Year</th><th class="pvtColLabel" colspan="2">2026</th><th class="pvtTotalLabel" rowspan="4">Totals</th></tr>
<tr><th class="pvtAxisLabel">Month</th><th class="pvtColLabel" colspan="2">11</th></tr>
<tr><th class="pvtAxisLabel">Day</th><th class="pvtColLabel" colspan="1" rowspan="2">02</th><th class="pvtColLabel" colspan="1" rowspan="2">03</th></tr>
<tr><th class="pvtAxisLabel">ORDERID</th><th class="pvtAxisLabel">PRODUCT_CODE</th><th class="pvtAxisLabel">PRODUCT_NAME</th><th></th></tr>
<tr><th class="pvtRowLabel" rowspan="2">HS1</th><th class="pvtRowLabel">A</th><th class="pvtRowLabel" colspan="2">Prod A</th><td class="pvtVal">5.00</td><td class="pvtVal"></td><td class="pvtTotal">5.00</td></tr>
<tr><th class="pvtRowLabel">B</th><th class="pvtRowLabel" colspan="2">Prod B</th><td class="pvtVal">7.00</td><td class="pvtVal"></td><td class="pvtTotal">7.00</td></tr>
<tr><th class="pvtRowLabel">CN1</th><th class="pvtRowLabel">A</th><th class="pvtRowLabel" colspan="2">Prod A</th><td class="pvtVal"></td><td class="pvtVal">-2.00</td><td class="pvtTotal">-2.00</td></tr>
<tr><th class="pvtTotalLabel" colspan="4">Totals</th><td class="pvtTotal">3.00</td><td class="pvtTotal">7.00</td><td class="pvtTotal">10.00</td></tr>
</table>""".encode()


def test_pivot_export_is_flattened_with_dates_and_carried_labels():
    grid, name = readers.read_grid(PIVOT, "html", None)
    assert name == "Pivot"
    assert grid[0] == ["ORDERID", "PRODUCT_CODE", "PRODUCT_NAME", "Year", "Month", "Day", "DATE", "VALUE"]
    assert grid[1:] == [["HS1", "A", "Prod A", "2026", "11", "02", "2026-11-02", 5],
                        ["HS1", "B", "Prod B", "2026", "11", "02", "2026-11-02", 7],
                        ["CN1", "A", "Prod A", "2026", "11", "03", "2026-11-03", -2]]


def test_pivot_import_end_to_end_with_credit_note_rule(client, shop):
    wh = shop["wh"]
    r = upload(client, PIVOT, "Export.xls", wh)
    j = r.json()
    assert j["suggested_mapping"]["doc_no"] == "ORDERID" and j["suggested_mapping"]["doc_date"] == "DATE"
    assert j["suggested_mapping"]["qty"] == "VALUE"
    m = {"doc_no": "ORDERID", "doc_date": "DATE", "product_code": "PRODUCT_CODE", "qty": "VALUE"}
    opts = {"doc_type_rules": [{"prefix": "CN", "doc_type": "CUSTOMER_RETURN"}]}
    s = process(client, j["batch_id"], m, opts).json()["summary"]
    assert s["rows"] == {"total": 3, "valid": 3, "error": 0, "blocked": 0, "duplicate": 0}
    assert s["docs"]["by_type"] == {"SALE": 1, "CUSTOMER_RETURN": 1}
    assert confirm(client, j["batch_id"]).status_code == 200
    bal = {x["product_code"]: x["qty_on_hand"] for x in client.get("/api/stock/balances", params={"warehouse_id": wh}).json()}
    assert bal == {"A": 1000 - 5 + 2, "B": 100 - 7}
    types = {x["doc_no"]: x["doc_type"] for x in client.get("/api/documents").json() if x["source"] == "IMPORT"}
    assert types == {"HS1": "SALE", "CN1": "CUSTOMER_RETURN"}
    assert_ledger_consistent()


def test_bad_files_are_rejected_cleanly(client, shop):
    wh = shop["wh"]
    assert upload(client, b"", "empty.xlsx", wh).status_code == 422
    assert upload(client, os.urandom(500), "junk.xlsx", wh).status_code == 422
    assert upload(client, b"PK\x03\x04 not really a zip", "bad.xlsx", wh).status_code == 422
    assert upload(client, b"<html>no tables here</html>", "x.xls", wh).status_code == 422
    r = upload(client, xlsx_bytes({"S": flat([])}), "e.xlsx", wh)
    assert r.status_code == 201
    p = process(client, r.json()["batch_id"], MAP)
    assert p.status_code == 422 and p.json()["error"]["code"] == "BAD_IMPORT_PARAMS"


# ---------------------------------------------------------------- nothing moves before confirm


def test_validation_and_preview_never_change_stock(client, shop):
    wh, a = shop["wh"], shop["a"]
    bid, s = run(client, [["S1", "2026-01-05", "A", 10, "PC", "C1", "Cust", "V1"]], wh)
    assert s["docs"]["ready"] == 1
    assert q("SELECT qty_on_hand FROM stock_balances WHERE product_id=%s", (a,))[0]["qty_on_hand"] == 1000
    assert q("SELECT count(*) AS n FROM stock_documents WHERE source='IMPORT'")[0]["n"] == 0
    assert q("SELECT count(*) AS n FROM stock_transactions WHERE txn_type='SALE'")[0]["n"] == 0
    # re-processing (changing options) is allowed and still moves nothing
    assert process(client, bid, MAP, {"default_unit": "PC"}).status_code == 200
    assert q("SELECT qty_on_hand FROM stock_balances WHERE product_id=%s", (a,))[0]["qty_on_hand"] == 1000


def test_confirm_posts_documents_units_customers_and_is_idempotent(client, shop):
    wh, a = shop["wh"], shop["a"]
    rows = [["S1", "2026-01-05", "A", 2, "CT", "C1", "ลูกค้า หนึ่ง", "V1"],
            ["S1", "2026-01-05", "B", 5, "PC", "C1", "ลูกค้า หนึ่ง", "V1"],
            ["S2", "2026-01-06", "A", 10, "", "C2", "Two", "V1"]]
    bid, s = run(client, rows, wh)
    assert s["docs"]["ready"] == 2 and s["qty_out"] == 48 + 5 + 10 and s["new_customers"] == 2
    r = confirm(client, bid, key="imp-1")
    assert r.status_code == 200 and r.json()["result"]["documents_posted"] == 2
    again = confirm(client, bid, key="imp-1")
    assert again.json()["replayed"] is True
    assert confirm(client, bid).status_code == 409                      # already confirmed, no key
    bal = {x["product_code"]: x["qty_on_hand"] for x in client.get("/api/stock/balances", params={"warehouse_id": wh}).json()}
    assert bal == {"A": 1000 - 48 - 10, "B": 95}
    docs = {d["doc_no"]: d for d in client.get("/api/documents", params={"doc_type": "SALE"}).json()}
    assert docs["S1"]["customer_name"] == "ลูกค้า หนึ่ง" and docs["S1"]["source"] == "IMPORT"
    item = client.get(f"/api/documents/{docs['S1']['document_id']}").json()["items"][0]
    assert (item["unit_code"], item["conversion_factor"], item["base_qty"]) == ("CT", 24, 48)
    assert client.get(f"/api/imports/{bid}").json()["status"] == "CONFIRMED"
    assert {r["status"] for r in rows_of(client, bid)} == {"POSTED"}
    assert len(client.get("/api/customers").json()) == 2
    assert q("SELECT count(*) AS n FROM audit_logs WHERE action='IMPORT_CONFIRM'")[0]["n"] == 1
    assert_ledger_consistent()


# ---------------------------------------------------------------- row validation


def test_every_validation_rule_reports_the_right_code(client, shop):
    wh = shop["wh"]
    rows = [
        ["O1", "2026-01-05", "A", 5, "PC", "", "", ""],
        ["O2", "not-a-date", "A", 5, "PC", "", "", ""],
        ["O3", "2026-01-05", "NOPE", 5, "PC", "", "", ""],
        ["O4", "2026-01-05", "A", "abc", "PC", "", "", ""],
        ["O5", "2026-01-05", "A", 5, "XX", "", "", ""],
        ["O6", "2026-01-05", "B", 5, "CT", "", "", ""],
        ["", "2026-01-05", "A", 5, "PC", "", "", ""],
        ["O8", "2026-01-05", "A", 0.5, "PC", "", "", ""],
        ["O9", "2026-01-05", "A", 0, "PC", "", "", ""],
        ["O10", "2026-01-05", "", 5, "PC", "", "", ""],
    ]
    bid, s = run(client, rows, wh)
    codes = {r["row_no"]: r["error_code"] for r in rows_of(client, bid)}
    assert codes == {2: None, 3: "BAD_DATE", 4: "PRODUCT_NOT_FOUND", 5: "BAD_QTY", 6: "UNIT_NOT_FOUND",
                     7: "UNIT_NOT_DEFINED", 8: "MISSING_DOC_NO", 9: "NOT_WHOLE_PC", 10: "BAD_QTY", 11: "MISSING_PRODUCT"}
    assert s["rows"]["valid"] == 1 and s["rows"]["error"] == 9
    c = confirm(client, bid)
    assert c.status_code == 409 and c.json()["error"]["code"] == "IMPORT_HAS_ERRORS"
    assert q("SELECT count(*) AS n FROM stock_documents WHERE source='IMPORT'")[0]["n"] == 0


def test_skip_errors_imports_only_fully_valid_documents(client, shop):
    wh, a = shop["wh"], shop["a"]
    rows = [["GOOD", "2026-01-05", "A", 4, "PC", "", "", ""],
            ["MIXED", "2026-01-05", "A", 1, "PC", "", "", ""],
            ["MIXED", "2026-01-05", "NOPE", 1, "PC", "", "", ""]]       # one bad line spoils the whole document
    bid, s = run(client, rows, wh)
    st = {r["row_no"]: r["status"] for r in rows_of(client, bid)}
    assert st == {2: "VALID", 3: "BLOCKED", 4: "ERROR"}
    assert s["docs"] == {"ready": 1, "blocked": 1, "duplicate": 0, "by_type": {"SALE": 1}}
    r = confirm(client, bid, skip=True)
    assert r.status_code == 200 and r.json()["result"]["documents_posted"] == 1
    assert {d["doc_no"] for d in client.get("/api/documents").json() if d["source"] == "IMPORT"} == {"GOOD"}
    assert q("SELECT qty_on_hand FROM stock_balances WHERE product_id=%s", (a,))[0]["qty_on_hand"] == 996


def test_bad_quantity_in_a_credit_note_blocks_the_whole_note(client, shop):
    """A row whose quantity cannot be read must not let the other rows of its document go through."""
    wh = shop["wh"]
    rows = [["CN9", "2026-01-05", "A", -3, "PC", "", "", ""], ["CN9", "2026-01-05", "B", "oops", "PC", "", "", ""]]
    bid, s = run(client, rows, wh)
    assert {r["status"] for r in rows_of(client, bid)} == {"BLOCKED", "ERROR"}
    assert s["docs"]["ready"] == 0
    assert confirm(client, bid, skip=True).status_code == 409           # nothing importable


def test_dates_numeric_serial_buddhist_year_and_slash_formats(client, shop):
    wh = shop["wh"]
    rows = [["D1", 46027, "A", 1, "PC", "", "", ""],                  # Excel serial (2026-01-05)
            ["D2", "05/01/2569", "A", 1, "PC", "", "", ""],           # Buddhist year, D/M/Y
            ["D3", "2026/01/07", "A", 1, "PC", "", "", ""],
            ["D4", "13/02/2026", "A", 1, "PC", "", "", ""]]
    bid, s = run(client, rows, wh)
    got = {r["doc_no"]: str(r["doc_date"]) for r in rows_of(client, bid)}
    assert got == {"D1": "2026-01-05", "D2": "2026-01-05", "D3": "2026-01-07", "D4": "2026-02-13"}


def test_negative_quantity_rules(client, shop):
    wh = shop["wh"]
    rows = [["R1", "2026-01-05", "A", -3, "PC", "", "", ""]]
    bid, s = run(client, rows, wh)                                     # sales file: negative -> customer return
    assert s["docs"]["by_type"] == {"CUSTOMER_RETURN": 1}
    bid2, s2 = run(client, rows, wh, options={"negative_as_return": False})
    assert s2["error_codes"] == {"NEGATIVE_QTY": 1}
    bid3, s3 = run(client, [["W1", "2026-01-05", "A", -3, "PC", "", "", ""]], wh, doc_type="WITHDRAWAL")
    assert s3["error_codes"] == {"NEGATIVE_QTY": 1}


def test_barcode_matching_incl_unit_barcode(client, w):
    wh = w.warehouse()
    pid = w.product("A", "Prod A", barcode="885001", conversions=[{"unit_code": "CT", "factor_to_base": 24, "barcode": "885099"}])
    w.receive(wh, pid, 500)
    m = {"doc_no": "ORDERID", "doc_date": "DATE", "barcode": "BARCODE", "qty": "QTY"}
    head = ["ORDERID", "DATE", "BARCODE", "QTY"]
    bid, s = run(client, [["B1", "2026-01-05", "885001", 3], ["B2", "2026-01-05", "885099", 2]], wh, m,
                 {"match_by": "BARCODE"}, head)
    assert s["rows"]["valid"] == 2 and s["qty_out"] == 3 + 48            # carton barcode implies the CT unit
    m2 = {**m, "product_code": "CODE"}
    head2 = ["ORDERID", "DATE", "BARCODE", "QTY", "CODE"]
    bid2, s2 = run(client, [["B3", "2026-01-05", "885001", 1, "A"], ["B4", "2026-01-05", "", 1, "a"]], wh, m2,
                   {"match_by": "BOTH"}, head2)
    assert s2["rows"]["valid"] == 2                                     # BOTH: code (case-insensitive) or barcode


# ---------------------------------------------------------------- duplicates


def test_reimporting_same_file_is_detected_and_cannot_double_deduct(client, shop):
    wh, a = shop["wh"], shop["a"]
    rows = [["S1", "2026-01-05", "A", 10, "PC", "", "", ""], ["S2", "2026-01-05", "A", 10, "PC", "", "", ""]]
    data = xlsx_bytes({"Sheet1": flat(rows)})
    r1 = upload(client, data, wh=wh).json()
    process(client, r1["batch_id"], MAP)
    assert confirm(client, r1["batch_id"]).status_code == 200
    r2 = upload(client, data, wh=wh).json()
    assert r2["already_imported_batch_id"] == r1["batch_id"]
    s = process(client, r2["batch_id"], MAP).json()["summary"]
    assert s["rows"]["duplicate"] == 2 and s["rows"]["valid"] == 0 and s["docs"]["duplicate"] == 2
    assert s["file_already_imported"] == r1["batch_id"]
    c = confirm(client, r2["batch_id"])
    assert c.status_code == 409 and c.json()["error"]["code"] == "IMPORT_HAS_ERRORS"
    assert q("SELECT qty_on_hand FROM stock_balances WHERE product_id=%s", (a,))[0]["qty_on_hand"] == 980
    # a new file that overlaps: only the new document is imported, the old one is skipped as duplicate
    bid, s = run(client, rows + [["S3", "2026-01-06", "A", 5, "PC", "", "", ""]], wh)
    assert s["docs"] == {"ready": 1, "blocked": 0, "duplicate": 2, "by_type": {"SALE": 1}}
    assert confirm(client, bid).status_code == 200
    assert q("SELECT qty_on_hand FROM stock_balances WHERE product_id=%s", (a,))[0]["qty_on_hand"] == 975
    assert_ledger_consistent()


def test_duplicate_lines_inside_a_file(client, shop):
    wh = shop["wh"]
    rows = [["S1", "2026-01-05", "A", 1, "PC", "", "", ""], ["S1", "2026-01-05", "A", 1, "PC", "", "", ""]]
    bid, s = run(client, rows, wh)
    assert s["error_codes"] == {"DUP_IN_FILE": 1} and s["docs"]["ready"] == 0   # doc blocked, nothing half-imported
    bid2, s2 = run(client, rows, wh, options={"duplicate_lines": "KEEP"})
    assert s2["docs"]["ready"] == 1 and s2["qty_out"] == 2


def test_inconsistent_document_is_rejected(client, shop):
    rows = [["S1", "2026-01-05", "A", 1, "PC", "", "", ""], ["S1", "2026-01-09", "B", 1, "PC", "", "", ""]]
    bid, s = run(client, rows, shop["wh"])
    assert s["error_codes"] == {"DOC_INCONSISTENT": 2}


# ---------------------------------------------------------------- stock sufficiency


def test_stock_is_simulated_in_date_order_not_file_order(client, w):
    wh, a = w.warehouse(), w.product("A", "A", conversions=[])
    w.receive(wh, a, 10)
    rows = [["LATER", "2026-01-02", "A", 5, "PC", "", "", ""],         # listed first, but dated second
            ["EARLY", "2026-01-01", "A", 8, "PC", "", "", ""]]
    bid, s = run(client, rows, wh)
    st = {r["doc_no"]: (r["status"], r["error_code"]) for r in rows_of(client, bid)}
    assert st == {"EARLY": ("VALID", None), "LATER": ("ERROR", "INSUFFICIENT_STOCK")}
    assert s["shortage_count"] == 1 and s["shortages"][0]["available"] == 2 and s["shortages"][0]["required"] == 5
    # a customer return dated in between makes the later sale possible
    rows.append(["CN1", "2026-01-01", "A", -4, "PC", "", "", ""])
    bid2, s2 = run(client, rows, wh)
    assert s2["docs"]["ready"] == 3 and not s2["error_codes"]
    assert confirm(client, bid2).status_code == 200
    # EARLY sale 8 (10->2), CN +4 (->6) on the same day is applied after it by doc_no order, LATER sale 5 (->1)
    assert q("SELECT qty_on_hand FROM stock_balances WHERE product_id=%s", (a,))[0]["qty_on_hand"] == 10 - 8 + 4 - 5


def test_confirm_rolls_back_everything_if_stock_changed_after_preview(client, shop):
    wh, a = shop["wh"], shop["a"]
    rows = [["S1", "2026-01-05", "A", 400, "PC", "", "", ""], ["S2", "2026-01-06", "A", 400, "PC", "", "", ""]]
    bid, s = run(client, rows, wh)
    assert s["docs"]["ready"] == 2
    assert client.post("/api/documents", json={"doc_type": "SALE", "warehouse_id": wh, "post": True,
                       "items": [{"product_id": a, "qty": 500}]}).status_code == 201     # stock now 500
    c = confirm(client, bid)
    assert c.status_code == 409 and c.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert q("SELECT qty_on_hand FROM stock_balances WHERE product_id=%s", (a,))[0]["qty_on_hand"] == 500   # S1 not kept
    assert q("SELECT count(*) AS n FROM stock_documents WHERE source='IMPORT'")[0]["n"] == 0
    assert q("SELECT count(*) AS n FROM customers")[0]["n"] == 0
    assert client.get(f"/api/imports/{bid}").json()["status"] == "VALIDATED"
    assert_ledger_consistent()
    # fixing the situation lets the same batch be revalidated and confirmed
    p = process(client, bid, MAP).json()["summary"]
    assert p["docs"]["ready"] == 1 and p["rows"]["error"] == 1
    assert confirm(client, bid, skip=True).status_code == 200


# ---------------------------------------------------------------- warehouses (VAN mapping) and permissions


def test_salesman_to_warehouse_mapping(client, w):
    wh1, wh2 = w.warehouse("MAIN", "Main"), w.warehouse("VAN03", "Van 03")
    a = w.product("A", "A", conversions=[])
    w.receive(wh1, a, 100)
    w.receive(wh2, a, 100)
    rows = [["S1", "2026-01-05", "A", 10, "PC", "", "", "VAN03"], ["S2", "2026-01-05", "A", 20, "PC", "", "", "OTHER"]]
    opts = {"warehouse_by": "salesman", "warehouse_map": {"VAN03": wh2}}
    bid, s = run(client, rows, wh1, options=opts)
    assert s["salesmen"] == {"OTHER": 1, "VAN03": 1}
    assert confirm(client, bid).status_code == 200
    assert w.balance(wh2, a) == 90 and w.balance(wh1, a) == 80          # unmapped salesman -> the default warehouse
    docs = {d["doc_no"]: d["warehouse_id"] for d in client.get("/api/documents").json() if d["source"] == "IMPORT"}
    assert docs == {"S1": wh2, "S2": wh1}


def test_warehouse_permissions_on_import(client, w):
    wh1, wh2 = w.warehouse("W1", "One"), w.warehouse("W2", "Two")
    a = w.product("A", "A", conversions=[])
    w.receive(wh1, a, 50)
    w.receive(wh2, a, 50)
    keeper = make_user(client, "STOREKEEPER", [{"warehouse_id": wh1, "can_view": True, "can_transact": True}], "keeper")
    data = xlsx_bytes({"S": flat([["S1", "2026-01-05", "A", 1, "PC", "", "", "V2"]])})
    assert upload(client, data, wh=wh2, c=keeper).status_code == 403          # no access to W2
    r = upload(client, data, wh=wh1, c=keeper)
    assert r.status_code == 201
    opts = {"warehouse_by": "salesman", "warehouse_map": {"V2": wh2}}
    s = keeper.post(f"/api/imports/{r.json()['batch_id']}/process", json={"mapping": MAP, "options": opts}).json()["summary"]
    assert s["error_codes"] == {"WAREHOUSE_NOT_ALLOWED": 1}
    viewer = make_user(client, "VIEWER", [{"warehouse_id": wh1, "can_view": True, "can_transact": True}], "viewer")
    assert upload(client, data, wh=wh1, c=viewer).status_code == 403
    sales = make_user(client, "SALES", [{"warehouse_id": wh1, "can_view": True, "can_transact": True}], "sales")
    assert upload(client, data, wh=wh1, doc_type="RECEIVING", c=sales).status_code == 403   # SALES may not import receipts
    assert upload(client, data, wh=wh1, c=sales).status_code == 201
    other = make_user(client, "STOREKEEPER", [{"warehouse_id": wh2, "can_view": True, "can_transact": True}], "other")
    assert other.get(f"/api/imports/{r.json()['batch_id']}").status_code == 403       # cannot see W1's batches
    anon = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(__import__("app.main", fromlist=["app"]).app)
    assert anon.post("/api/imports", data={"warehouse_id": wh1, "doc_type": "SALE"}).status_code == 401


# ---------------------------------------------------------------- templates, error export, history, cancel


def test_template_is_saved_and_suggested_for_the_next_file(client, shop):
    wh = shop["wh"]
    data = xlsx_bytes({"S": flat([["S1", "2026-01-05", "A", 1, "PC", "C1", "N", "V"]])})
    bid = upload(client, data, wh=wh).json()["batch_id"]
    p = process(client, bid, MAP, {"default_unit": "PC", "doc_type_rules": [{"prefix": "CN", "doc_type": "CUSTOMER_RETURN"}]},
                save_template_as="My POS")
    tid = p.json()["template_id"]
    assert tid
    tpl = client.get("/api/import-templates").json()
    assert len(tpl) == 1 and tpl[0]["mapping"] == MAP and tpl[0]["settings"]["doc_type_rules"][0]["prefix"] == "CN"
    nxt = upload(client, xlsx_bytes({"S": flat([["S9", "2026-02-01", "B", 2, "PC", "C1", "N", "V"]])}), wh=wh).json()
    assert nxt["suggested_template_id"] == tid
    unrelated = upload(client, xlsx_bytes({"S": [["foo", "bar"], [1, 2]]}), wh=wh).json()
    assert unrelated["suggested_template_id"] is None
    assert client.delete(f"/api/import-templates/{tid}").status_code == 200
    assert client.get("/api/import-templates").json() == []


def test_error_export_can_be_fixed_and_reimported(client, shop, w):
    wh = shop["wh"]
    rows = [["S1", "2026-01-05", "A", 1, "PC", "", "", ""],
            ["S2", "2026-01-05", "NEWPROD", 3, "PC", "", "", ""],
            ["S3", "2026-01-05", "A", 1, "PC", "", "", ""],
            ["S3", "2026-01-05", "ZZZ", 1, "PC", "", "", ""]]
    bid, s = run(client, rows, wh)
    resp = client.get(f"/api/imports/{bid}/errors.xlsx")
    assert resp.status_code == 200 and "spreadsheetml" in resp.headers["content-type"]
    ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
    data = list(ws.iter_rows(values_only=True))
    assert list(data[0][:4]) == ["ROW_NO", "STATUS", "ERROR_CODE", "ERROR_DETAIL"] and list(data[0][4:]) == HEAD
    assert [(r[0], r[1], r[2]) for r in data[1:]] == [(3, "ERROR", "PRODUCT_NOT_FOUND"), (4, "BLOCKED", "DOC_HAS_ERRORS"),
                                                      (5, "ERROR", "PRODUCT_NOT_FOUND")]
    assert data[1][4:] == tuple(None if v == "" else v for v in rows[1])     # original cells preserved, in order
    # fix: register the missing products, then re-import the exported file as is
    w.product("NEWPROD", "New", conversions=[])
    w.product("ZZZ", "Zed", conversions=[])
    w.receive(wh, 3, 50)
    w.receive(wh, 4, 50)
    r2 = upload(client, resp.content, "fixed.xlsx", wh).json()
    s2 = process(client, r2["batch_id"], MAP).json()["summary"]
    assert s2["rows"]["valid"] == 3 and s2["docs"]["ready"] == 2 and not s2["error_codes"]
    assert confirm(client, r2["batch_id"]).status_code == 200
    # S1 was imported in neither run, so the three documents together are now complete
    bid3, s3 = run(client, rows[:1], wh)
    assert confirm(client, bid3).status_code == 200


def test_history_and_cancel(client, shop):
    wh = shop["wh"]
    bid, _ = run(client, [["S1", "2026-01-05", "A", 1, "PC", "", "", ""]], wh)
    assert client.post(f"/api/imports/{bid}/cancel").status_code == 200
    assert client.get(f"/api/imports/{bid}").json()["status"] == "CANCELLED"
    assert client.post(f"/api/imports/{bid}/cancel").status_code == 409
    assert confirm(client, bid).status_code == 409
    assert process(client, bid, MAP).status_code == 409
    assert client.get(f"/api/imports/{bid}/preview").status_code == 404            # uploaded file was removed
    bid2, _ = run(client, [["S2", "2026-01-05", "A", 1, "PC", "", "", ""]], wh)
    confirm(client, bid2)
    hist = client.get("/api/imports").json()
    assert [h["status"] for h in hist] == ["CONFIRMED", "CANCELLED"] and hist[0]["documents_posted"] == 1


def test_opening_stock_from_a_simple_list_without_doc_no_or_date(client, w):
    wh = w.warehouse()
    a, b = w.product("A", "A", conversions=[{"unit_code": "CT", "factor_to_base": 12}]), w.product("B", "B", conversions=[])
    head = ["PRODUCT_CODE", "QTY", "UNIT"]
    m = {"product_code": "PRODUCT_CODE", "qty": "QTY", "unit_code": "UNIT"}
    bid, s = run(client, [["A", 10, "CT"], ["B", 7, "PC"]], wh, m,
                 {"default_doc_no": "OPEN-2026", "default_date": "2026-01-01"}, head, doc_type="RECEIVING")
    assert s["docs"]["ready"] == 1 and s["qty_in"] == 127
    assert confirm(client, bid).status_code == 200
    assert (w.balance(wh, a), w.balance(wh, b)) == (120, 7)
    doc = client.get("/api/documents", params={"doc_type": "RECEIVING"}).json()[0]
    assert doc["doc_no"] == "OPEN-2026" and str(doc["doc_date"]) == "2026-01-01"
    # mapping without a doc number or default is refused
    r = upload(client, xlsx_bytes({"S": flat([["A", 1]], ["PRODUCT_CODE", "QTY"])}), wh=wh, doc_type="RECEIVING").json()
    p = process(client, r["batch_id"], {"product_code": "PRODUCT_CODE", "qty": "QTY"})
    assert p.status_code == 422 and p.json()["error"]["code"] == "BAD_IMPORT_PARAMS"


# ---------------------------------------------------------------- the real sample file


REAL = "/mnt/user-data/attachments/e9ddf27b-3981-5d55-af7d-a042deab06d0/e01f5c5f-Export_28.xls"


@pytest.mark.skipif(not os.path.exists(REAL), reason="real sample file not available")
def test_real_sales_export_end_to_end(client, w):
    data = open(REAL, "rb").read()
    grid, _ = readers.read_grid(data, "html", None)
    H = grid[0]
    ix = {h: i for i, h in enumerate(H)}
    prods = {}
    sold = {}
    for r in grid[1:]:
        prods[r[ix["PRODUCT_CODE"]]] = r[ix["PRODUCT_NAME"]]
        sold[r[ix["PRODUCT_CODE"]]] = sold.get(r[ix["PRODUCT_CODE"]], 0) + r[ix["VALUE"]]
    assert len(prods) == 148
    wh = w.warehouse("HHX", "HHX main")
    for code, name in prods.items():
        w.product(code, str(name)[:200], conversions=[])
    # opening stock through the import engine itself (RECEIVING, one document)
    opening = flat([[c, max(sold[c], 0) + 1000] for c in prods], ["PRODUCT_CODE", "QTY"])
    r = upload(client, xlsx_bytes({"Opening": opening}), "opening.xlsx", wh, "RECEIVING").json()
    s = process(client, r["batch_id"], {"product_code": "PRODUCT_CODE", "qty": "QTY"},
                {"default_doc_no": "OPENING-2026", "default_date": "2026-11-01"}).json()["summary"]
    assert s["rows"]["valid"] == 148 and confirm(client, r["batch_id"]).status_code == 200
    before = {x["product_code"]: x["qty_on_hand"] for x in client.get("/api/stock/balances", params={"warehouse_id": wh, "limit": 500}).json()}

    up = upload(client, data, "Export_28.xls", wh).json()
    assert up["file_kind"] == "html" and up["sheet"] == "Pivot"
    sm = up["suggested_mapping"]
    assert sm == {"doc_no": "ORDERID", "doc_date": "DATE", "product_code": "PRODUCT_CODE", "qty": "VALUE",
                  "customer_code": "CUSTOMER", "customer_name": "CUSTOMER_NAME", "salesman": "SALESMAN"}
    opts = {"doc_type_rules": [{"prefix": "CN", "doc_type": "CUSTOMER_RETURN"}]}
    s = process(client, up["batch_id"], sm, opts, save_template_as="Distributor pivot export").json()["summary"]
    assert s["rows"] == {"total": 1517, "valid": 1517, "error": 0, "blocked": 0, "duplicate": 0}
    assert s["docs"] == {"ready": 570, "blocked": 0, "duplicate": 0, "by_type": {"SALE": 561, "CUSTOMER_RETURN": 9}}
    assert (s["qty_out"], s["qty_in"]) == (531_895, 17_738) and s["new_customers"] == 339
    assert set(s["salesmen"]) == {"Adminsale", "VAN03", "VAN04", "VAN05", "VAN08", "VAN10"}
    after_validate = {x["product_code"]: x["qty_on_hand"] for x in client.get("/api/stock/balances", params={"warehouse_id": wh, "limit": 500}).json()}
    assert after_validate == before                                          # still nothing moved

    c = confirm(client, up["batch_id"], key="real-1")
    assert c.status_code == 200, c.text
    assert c.json()["result"] == {"documents_posted": 570, "rows_posted": 1517, "rows_skipped": 0, "customers_created": 339}
    after = {x["product_code"]: x["qty_on_hand"] for x in client.get("/api/stock/balances", params={"warehouse_id": wh, "limit": 500}).json()}
    assert all(after[c] == before[c] - sold[c] for c in prods)               # every product: opening - net sold
    assert sum(before.values()) - sum(after.values()) == 514_157
    assert q("SELECT count(*) AS n FROM stock_documents WHERE source='IMPORT' AND doc_type<>'RECEIVING'")[0]["n"] == 570
    assert q("SELECT count(*) AS n FROM customers")[0]["n"] == 339
    assert_ledger_consistent()
    # importing the very same file again is recognised and moves nothing
    up2 = upload(client, data, "Export_28.xls", wh).json()
    assert up2["already_imported_batch_id"] == up["batch_id"] and up2["suggested_template_id"]
    s2 = process(client, up2["batch_id"], sm, opts).json()["summary"]
    assert s2["rows"]["duplicate"] == 1517 and s2["docs"]["duplicate"] == 570
    after2 = {x["product_code"]: x["qty_on_hand"] for x in client.get("/api/stock/balances", params={"warehouse_id": wh, "limit": 500}).json()}
    assert after2 == after

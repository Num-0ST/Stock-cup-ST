"""Filling document lines from an Excel / CSV file."""
import io

import pytest
from openpyxl import Workbook, load_workbook


@pytest.fixture()
def shop(client, w):
    wh = w.warehouse("HHX", "Main")
    p1 = w.product("P00061", "แก้ว 16 ออนซ์")                       # CT=24, PK=6
    p2 = w.product("P00099", "ฝาโดม", conversions=[{"unit_code": "CT", "factor_to_base": 1000, "barcode": "8850001112223"}])
    p3 = w.product("P00120", "หลอดดูด", conversions=[])
    return {"wh": wh, "p1": p1, "p2": p2, "p3": p3, "w": w}


def xlsx(rows, sheets=None):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    for name, extra in (sheets or {}).items():
        s2 = wb.create_sheet(name)
        for r in extra:
            s2.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse(client, data, name="items.xlsx"):
    return client.post("/api/document-lines/parse", files={"file": (name, io.BytesIO(data), "application/octet-stream")})


def test_header_file_with_units_duplicates_and_problems(client, shop):
    data = xlsx([
        ["ยอดยกมา ณ 31/10/2025"], [],
        ["ลำดับ", "รหัสสินค้า", "ชื่อสินค้า", "จำนวน", "หน่วย", "จำนวนเงิน"],
        [1, "P00061", "แก้ว", 10, "ลัง", 4500],
        [2, "p00061", "แก้ว", 3, "PC", 30],                 # other unit -> separate line
        [3, 8850001112223, "ฝาโดม", 2, None, 1600],          # unit barcode as a number -> CT
        [4, "P00120", "หลอดดูด", "1,500", "ชิ้น", 0],
        [5, "P00061", "แก้ว", 5, "CT", 0],                  # same product+unit -> merged
        [6, "ZZZ", "ไม่มีในระบบ", 7, "PC", 0],
        [7, "P00120", "หลอดดูด", 0, "PC", 0],               # zero -> skipped
        [8, "P00120", "หลอดดูด", 2, "ลัง", 0],              # unit not set up
        [9, "P00061", "แก้ว", "abc", "PC", 0],
        [10, "P00061", "แก้ว", "0.1", "PK", 0],             # 0.6 PC -> not whole
    ])
    r = parse(client, data)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["header_row"] == 3 and d["columns"]["qty"] == "จำนวน" and d["columns"]["unit"] == "หน่วย"
    got = [(x["product"]["product_code"], x["unit_code"], x["qty"]) for x in d["lines"]]
    assert got == [("P00061", "CT", "15"), ("P00061", "PC", "3"), ("P00099", "CT", "2"), ("P00120", "PC", "1500")]
    assert d["lines"][0]["rows"] == [4, 8] and d["merged"] == 1
    assert d["unmatched"] == [{"row": 9, "code": "ZZZ", "name": "ไม่มีในระบบ", "qty": "7"}]
    assert {(e["row"], e["code"]) for e in d["errors"]} == {(11, "UNIT_NOT_SET"), (12, "BAD_QTY"), (13, "NOT_WHOLE_PC")}
    assert d["skipped_empty"] == 1
    # the lines go straight into a document
    items = [{"product_id": x["product"]["product_id"], "unit_code": x["unit_code"], "qty": x["qty"]} for x in d["lines"]]
    doc = shop["w"].doc("RECEIVING", shop["wh"], items)
    assert doc.status_code == 201 and sum(i["base_qty"] for i in doc.json()["document"]["items"]) == 15 * 24 + 3 + 2000 + 1500


def test_unit_columns_and_names(client, shop):
    data = xlsx([["สินค้า", "ลัง", "แพ็ค", "ชิ้น"],
                 ["แก้ว 16 ออนซ์", 2, 1, 3],          # matched by exact name; 48+6+3
                 ["ฝาโดม", 1, None, 5]])
    d = parse(client, data).json()
    assert [(x["product"]["product_code"], x["unit_code"], x["qty"]) for x in d["lines"]] == [("P00061", "PC", "57"), ("P00099", "PC", "1005")]
    assert d["errors"] == []


def test_no_header_csv_and_best_sheet(client, shop):
    csv = "P00061,แก้ว,12\nP00120,หลอด,300\n".encode("utf-8")
    d = parse(client, csv, "items.csv").json()
    assert [(x["product"]["product_code"], x["qty"]) for x in d["lines"]] == [("P00061", "12"), ("P00120", "300")]
    data = xlsx([["สรุป"], ["ไม่มีข้อมูล"]], sheets={"ยอดยกมา": [["Item code", "Qty"], ["P00099", 4]]})
    d = parse(client, data).json()
    assert d["sheet"] == "ยอดยกมา" and d["lines"][0]["qty"] == "4"


def test_bad_files_and_permissions(client, shop):
    from test_stock_engine import make_user
    assert parse(client, b"not a spreadsheet at all", "x.pdf").status_code == 422
    r = parse(client, xlsx([["a", "b"], ["x", "y"]]))
    assert r.status_code == 422 and r.json()["error"]["code_hint"] == "NO_COLUMNS"
    viewer = make_user(client, "VIEWER", [{"warehouse_id": shop["wh"]}], "viewer")
    assert viewer.post("/api/document-lines/parse", files={"file": ("a.csv", io.BytesIO(b"P00061,1"), "text/csv")}).status_code == 403


def test_template(client, shop):
    r = client.get("/api/document-lines/template")
    assert r.status_code == 200
    ws = load_workbook(io.BytesIO(r.content)).active
    rows = list(ws.values)
    assert rows[0] == ("รหัสสินค้า", "ชื่อสินค้า", "จำนวน", "หน่วย") and [x[0] for x in rows[1:]] == ["P00061", "P00099", "P00120"]
    # a filled-in template reads back
    ws["C2"] = 5
    buf = io.BytesIO(); ws.parent.save(buf)
    d = parse(client, buf.getvalue()).json()
    assert [(x["product"]["product_code"], x["qty"]) for x in d["lines"]] == [("P00061", "5")] and d["skipped_empty"] == 2

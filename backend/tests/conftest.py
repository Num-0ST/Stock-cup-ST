import os
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql://postgres@/wms_test?host=/tmp&port=5433")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789-abcdefghij")
os.environ["ADMIN_PASSWORD"] = "admin-pass-123"

SCHEMA = (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text()

from app.main import app  # noqa: E402
from app import db  # noqa: E402


def reset_db():
    db.close_pool()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute(SCHEMA)


@pytest.fixture()
def client():
    reset_db()
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-123"})
        assert r.status_code == 200, r.text
        c.headers["Authorization"] = "Bearer " + r.json()["token"]
        yield c
    db.close_pool()


class World:
    """Small helper to build master data through the real API."""

    def __init__(self, client):
        self.c = client

    def warehouse(self, code="WH1", name="Main"):
        r = self.c.post("/api/warehouses", json={"warehouse_code": code, "name": name})
        assert r.status_code == 201, r.text
        return r.json()["warehouse_id"]

    def product(self, code="P1", name="Product", conversions=None, barcode=None):
        r = self.c.post("/api/products", json={
            "product_code": code, "name": name, "barcode": barcode,
            "conversions": conversions if conversions is not None else [
                {"unit_code": "CT", "factor_to_base": 24}, {"unit_code": "PK", "factor_to_base": 6}]})
        assert r.status_code == 201, r.text
        return r.json()["product_id"]

    def doc(self, doc_type, wh, items, post=True, key=None, **extra):
        headers = {"Idempotency-Key": key} if key else {}
        return self.c.post("/api/documents", json={
            "doc_type": doc_type, "warehouse_id": wh, "items": items, "post": post, **extra}, headers=headers)

    def receive(self, wh, pid, qty, unit="PC"):
        r = self.doc("RECEIVING", wh, [{"product_id": pid, "unit_code": unit, "qty": qty}])
        assert r.status_code == 201, r.text
        return r.json()["document_id"]

    def balance(self, wh, pid):
        rows = self.c.get("/api/stock/balances", params={"warehouse_id": wh}).json()
        for r in rows:
            if r["product_id"] == pid:
                return r["qty_on_hand"]
        return 0


@pytest.fixture()
def w(client):
    return World(client)

from __future__ import annotations

import hashlib
import json
import ipaddress
import os
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

import jwt
import psycopg
from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import billocr, billscan, deposits, linefile, engine, importer, readers, reports, transfers
from .migrate import run_migrations
from .auth import (check_password_policy, decode_token_full, hash_password, make_token, verify_password)
from .db import close_pool, get_conn, get_pool
from .engine import StockError, audit
from psycopg.types.json import Jsonb


class Forbidden(StockError):
    status, code = 403, "FORBIDDEN"


class Unauthorized(StockError):
    status, code = 401, "UNAUTHORIZED"


# ------------------------------------------------------------------ startup


def seed_admin() -> None:
    """Create the first admin. Several workers start at once, so serialise with an advisory lock."""
    pw = os.environ.get("ADMIN_PASSWORD")
    with get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(727001)")
            n = conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"]
            if n == 0:
                if not pw or len(pw) < 8:
                    raise RuntimeError("ADMIN_PASSWORD (min 8 chars) is required to create the first admin user")
                role = conn.execute("SELECT role_id FROM roles WHERE role_code='ADMIN'").fetchone()["role_id"]
                conn.execute(
                    "INSERT INTO users (username, password_hash, full_name, role_id) VALUES ('admin', %s, 'Administrator', %s) "
                    "ON CONFLICT (username) DO NOTHING",
                    (hash_password(pw), role),
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    seed_admin()
    yield
    close_pool()


app = FastAPI(title="Multi-Warehouse Inventory", version="0.2.0", lifespan=lifespan)


MAX_BODY_BYTES = 30 * 1024 * 1024     # biggest legitimate upload is a 25 MB Excel file


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """Refuse oversized uploads before they are spooled to disk (protects the server's disk and memory)."""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        return JSONResponse({"error": {"code": "PAYLOAD_TOO_LARGE", "message": "ไฟล์หรือข้อมูลใหญ่เกินไป"}}, status_code=413)
    if request.method in ("POST", "PUT", "PATCH") and not cl and request.headers.get("transfer-encoding"):
        return JSONResponse({"error": {"code": "LENGTH_REQUIRED", "message": "ต้องระบุขนาดข้อมูล"}}, status_code=411)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    elif request.url.path == "/" or request.url.path.startswith("/static/"):
        # always revalidate (cheap 304 via ETag) so a new version shows up right after an update
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.exception_handler(StockError)
async def stock_error_handler(_: Request, exc: StockError):
    return JSONResponse({"error": exc.as_dict()}, status_code=exc.status)


@app.exception_handler(psycopg.errors.CheckViolation)
async def check_violation_handler(_: Request, exc: psycopg.errors.CheckViolation):
    msg = str(exc)
    if "INSUFFICIENT_STOCK" in msg:
        return JSONResponse({"error": {"code": "INSUFFICIENT_STOCK", "message": "สต็อกไม่พอ"}}, status_code=409)
    return JSONResponse({"error": {"code": "CONSTRAINT_VIOLATION", "message": "ข้อมูลไม่ผ่านเงื่อนไขของระบบ"}},
                        status_code=422)


@app.exception_handler(psycopg.errors.UniqueViolation)
async def unique_violation_handler(_: Request, exc: psycopg.errors.UniqueViolation):
    return JSONResponse({"error": {"code": "DUPLICATE", "message": "ข้อมูลซ้ำกับที่มีอยู่แล้ว"}}, status_code=409)


@app.exception_handler(psycopg.errors.ForeignKeyViolation)
async def fk_violation_handler(_: Request, exc: psycopg.errors.ForeignKeyViolation):
    return JSONResponse({"error": {"code": "INVALID_REFERENCE", "message": "ข้อมูลอ้างอิงไม่ถูกต้อง"}}, status_code=422)


# ------------------------------------------------------------------ auth & permissions


def current_user(authorization: Optional[str] = Header(None), conn=Depends(get_conn)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("กรุณาเข้าสู่ระบบ")
    try:
        uid, iat = decode_token_full(authorization.split(" ", 1)[1])
    except jwt.PyJWTError:
        raise Unauthorized("เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่")
    u = conn.execute(
        "SELECT u.*, r.role_code FROM users u JOIN roles r USING (role_id) WHERE u.user_id=%s", (uid,)
    ).fetchone()
    if not u or not u["is_active"]:
        raise Unauthorized("บัญชีนี้ใช้งานไม่ได้")
    if u["password_changed_at"] and iat < int(u["password_changed_at"].timestamp()):
        raise Unauthorized("รหัสผ่านถูกเปลี่ยนแล้ว กรุณาเข้าสู่ระบบใหม่")
    rows = conn.execute("SELECT * FROM user_warehouse_access WHERE user_id=%s", (uid,)).fetchall()
    u["access"] = {r["warehouse_id"]: r for r in rows}
    return u


def is_admin(u: dict) -> bool:
    return u["role_code"] == "ADMIN"


def viewable_warehouses(conn, u: dict) -> list[int]:
    if is_admin(u):
        return [r["warehouse_id"] for r in conn.execute("SELECT warehouse_id FROM warehouses").fetchall()]
    return [w for w, a in u["access"].items() if a["can_view"]]


def require_wh(u: dict, warehouse_id: int, flag: str) -> None:
    if is_admin(u):
        return
    a = u["access"].get(warehouse_id)
    if not a or not a[flag]:
        raise Forbidden("คุณไม่มีสิทธิ์ในคลังสินค้านี้")


def require_role(u: dict, *roles: str) -> None:
    if u["role_code"] not in roles and not is_admin(u):
        raise Forbidden("คุณไม่มีสิทธิ์ทำรายการนี้")


def require_doc_write(u: dict, doc_type: str, warehouse_id: int) -> None:
    if u["role_code"] == "VIEWER":
        raise Forbidden("บัญชีนี้ดูข้อมูลได้อย่างเดียว")
    if u["role_code"] == "SALES" and doc_type not in ("SALE", "CUSTOMER_RETURN"):
        raise Forbidden("ฝ่ายขายทำได้เฉพาะใบขายและรับคืนจากลูกค้า")
    if doc_type == "ADJUSTMENT":                 # same people who may apply a stock count
        require_role(u, "MANAGER", "STOREKEEPER")
    require_wh(u, warehouse_id, "can_transact")


def clean_page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, 500)), max(0, offset)


# ------------------------------------------------------------------ models


class LoginIn(BaseModel):
    username: str
    password: str


class ItemIn(BaseModel):
    product_id: Optional[int] = None
    product_code: Optional[str] = None
    unit_id: Optional[int] = None
    unit_code: Optional[str] = None
    qty: Decimal
    deposit_qty: Optional[Decimal] = None      # SALE only: part the customer leaves in the warehouse (same unit)
    deposit_qty_base: Optional[int] = Field(None, ge=0)   # ... or the same in PC
    direction: Optional[str] = None            # ADJUSTMENT only: "IN" raises the stock, "OUT" lowers it


class DocumentIn(BaseModel):
    doc_type: str
    doc_no: Optional[str] = None
    doc_date: Optional[date] = None
    warehouse_id: int
    customer_id: Optional[int] = None
    supplier_id: Optional[int] = None
    remark: Optional[str] = None
    adjust_reason: Optional[str] = None        # ADJUSTMENT only: DAMAGED / EXPIRED / LOST / FOUND / RECOUNT / SAMPLE / OTHER
    items: list[ItemIn]
    post: bool = False
    bill_scan_id: Optional[int] = None         # set when the document was created from a scanned bill


class ReverseIn(BaseModel):
    reason: Optional[str] = None


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    parent_id: Optional[int] = None


class CategoryUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    is_active: bool = True


class ConversionIn(BaseModel):
    unit_code: str
    factor_to_base: int = Field(gt=0)
    barcode: Optional[str] = None


class ProductIn(BaseModel):
    product_code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=255)
    barcode: Optional[str] = None
    category_id: Optional[int] = None
    reorder_level: int = Field(default=0, ge=0)
    is_active: bool = True
    conversions: list[ConversionIn] = []


class WarehouseIn(BaseModel):
    warehouse_code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=150)
    is_active: bool = True


class PartyIn(BaseModel):
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=200)
    phone: Optional[str] = None
    is_active: bool = True


class WhAccessIn(BaseModel):
    warehouse_id: int
    can_view: bool = True
    can_transact: bool = False
    can_approve: bool = False


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8)
    full_name: str
    email: Optional[str] = None
    role_code: str
    warehouses: list[WhAccessIn] = []


class UserUpdateIn(BaseModel):
    full_name: Optional[str] = None
    password: Optional[str] = Field(default=None, min_length=8)
    role_code: Optional[str] = None
    is_active: Optional[bool] = None
    warehouses: Optional[list[WhAccessIn]] = None


class CountIn(BaseModel):
    warehouse_id: int
    product_id: int
    counted_qty: int = Field(ge=0)
    count_date: Optional[date] = None


class ApplyCountIn(BaseModel):
    reason: Optional[str] = None


# ------------------------------------------------------------------ public


@app.get("/api/health")
def health():
    return {"status": "ok"}


MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
MAX_FAILED_PER_IP = 30          # failed logins from one address within IP_WINDOW_MINUTES (any usernames)
IP_WINDOW_MINUTES = 15
# compared against when the username does not exist, so a wrong username takes as long as a wrong password
_DUMMY_HASH = hash_password("not-a-real-password-0")


class TooManyAttempts(StockError):
    status, code = 429, "ACCOUNT_LOCKED"


_DOCKER_NET = ipaddress.ip_network("172.16.0.0/12")


def _client_ip(request: Request) -> Optional[str]:
    """Address of the real visitor. Behind the Cloudflare tunnel every request arrives from the tunnel container
    (a Docker address), so the visitor's address is taken from CF-Connecting-IP - but only then, never from
    a client that connects directly."""
    host = request.client.host if request.client else None
    try:
        peer = ipaddress.ip_address(host) if host else None
    except ValueError:
        return None
    if peer is not None and peer in _DOCKER_NET:
        fwd = (request.headers.get("cf-connecting-ip") or "").strip()
        try:
            if fwd:
                return str(ipaddress.ip_address(fwd))
        except ValueError:
            pass
    return str(peer) if peer else None


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, conn=Depends(get_conn)):
    ip = _client_ip(request)
    if ip:
        n = conn.execute(
            """SELECT count(*) AS n FROM audit_logs WHERE ip_address=%s AND action IN ('LOGIN_FAILED','ACCOUNT_LOCKED','LOGIN_BLOCKED')
               AND occurred_at > now() - make_interval(mins => %s)""", (ip, IP_WINDOW_MINUTES)).fetchone()["n"]
        if n >= MAX_FAILED_PER_IP:
            raise TooManyAttempts(f"ลองเข้าสู่ระบบผิดหลายครั้งจากเครื่องนี้ กรุณารอ {IP_WINDOW_MINUTES} นาที",
                                  minutes=IP_WINDOW_MINUTES, code_hint="IP_THROTTLED")
    u = conn.execute("SELECT * FROM users WHERE username=%s", (body.username,)).fetchone()
    if u and u["locked_until"] and u["locked_until"] > conn.execute("SELECT now() AS n").fetchone()["n"]:
        audit(conn.cursor(), u["user_id"], "LOGIN_BLOCKED", "user", u["user_id"], ip=ip)
        raise TooManyAttempts(f"บัญชีถูกล็อกชั่วคราว (ลองผิดหลายครั้ง) กรุณารอ {LOCK_MINUTES} นาที หรือให้ผู้ดูแลปลดล็อก",
                              minutes=LOCK_MINUTES)
    password_ok = verify_password(body.password, u["password_hash"] if u else _DUMMY_HASH)
    if not u or not u["is_active"] or not password_ok:
        if u:
            # separate autocommit statements so the failure is recorded even though we raise
            row = conn.execute(
                """UPDATE users SET failed_logins = failed_logins + 1,
                          locked_until = CASE WHEN failed_logins + 1 >= %s THEN now() + make_interval(mins => %s) ELSE locked_until END
                   WHERE user_id=%s RETURNING failed_logins, locked_until""", (MAX_FAILED_LOGINS, LOCK_MINUTES, u["user_id"])).fetchone()
            locked = row["failed_logins"] >= MAX_FAILED_LOGINS
            if locked:
                conn.execute("UPDATE users SET failed_logins=0 WHERE user_id=%s", (u["user_id"],))
            audit(conn.cursor(), u["user_id"], "ACCOUNT_LOCKED" if locked else "LOGIN_FAILED", "user", u["user_id"], ip=ip)
        else:
            audit(conn.cursor(), None, "LOGIN_FAILED", "user", body.username[:50], ip=ip)
        raise Unauthorized("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
    with conn.transaction():
        conn.execute("UPDATE users SET last_login_at=now(), failed_logins=0, locked_until=NULL WHERE user_id=%s", (u["user_id"],))
        audit(conn.cursor(), u["user_id"], "LOGIN", "user", u["user_id"], ip=ip)
    return {"token": make_token(u["user_id"])}


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/me/password")
def change_password(body: PasswordChangeIn, u=Depends(current_user), conn=Depends(get_conn)):
    if not verify_password(body.current_password, u["password_hash"]):
        raise engine.ValidationFailed("รหัสผ่านปัจจุบันไม่ถูกต้อง", field="current_password", code_hint="BAD_CURRENT_PASSWORD")
    err = check_password_policy(body.new_password, u["username"])
    if err:
        raise engine.ValidationFailed(err, field="new_password", code_hint="WEAK_PASSWORD")
    if body.new_password == body.current_password:
        raise engine.ValidationFailed("รหัสผ่านใหม่ต้องต่างจากรหัสผ่านเดิม", field="new_password", code_hint="SAME_PASSWORD")
    with conn.transaction():
        conn.execute("UPDATE users SET password_hash=%s, password_changed_at=now() WHERE user_id=%s",
                     (hash_password(body.new_password), u["user_id"]))
        audit(conn.cursor(), u["user_id"], "CHANGE_PASSWORD", "user", u["user_id"])
    return {"token": make_token(u["user_id"])}                 # older tokens are now rejected


@app.post("/api/users/{user_id}/unlock")
def unlock_user(user_id: int, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "ADMIN")
    with conn.transaction():
        n = conn.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE user_id=%s", (user_id,)).rowcount
        if not n:
            raise engine.NotFound("ไม่พบผู้ใช้")
        audit(conn.cursor(), u["user_id"], "UNLOCK_USER", "user", user_id)
    return {"ok": True}


@app.get("/api/me")
def me(u=Depends(current_user), conn=Depends(get_conn)):
    return {
        "user_id": u["user_id"], "username": u["username"], "full_name": u["full_name"],
        "role_code": u["role_code"],
        "warehouses": viewable_warehouses(conn, u),
        "transact_warehouses": [w for w in viewable_warehouses(conn, u)
                                if is_admin(u) or u["access"].get(w, {}).get("can_transact")],
    }


# ------------------------------------------------------------------ master data


@app.get("/api/units")
def units(u=Depends(current_user), conn=Depends(get_conn)):
    return conn.execute("SELECT * FROM units ORDER BY is_base DESC, unit_code").fetchall()


@app.get("/api/categories")
def categories(u=Depends(current_user), conn=Depends(get_conn)):
    return conn.execute("""SELECT c.*, (SELECT count(*) FROM products p WHERE p.category_id = c.category_id) AS product_count
                           FROM categories c ORDER BY c.name""").fetchall()


def _category_name(conn, name: str, skip_id: Optional[int] = None) -> str:
    name = " ".join(name.split())
    if not name:
        raise engine.ValidationFailed("กรุณาระบุชื่อหมวด", field="name")
    if conn.execute("SELECT 1 FROM categories WHERE lower(name)=lower(%s) AND category_id IS DISTINCT FROM %s",
                    (name, skip_id)).fetchone():
        raise engine.ValidationFailed(f"มีหมวด {name} อยู่แล้ว", field="name", code_hint="DUPLICATE")
    return name


@app.post("/api/categories", status_code=201)
def create_category(body: CategoryIn, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "MANAGER", "STOREKEEPER")            # same people who may edit products
    name = _category_name(conn, body.name)
    row = conn.execute("INSERT INTO categories (name, parent_id) VALUES (%s,%s) RETURNING *", (name, body.parent_id)).fetchone()
    audit(conn.cursor(), u["user_id"], "CREATE_CATEGORY", "category", row["category_id"], new={"name": name})
    return row


@app.put("/api/categories/{category_id}")
def update_category(category_id: int, body: CategoryUpdate, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "MANAGER", "STOREKEEPER")
    old = conn.execute("SELECT * FROM categories WHERE category_id=%s", (category_id,)).fetchone()
    if not old:
        raise engine.NotFound("ไม่พบหมวดสินค้า")
    name = _category_name(conn, body.name, category_id)
    row = conn.execute("UPDATE categories SET name=%s, is_active=%s WHERE category_id=%s RETURNING *",
                       (name, body.is_active, category_id)).fetchone()
    audit(conn.cursor(), u["user_id"], "UPDATE_CATEGORY", "category", category_id,
          old={"name": old["name"], "is_active": old["is_active"]}, new={"name": name, "is_active": body.is_active})
    return row


@app.get("/api/warehouses")
def warehouses(u=Depends(current_user), conn=Depends(get_conn)):
    return conn.execute(
        "SELECT * FROM warehouses WHERE warehouse_id = ANY(%s) AND warehouse_type='PHYSICAL' ORDER BY warehouse_code",
        (viewable_warehouses(conn, u),)).fetchall()


@app.post("/api/warehouses", status_code=201)
def create_warehouse(body: WarehouseIn, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "ADMIN")
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO warehouses (warehouse_code, name, is_active) VALUES (%s,%s,%s) RETURNING *",
            (body.warehouse_code.strip(), body.name, body.is_active)).fetchone()
        audit(conn.cursor(), u["user_id"], "CREATE_WAREHOUSE", "warehouse", row["warehouse_id"], row["warehouse_id"])
    return row


@app.put("/api/warehouses/{warehouse_id}")
def update_warehouse(warehouse_id: int, body: WarehouseIn, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "ADMIN")
    row = conn.execute(
        """UPDATE warehouses SET warehouse_code=%s, name=%s, is_active=%s
           WHERE warehouse_id=%s AND warehouse_type='PHYSICAL' RETURNING *""",
        (body.warehouse_code.strip(), body.name, body.is_active, warehouse_id)).fetchone()
    if not row:
        raise engine.NotFound("ไม่พบคลังสินค้า")
    return row


PRODUCT_SELECT = """
SELECT p.*, c.name AS category_name, bu.unit_code AS base_unit_code,
  COALESCE((SELECT json_agg(json_build_object('unit_code', un.unit_code, 'unit_id', un.unit_id,
            'factor_to_base', pc.factor_to_base, 'barcode', pc.barcode) ORDER BY pc.factor_to_base DESC)
            FROM product_unit_conversions pc JOIN units un USING (unit_id)
            WHERE pc.product_id = p.product_id), '[]'::json) AS conversions
FROM products p
LEFT JOIN categories c USING (category_id)
JOIN units bu ON bu.unit_id = p.base_unit_id
"""


@app.get("/api/products")
def list_products(q: Optional[str] = None, active_only: bool = True, limit: int = 100, offset: int = 0,
                  u=Depends(current_user), conn=Depends(get_conn)):
    limit, offset = clean_page(limit, offset)
    like = f"%{q.strip().lower()}%" if q and q.strip() else None
    return conn.execute(
        PRODUCT_SELECT + """
        WHERE (%(like)s::text IS NULL OR lower(p.product_code) LIKE %(like)s OR lower(p.name) LIKE %(like)s
               OR p.barcode = %(exact)s
               OR EXISTS (SELECT 1 FROM product_unit_conversions pc WHERE pc.product_id = p.product_id AND pc.barcode = %(exact)s))
          AND (NOT %(active)s OR p.is_active)
        ORDER BY p.product_code LIMIT %(limit)s OFFSET %(offset)s""",
        {"like": like, "exact": q.strip() if q else None, "active": active_only, "limit": limit, "offset": offset},
    ).fetchall()


def _save_conversions(conn, product_id: int, base_unit_id: int, items: list[ConversionIn]) -> None:
    seen = set()
    conn.execute("DELETE FROM product_unit_conversions WHERE product_id=%s", (product_id,))
    for c in items:
        code = c.unit_code.upper()
        unit = conn.execute("SELECT unit_id FROM units WHERE unit_code=%s", (code,)).fetchone()
        if not unit:
            raise engine.ValidationFailed(f"ไม่พบหน่วยนับ {c.unit_code}", field="conversions")
        if unit["unit_id"] == base_unit_id:
            continue  # base unit is implicit, factor 1
        if code in seen:
            raise engine.ValidationFailed(f"หน่วย {code} ซ้ำ", field="conversions")
        seen.add(code)
        conn.execute(
            "INSERT INTO product_unit_conversions (product_id, unit_id, factor_to_base, barcode) VALUES (%s,%s,%s,%s)",
            (product_id, unit["unit_id"], c.factor_to_base, (c.barcode or None)))


@app.post("/api/products", status_code=201)
def create_product(body: ProductIn, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "MANAGER", "STOREKEEPER")
    with conn.transaction():
        base = conn.execute("SELECT unit_id FROM units WHERE is_base").fetchone()["unit_id"]
        pid = conn.execute(
            """INSERT INTO products (product_code, barcode, name, category_id, base_unit_id, reorder_level, is_active)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING product_id""",
            (body.product_code.strip(), body.barcode or None, body.name, body.category_id, base,
             body.reorder_level, body.is_active)).fetchone()["product_id"]
        _save_conversions(conn, pid, base, body.conversions)
        audit(conn.cursor(), u["user_id"], "CREATE_PRODUCT", "product", pid)
    return conn.execute(PRODUCT_SELECT + " WHERE p.product_id=%s", (pid,)).fetchone()


@app.put("/api/products/{product_id}")
def update_product(product_id: int, body: ProductIn, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "MANAGER", "STOREKEEPER")
    with conn.transaction():
        row = conn.execute(
            """UPDATE products SET product_code=%s, barcode=%s, name=%s, category_id=%s, reorder_level=%s, is_active=%s
               WHERE product_id=%s RETURNING base_unit_id""",
            (body.product_code.strip(), body.barcode or None, body.name, body.category_id, body.reorder_level,
             body.is_active, product_id)).fetchone()
        if not row:
            raise engine.NotFound("ไม่พบสินค้า")
        # changing conversions never rewrites old documents: each line keeps its own snapshot
        _save_conversions(conn, product_id, row["base_unit_id"], body.conversions)
        audit(conn.cursor(), u["user_id"], "UPDATE_PRODUCT", "product", product_id)
    return conn.execute(PRODUCT_SELECT + " WHERE p.product_id=%s", (product_id,)).fetchone()


def _party_routes(path: str, table: str, id_col: str, code_col: str):
    @app.get(f"/api/{path}", name=f"list_{path}")
    def list_parties(q: Optional[str] = None, u=Depends(current_user), conn=Depends(get_conn)):
        like = f"%{q.strip().lower()}%" if q and q.strip() else None
        return conn.execute(
            f"""SELECT {id_col} AS id, {code_col} AS code, name, phone, is_active FROM {table}
                WHERE (%(like)s::text IS NULL OR lower({code_col}) LIKE %(like)s OR lower(name) LIKE %(like)s)
                ORDER BY name LIMIT 500""", {"like": like}).fetchall()

    @app.post(f"/api/{path}", status_code=201, name=f"create_{path}")
    def create_party(body: PartyIn, u=Depends(current_user), conn=Depends(get_conn)):
        require_role(u, "MANAGER", "SALES", "STOREKEEPER")
        return conn.execute(
            f"""INSERT INTO {table} ({code_col}, name, phone, is_active) VALUES (%s,%s,%s,%s)
                RETURNING {id_col} AS id, {code_col} AS code, name, phone, is_active""",
            (body.code.strip(), body.name, body.phone, body.is_active)).fetchone()

    @app.put(f"/api/{path}/{{item_id}}", name=f"update_{path}")
    def update_party(item_id: int, body: PartyIn, u=Depends(current_user), conn=Depends(get_conn)):
        require_role(u, "MANAGER")
        row = conn.execute(
            f"""UPDATE {table} SET {code_col}=%s, name=%s, phone=%s, is_active=%s WHERE {id_col}=%s
                RETURNING {id_col} AS id, {code_col} AS code, name, phone, is_active""",
            (body.code.strip(), body.name, body.phone, body.is_active, item_id)).fetchone()
        if not row:
            raise engine.NotFound("ไม่พบข้อมูล")
        return row


_party_routes("customers", "customers", "customer_id", "customer_code")
_party_routes("suppliers", "suppliers", "supplier_id", "supplier_code")


# ------------------------------------------------------------------ users (admin)


@app.get("/api/users")
def list_users(u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "ADMIN")
    return conn.execute(
        """SELECT u.user_id, u.username, u.full_name, u.email, u.is_active, r.role_code,
             COALESCE((SELECT json_agg(a) FROM user_warehouse_access a WHERE a.user_id=u.user_id), '[]'::json) AS warehouses
           FROM users u JOIN roles r USING (role_id) ORDER BY u.username""").fetchall()


def _save_access(conn, user_id: int, items: list[WhAccessIn]) -> None:
    conn.execute("DELETE FROM user_warehouse_access WHERE user_id=%s", (user_id,))
    for a in items:
        conn.execute(
            """INSERT INTO user_warehouse_access (user_id, warehouse_id, can_view, can_transact, can_approve)
               VALUES (%s,%s,%s,%s,%s)""", (user_id, a.warehouse_id, a.can_view, a.can_transact, a.can_approve))


@app.post("/api/users", status_code=201)
def create_user(body: UserIn, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "ADMIN")
    role = conn.execute("SELECT role_id FROM roles WHERE role_code=%s", (body.role_code,)).fetchone()
    if not role:
        raise engine.ValidationFailed("ไม่พบบทบาทผู้ใช้", field="role_code")
    err = check_password_policy(body.password, body.username)
    if err:
        raise engine.ValidationFailed(err, field="password", code_hint="WEAK_PASSWORD")
    with conn.transaction():
        uid = conn.execute(
            "INSERT INTO users (username, password_hash, full_name, email, role_id) VALUES (%s,%s,%s,%s,%s) RETURNING user_id",
            (body.username, hash_password(body.password), body.full_name, body.email or None,
             role["role_id"])).fetchone()["user_id"]
        _save_access(conn, uid, body.warehouses)
        audit(conn.cursor(), u["user_id"], "CREATE_USER", "user", uid)
    return {"user_id": uid}


@app.put("/api/users/{user_id}")
def update_user(user_id: int, body: UserUpdateIn, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "ADMIN")
    with conn.transaction():
        cur = conn.execute("SELECT * FROM users WHERE user_id=%s FOR UPDATE", (user_id,)).fetchone()
        if not cur:
            raise engine.NotFound("ไม่พบผู้ใช้")
        if user_id == u["user_id"] and body.is_active is False:
            raise engine.ValidationFailed("ไม่สามารถปิดบัญชีของตัวเองได้")
        if body.role_code:
            role = conn.execute("SELECT role_id FROM roles WHERE role_code=%s", (body.role_code,)).fetchone()
            if not role:
                raise engine.ValidationFailed("ไม่พบบทบาทผู้ใช้", field="role_code")
            conn.execute("UPDATE users SET role_id=%s WHERE user_id=%s", (role["role_id"], user_id))
        if body.full_name:
            conn.execute("UPDATE users SET full_name=%s WHERE user_id=%s", (body.full_name, user_id))
        if body.password:
            err = check_password_policy(body.password, cur["username"])
            if err:
                raise engine.ValidationFailed(err, field="password", code_hint="WEAK_PASSWORD")
            conn.execute("UPDATE users SET password_hash=%s, password_changed_at=now(), failed_logins=0, locked_until=NULL WHERE user_id=%s",
                         (hash_password(body.password), user_id))
        if body.is_active is not None:
            conn.execute("UPDATE users SET is_active=%s WHERE user_id=%s", (body.is_active, user_id))
        if body.warehouses is not None:
            _save_access(conn, user_id, body.warehouses)
        audit(conn.cursor(), u["user_id"], "UPDATE_USER", "user", user_id)
    return {"ok": True}


# ------------------------------------------------------------------ documents


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def document_detail(conn, document_id: int) -> dict | None:
    d = conn.execute(
        """SELECT d.*, w.name AS warehouse_name, w.warehouse_code, c.name AS customer_name, s.name AS supplier_name,
                  cu.full_name AS created_by_name, pu.full_name AS posted_by_name
           FROM stock_documents d JOIN warehouses w USING (warehouse_id)
           LEFT JOIN customers c USING (customer_id) LEFT JOIN suppliers s USING (supplier_id)
           JOIN users cu ON cu.user_id = d.created_by LEFT JOIN users pu ON pu.user_id = d.posted_by
           WHERE d.document_id=%s""", (document_id,)).fetchone()
    if not d:
        return None
    d["items"] = conn.execute(
        """SELECT i.*, p.product_code, p.name AS product_name, un.unit_code
           FROM stock_document_items i JOIN products p USING (product_id) JOIN units un USING (unit_id)
           WHERE i.document_id=%s ORDER BY i.line_no""", (document_id,)).fetchall()
    d["deposits"] = conn.execute(
        """SELECT d.deposit_id, d.deposit_no, d.status, d.deposit_date,
                  sum(di.qty_base - di.qty_delivered_base - di.qty_cancelled_base)::bigint AS qty_remaining_base
           FROM customer_deposits d JOIN customer_deposit_items di USING (deposit_id)
           WHERE d.sale_document_id=%s GROUP BY d.deposit_id ORDER BY d.deposit_id""", (document_id,)).fetchall()
    used = {r["sale_item_id"]: r["q"] for r in conn.execute(
        """SELECT di.sale_item_id, sum(di.qty_base - di.qty_cancelled_base)::bigint AS q
           FROM customer_deposit_items di JOIN customer_deposits d USING (deposit_id)
           WHERE d.sale_document_id=%s GROUP BY di.sale_item_id""", (document_id,)).fetchall()}
    for it in d["items"]:
        it["deposited_base"] = used.get(it["item_id"], 0)
    scan = conn.execute("SELECT scan_id FROM bill_scans WHERE document_id=%s ORDER BY scan_id DESC LIMIT 1",
                        (document_id,)).fetchone()
    d["bill_scan_id"] = scan["scan_id"] if scan else None
    return d


@app.post("/api/documents", status_code=201)
def create_document(body: DocumentIn, idempotency_key: Optional[str] = Header(None),
                    u=Depends(current_user), conn=Depends(get_conn)):
    require_doc_write(u, body.doc_type, body.warehouse_id)
    payload = body.model_dump(mode="json")

    def work() -> int:
        # create + post is one unit: if posting fails (e.g. insufficient stock) no draft is left behind
        with conn.transaction():
            did = engine.create_document(conn, payload, u["user_id"])
            if body.post:
                engine.post_document(conn, did, u["user_id"])
            if body.bill_scan_id:
                conn.execute("UPDATE bill_scans SET document_id=%s, status='USED' WHERE scan_id=%s AND document_id IS NULL",
                             (did, body.bill_scan_id))
            return did

    did, replayed = engine.run_idempotent(conn, idempotency_key, u["user_id"],
                                          "CREATE_POST_DOC" if body.post else "CREATE_DOC", _hash(payload), work)
    return {"document_id": did, "replayed": replayed, "document": document_detail(conn, did)}


@app.get("/api/documents")
def list_documents(doc_type: Optional[str] = None, warehouse_id: Optional[int] = None, status: Optional[str] = None,
                   q: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
                   limit: int = 50, offset: int = 0, u=Depends(current_user), conn=Depends(get_conn)):
    limit, offset = clean_page(limit, offset)
    whs = viewable_warehouses(conn, u)
    if warehouse_id is not None:
        whs = [w for w in whs if w == warehouse_id]
    like = f"%{q.strip().lower()}%" if q and q.strip() else None
    return conn.execute(
        """SELECT d.document_id, d.doc_type, d.doc_no, d.doc_date, d.status, d.warehouse_id, w.name AS warehouse_name,
                  c.name AS customer_name, s.name AS supplier_name, d.source, d.reverses_document_id, d.created_at,
                  cu.full_name AS created_by_name,
                  (SELECT count(*) FROM stock_document_items i WHERE i.document_id = d.document_id) AS line_count
           FROM stock_documents d JOIN warehouses w USING (warehouse_id)
           LEFT JOIN customers c USING (customer_id) LEFT JOIN suppliers s USING (supplier_id)
           JOIN users cu ON cu.user_id = d.created_by
           WHERE d.warehouse_id = ANY(%(whs)s)
             AND (%(doc_type)s::text IS NULL OR d.doc_type = %(doc_type)s)
             AND (%(status)s::text IS NULL OR d.status = %(status)s)
             AND (%(like)s::text IS NULL OR lower(d.doc_no) LIKE %(like)s)
             AND (%(df)s::date IS NULL OR d.doc_date >= %(df)s)
             AND (%(dt)s::date IS NULL OR d.doc_date <= %(dt)s)
           ORDER BY d.document_id DESC LIMIT %(limit)s OFFSET %(offset)s""",
        {"whs": whs, "doc_type": doc_type, "status": status, "like": like, "df": date_from, "dt": date_to,
         "limit": limit, "offset": offset}).fetchall()


def _load_doc_for_action(conn, u, document_id: int, flag: str = "can_transact") -> dict:
    d = conn.execute("SELECT document_id, doc_type, warehouse_id, doc_no FROM stock_documents WHERE document_id=%s",
                     (document_id,)).fetchone()
    if not d:
        raise engine.NotFound("ไม่พบเอกสาร")
    require_wh(u, d["warehouse_id"], "can_view")
    require_doc_write(u, d["doc_type"], d["warehouse_id"])
    return d


@app.get("/api/documents/{document_id}")
def get_document(document_id: int, u=Depends(current_user), conn=Depends(get_conn)):
    d = document_detail(conn, document_id)
    if not d:
        raise engine.NotFound("ไม่พบเอกสาร")
    require_wh(u, d["warehouse_id"], "can_view")
    return d


@app.post("/api/documents/{document_id}/post")
def post_document(document_id: int, idempotency_key: Optional[str] = Header(None),
                  u=Depends(current_user), conn=Depends(get_conn)):
    _load_doc_for_action(conn, u, document_id)
    did, replayed = engine.run_idempotent(
        conn, idempotency_key, u["user_id"], "POST_DOC", _hash({"id": document_id}),
        lambda: engine.post_document(conn, document_id, u["user_id"]))
    return {"document_id": did, "replayed": replayed, "document": document_detail(conn, did)}


@app.post("/api/documents/{document_id}/reverse")
def reverse_document(document_id: int, body: ReverseIn, idempotency_key: Optional[str] = Header(None),
                     u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "MANAGER")
    _load_doc_for_action(conn, u, document_id)
    rev, replayed = engine.run_idempotent(
        conn, idempotency_key, u["user_id"], "REVERSE_DOC", _hash({"id": document_id, "r": body.reason}),
        lambda: engine.reverse_document(conn, document_id, u["user_id"], body.reason))
    return {"reversal_document_id": rev, "replayed": replayed, "document": document_detail(conn, rev)}


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: int, u=Depends(current_user), conn=Depends(get_conn)):
    _load_doc_for_action(conn, u, document_id)
    engine.delete_draft(conn, document_id, u["user_id"])
    return {"ok": True}


# ------------------------------------------------------------------ stock views


@app.get("/api/stock/available")
def stock_available(warehouse_id: int, u=Depends(current_user), conn=Depends(get_conn)):
    """{product_id: qty_on_hand} for one warehouse - used to show stock next to each line while entering a document."""
    require_wh(u, warehouse_id, "can_view")
    rows = conn.execute("SELECT product_id, qty_on_hand FROM stock_balances WHERE warehouse_id=%s AND qty_on_hand <> 0",
                        (warehouse_id,)).fetchall()
    return {str(r["product_id"]): r["qty_on_hand"] for r in rows}


@app.get("/api/stock/balances")
def stock_balances(view: str = Query("by_warehouse", pattern="^(by_warehouse|total)$"),
                   warehouse_id: Optional[int] = None, q: Optional[str] = None, low_only: bool = False,
                   limit: int = 200, offset: int = 0, u=Depends(current_user), conn=Depends(get_conn)):
    limit, offset = clean_page(limit, offset)
    whs = viewable_warehouses(conn, u)
    if warehouse_id is not None:
        whs = [w for w in whs if w == warehouse_id]
    like = f"%{q.strip().lower()}%" if q and q.strip() else None
    params = {"whs": whs, "like": like, "exact": q.strip() if q else None, "low": low_only,
              "limit": limit, "offset": offset}
    conv = """COALESCE((SELECT json_agg(json_build_object('unit_code', un.unit_code, 'factor_to_base', pc.factor_to_base)
                         ORDER BY pc.factor_to_base DESC)
                       FROM product_unit_conversions pc JOIN units un USING (unit_id)
                       WHERE pc.product_id = p.product_id), '[]'::json)"""
    flt = """(%(like)s::text IS NULL OR lower(p.product_code) LIKE %(like)s OR lower(p.name) LIKE %(like)s
              OR p.barcode = %(exact)s)"""
    held = """(SELECT di.product_id, d.warehouse_id,
                     sum(di.qty_base - di.qty_delivered_base - di.qty_cancelled_base)::bigint AS deposit_qty
              FROM customer_deposit_items di JOIN customer_deposits d USING (deposit_id)
              WHERE d.status IN ('OPEN','PARTIAL') GROUP BY 1, 2)"""
    if view == "by_warehouse":
        return conn.execute(
            f"""SELECT b.warehouse_id, w.warehouse_code, w.name AS warehouse_name, p.product_id, p.product_code,
                       p.name, p.reorder_level, b.qty_on_hand, COALESCE(h.deposit_qty, 0)::bigint AS deposit_qty,
                       (b.qty_on_hand + COALESCE(h.deposit_qty, 0))::bigint AS physical_qty, {conv} AS conversions
                FROM stock_balances b JOIN products p USING (product_id) JOIN warehouses w USING (warehouse_id)
                LEFT JOIN {held} h ON h.product_id = b.product_id AND h.warehouse_id = b.warehouse_id
                WHERE b.warehouse_id = ANY(%(whs)s) AND w.warehouse_type='PHYSICAL' AND p.is_active AND {flt}
                  AND (NOT %(low)s OR b.qty_on_hand <= p.reorder_level)
                ORDER BY p.product_code, w.warehouse_code LIMIT %(limit)s OFFSET %(offset)s""", params).fetchall()
    return conn.execute(
        f"""SELECT p.product_id, p.product_code, p.name, p.reorder_level,
                   COALESCE(sum(b.qty_on_hand), 0)::bigint AS qty_on_hand,
                   COALESCE(sum(h.deposit_qty), 0)::bigint AS deposit_qty,
                   (COALESCE(sum(b.qty_on_hand), 0) + COALESCE(sum(h.deposit_qty), 0))::bigint AS physical_qty,
                   COALESCE(json_agg(json_build_object('warehouse_id', b.warehouse_id, 'warehouse_code', w.warehouse_code,
                            'qty_on_hand', b.qty_on_hand, 'deposit_qty', COALESCE(h.deposit_qty, 0)) ORDER BY w.warehouse_code)
                            FILTER (WHERE b.warehouse_id IS NOT NULL), '[]'::json) AS by_warehouse,
                   {conv} AS conversions
            FROM products p
            LEFT JOIN stock_balances b ON b.product_id = p.product_id AND b.warehouse_id = ANY(%(whs)s)
            LEFT JOIN warehouses w ON w.warehouse_id = b.warehouse_id AND w.warehouse_type='PHYSICAL'
            LEFT JOIN {held} h ON h.product_id = b.product_id AND h.warehouse_id = b.warehouse_id
            WHERE p.is_active AND {flt}
            GROUP BY p.product_id
            HAVING (NOT %(low)s OR COALESCE(sum(b.qty_on_hand), 0) <= p.reorder_level)
            ORDER BY p.product_code LIMIT %(limit)s OFFSET %(offset)s""", params).fetchall()


@app.get("/api/stock/movements")
def stock_movements(product_id: Optional[int] = None, warehouse_id: Optional[int] = None,
                    doc_type: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
                    limit: int = 100, offset: int = 0, u=Depends(current_user), conn=Depends(get_conn)):
    limit, offset = clean_page(limit, offset)
    whs = viewable_warehouses(conn, u)
    if warehouse_id is not None:
        whs = [w for w in whs if w == warehouse_id]
    return conn.execute(
        """SELECT t.txn_id, t.txn_at, t.txn_date, t.txn_type, t.warehouse_id, w.warehouse_code, w.name AS warehouse_name,
                  p.product_id, p.product_code, p.name AS product_name,
                  COALESCE(d.doc_type, t.txn_type) AS doc_type,
                  COALESCE(d.doc_no, a.adjust_no, tr.transfer_no) AS doc_no,
                  t.qty_in, t.qty_out, t.balance_after, usr.full_name AS user_name, t.reverses_txn_id
           FROM stock_transactions t
           JOIN products p USING (product_id) JOIN warehouses w USING (warehouse_id)
           JOIN users usr ON usr.user_id = t.created_by
           LEFT JOIN stock_documents d ON d.document_id = t.document_id
           LEFT JOIN stock_adjustments a ON a.adjustment_id = t.adjustment_id
           LEFT JOIN stock_transfers tr ON tr.transfer_id = t.transfer_id
           WHERE t.warehouse_id = ANY(%(whs)s)
             AND (%(pid)s::bigint IS NULL OR t.product_id = %(pid)s)
             AND (%(dt_)s::text IS NULL OR t.txn_type = %(dt_)s OR d.doc_type = %(dt_)s)
             AND (%(df)s::date IS NULL OR t.txn_date >= %(df)s)
             AND (%(dto)s::date IS NULL OR t.txn_date <= %(dto)s)
           ORDER BY t.txn_id DESC LIMIT %(limit)s OFFSET %(offset)s""",
        {"whs": whs, "pid": product_id, "dt_": doc_type, "df": date_from, "dto": date_to,
         "limit": limit, "offset": offset}).fetchall()


# ------------------------------------------------------------------ stock counts


@app.get("/api/counts")
def list_counts(status: Optional[str] = None, u=Depends(current_user), conn=Depends(get_conn)):
    return conn.execute(
        """SELECT c.*, p.product_code, p.name AS product_name, w.name AS warehouse_name, usr.full_name AS counted_by_name
           FROM stock_counts c JOIN products p USING (product_id) JOIN warehouses w USING (warehouse_id)
           JOIN users usr ON usr.user_id = c.counted_by
           WHERE c.warehouse_id = ANY(%s) AND (%s::text IS NULL OR c.status = %s)
           ORDER BY c.count_id DESC LIMIT 200""",
        (viewable_warehouses(conn, u), status, status)).fetchall()


@app.post("/api/counts", status_code=201)
def create_count(body: CountIn, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "MANAGER", "STOREKEEPER")
    require_wh(u, body.warehouse_id, "can_transact")
    cid = engine.create_count(conn, body.warehouse_id, body.product_id, body.counted_qty, u["user_id"], body.count_date)
    return {"count_id": cid}


@app.post("/api/counts/{count_id}/apply")
def apply_count(count_id: int, body: ApplyCountIn, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "MANAGER", "STOREKEEPER")
    c = conn.execute("SELECT warehouse_id FROM stock_counts WHERE count_id=%s", (count_id,)).fetchone()
    if not c:
        raise engine.NotFound("ไม่พบใบตรวจนับ")
    require_wh(u, c["warehouse_id"], "can_transact")
    adj = engine.apply_count(conn, count_id, u["user_id"], body.reason)
    return {"adjustment_id": adj, "no_difference": adj is None}



# ------------------------------------------------------------------ Excel import


class ProcessIn(BaseModel):
    sheet: Optional[str] = None
    header_row: int = 1
    mapping: dict = {}
    options: dict = {}
    template_id: Optional[int] = None
    save_template_as: Optional[str] = None


class ConfirmIn(BaseModel):
    skip_errors: bool = False


class TemplateIn(BaseModel):
    name: str
    doc_type: str
    sheet_name: Optional[str] = None
    header_row: int = 1
    mapping: dict = {}
    settings: dict = {}


def _can_transact(u: dict):
    return lambda wh: is_admin(u) or bool(u["access"].get(wh, {}).get("can_transact"))


def _can_post(u: dict):
    def check(doc_type: str, wh: int) -> bool:
        try:
            require_doc_write(u, doc_type, wh)
            return True
        except Forbidden:
            return False
    return check


def _import_batch(conn, u: dict, batch_id: int) -> dict:
    b = conn.execute(
        """SELECT b.*, w.warehouse_code, w.name AS warehouse_name, usr.full_name AS uploaded_by_name
           FROM import_batches b JOIN warehouses w USING (warehouse_id) JOIN users usr ON usr.user_id = b.uploaded_by
           WHERE b.batch_id=%s""", (batch_id,)).fetchone()
    if not b:
        raise engine.NotFound("ไม่พบรายการนำเข้า")
    require_wh(u, b["warehouse_id"], "can_view")
    return b


def _preview_response(conn, data: bytes, kind: str, doc_type: str, sheet: Optional[str], header_row: Optional[int]) -> dict:
    sheets = readers.list_sheets(data, kind)
    grid, name = readers.read_grid(data, kind, sheet)
    hdr = header_row or readers.guess_header_row(grid)
    pv = importer.preview_payload(grid, hdr)
    return {"file_kind": kind, "sheets": sheets, "sheet": name, "guessed_header_row": readers.guess_header_row(grid),
            "preview": pv, "suggested_mapping": importer.suggest_mapping(pv["headers"]),
            "suggested_template_id": importer.suggest_template(conn, pv["headers"], doc_type)}


@app.post("/api/imports", status_code=201)
def upload_import(file: UploadFile = File(...), warehouse_id: int = Form(...), doc_type: str = Form(...),
                  u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "MANAGER", "STOREKEEPER", "SALES")
    if doc_type not in importer.IMPORT_DOC_TYPES:
        raise engine.ValidationFailed("ประเภทเอกสารไม่ถูกต้อง", field="doc_type", code_hint="BAD_DOC_TYPE")
    require_doc_write(u, doc_type, warehouse_id)
    wh = conn.execute("SELECT warehouse_type, is_active FROM warehouses WHERE warehouse_id=%s", (warehouse_id,)).fetchone()
    if not wh or not wh["is_active"] or wh["warehouse_type"] != "PHYSICAL":
        raise engine.ValidationFailed("ไม่พบคลังสินค้า หรือคลังถูกปิดใช้งาน", field="warehouse_id", code_hint="WAREHOUSE_NOT_FOUND")
    data = file.file.read(readers.MAX_FILE_BYTES + 1)
    if len(data) > readers.MAX_FILE_BYTES:
        raise readers.BadFile("ไฟล์ใหญ่เกิน 25 MB", code_hint="FILE_TOO_LARGE")
    name = (file.filename or "upload").replace("\\", "/").split("/")[-1][:255]
    kind = readers.sniff_kind(data, name)
    pv = _preview_response(conn, data, kind, doc_type, None, None)
    sha = hashlib.sha256(data).hexdigest()
    with conn.transaction():
        bid = conn.execute(
            """INSERT INTO import_batches (warehouse_id, doc_type, file_name, file_hash, uploaded_by)
               VALUES (%s,%s,%s,%s,%s) RETURNING batch_id""", (warehouse_id, doc_type, name, sha, u["user_id"])).fetchone()["batch_id"]
        importer.save_file(bid, data)
        audit(conn.cursor(), u["user_id"], "IMPORT_UPLOAD", "import_batch", bid, warehouse_id, new={"file": name, "kind": kind})
    prev = conn.execute("SELECT batch_id FROM import_batches WHERE file_hash=%s AND status='CONFIRMED' ORDER BY batch_id DESC LIMIT 1",
                        (sha,)).fetchone()
    return {"batch_id": bid, "file_name": name, "already_imported_batch_id": prev["batch_id"] if prev else None, **pv}


@app.get("/api/imports/{batch_id}/preview")
def import_preview(batch_id: int, sheet: Optional[str] = None, header_row: Optional[int] = None,
                   u=Depends(current_user), conn=Depends(get_conn)):
    b = _import_batch(conn, u, batch_id)
    data = importer.load_file(batch_id)
    return _preview_response(conn, data, readers.sniff_kind(data, b["file_name"]), b["doc_type"], sheet, header_row)


@app.post("/api/imports/{batch_id}/process")
def import_process(batch_id: int, body: ProcessIn, u=Depends(current_user), conn=Depends(get_conn)):
    b = _import_batch(conn, u, batch_id)
    require_doc_write(u, b["doc_type"], b["warehouse_id"])
    params = body.model_dump()
    summary = importer.process_batch(conn, batch_id, params, u["user_id"], _can_transact(u))
    template_id = None
    if body.save_template_as:
        template_id = importer.save_template(conn, u["user_id"], {
            "name": body.save_template_as, "doc_type": b["doc_type"], "sheet_name": body.sheet,
            "header_row": body.header_row, "mapping": body.mapping, "settings": body.options})
        conn.execute("UPDATE import_batches SET template_id=%s WHERE batch_id=%s", (template_id, batch_id))
    return {"summary": summary, "template_id": template_id}


@app.get("/api/imports/{batch_id}")
def import_detail(batch_id: int, u=Depends(current_user), conn=Depends(get_conn)):
    return _import_batch(conn, u, batch_id)


@app.get("/api/imports/{batch_id}/rows")
def import_rows(batch_id: int, status: Optional[str] = None, limit: int = 200, offset: int = 0,
                u=Depends(current_user), conn=Depends(get_conn)):
    _import_batch(conn, u, batch_id)
    limit, offset = clean_page(limit, offset)
    status = status or None  # '?status=' (empty) means "no filter"
    return conn.execute(
        """SELECT r.row_no, r.status, r.error_code, r.error_message AS error_detail, r.doc_no, r.doc_date, r.doc_type,
                  r.product_ref, r.unit_code, r.qty, r.base_qty, r.salesman, r.customer_code, r.document_id,
                  w.warehouse_code
           FROM import_rows r LEFT JOIN warehouses w USING (warehouse_id)
           WHERE r.batch_id=%s AND (%s::text IS NULL OR r.status = %s)
           ORDER BY r.row_no LIMIT %s OFFSET %s""", (batch_id, status, status, limit, offset)).fetchall()


@app.get("/api/imports/{batch_id}/errors.xlsx")
def import_errors_xlsx(batch_id: int, include_duplicates: bool = False, u=Depends(current_user), conn=Depends(get_conn)):
    _import_batch(conn, u, batch_id)
    data = importer.export_errors_xlsx(conn, batch_id, include_duplicates)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="import-{batch_id}-errors.xlsx"'})


@app.post("/api/imports/{batch_id}/confirm")
def import_confirm(batch_id: int, body: ConfirmIn, idempotency_key: Optional[str] = Header(None),
                   u=Depends(current_user), conn=Depends(get_conn)):
    b = _import_batch(conn, u, batch_id)
    require_doc_write(u, b["doc_type"], b["warehouse_id"])
    result_holder: dict = {}

    def work() -> int:
        result_holder["r"] = importer.confirm_batch(conn, batch_id, u["user_id"], body.skip_errors, _can_post(u))
        return batch_id

    _, replayed = engine.run_idempotent(conn, idempotency_key, u["user_id"], "IMPORT_CONFIRM",
                                        _hash({"id": batch_id, "skip": body.skip_errors}), work)
    batch = _import_batch(conn, u, batch_id)
    return {"replayed": replayed, "result": (batch["summary"] or {}).get("result"), "status": batch["status"]}


@app.post("/api/imports/{batch_id}/cancel")
def import_cancel(batch_id: int, u=Depends(current_user), conn=Depends(get_conn)):
    b = _import_batch(conn, u, batch_id)
    require_doc_write(u, b["doc_type"], b["warehouse_id"])
    importer.cancel_batch(conn, batch_id, u["user_id"])
    return {"ok": True}


@app.get("/api/imports")
def import_history(status: Optional[str] = None, limit: int = 50, offset: int = 0,
                   u=Depends(current_user), conn=Depends(get_conn)):
    limit, offset = clean_page(limit, offset)
    return conn.execute(
        """SELECT b.batch_id, b.file_name, b.doc_type, b.status, b.total_rows, b.valid_rows, b.error_rows, b.duplicate_rows,
                  b.uploaded_at, b.confirmed_at, w.warehouse_code, usr.full_name AS uploaded_by_name,
                  (b.summary->'result'->>'documents_posted')::int AS documents_posted
           FROM import_batches b JOIN warehouses w USING (warehouse_id) JOIN users usr ON usr.user_id = b.uploaded_by
           WHERE b.warehouse_id = ANY(%s) AND (%s::text IS NULL OR b.status = %s)
           ORDER BY b.batch_id DESC LIMIT %s OFFSET %s""",
        (viewable_warehouses(conn, u), status, status, limit, offset)).fetchall()


@app.get("/api/import-templates")
def import_templates(u=Depends(current_user), conn=Depends(get_conn)):
    return importer.list_templates(conn)


@app.post("/api/import-templates", status_code=201)
def save_import_template(body: TemplateIn, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "MANAGER", "STOREKEEPER", "SALES")
    return {"template_id": importer.save_template(conn, u["user_id"], body.model_dump())}


@app.delete("/api/import-templates/{template_id}")
def delete_import_template(template_id: int, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "MANAGER")
    with conn.transaction():
        conn.execute("UPDATE import_batches SET template_id=NULL WHERE template_id=%s", (template_id,))
        n = conn.execute("DELETE FROM import_templates WHERE template_id=%s", (template_id,)).rowcount
    if not n:
        raise engine.NotFound("ไม่พบรูปแบบ")
    return {"ok": True}


# ------------------------------------------------------------------ transfers (Phase 4)


class TransferItemIn(BaseModel):
    product_id: Optional[int] = None
    product_code: Optional[str] = None
    unit_code: Optional[str] = None
    qty: int = Field(gt=0)


class TransferIn(BaseModel):
    mode: str = "APPROVAL"
    from_warehouse_id: int
    to_warehouse_id: int
    transfer_date: Optional[date] = None
    transfer_no: Optional[str] = Field(None, max_length=50)
    remark: Optional[str] = None
    items: list[TransferItemIn]


class ReceiveIn(BaseModel):
    lines: Optional[list[TransferItemIn]] = None      # None = receive everything outstanding


class CancelIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)


def _transfer_row(conn, u, tid: int) -> dict:
    t = conn.execute("SELECT * FROM stock_transfers WHERE transfer_id=%s", (tid,)).fetchone()
    if not t:
        raise engine.NotFound("ไม่พบใบโอน")
    if not is_admin(u) and not ({t["from_warehouse_id"], t["to_warehouse_id"]} & set(viewable_warehouses(conn, u))):
        raise Forbidden("คุณไม่มีสิทธิ์ดูใบโอนนี้")
    return t


def transfer_detail(conn, tid: int) -> dict:
    t = conn.execute(
        """SELECT t.*, f.warehouse_code AS from_code, f.name AS from_name, d.warehouse_code AS to_code, d.name AS to_name,
                  cu.full_name AS created_by_name
           FROM stock_transfers t JOIN warehouses f ON f.warehouse_id=t.from_warehouse_id
           JOIN warehouses d ON d.warehouse_id=t.to_warehouse_id JOIN users cu ON cu.user_id=t.created_by
           WHERE t.transfer_id=%s""", (tid,)).fetchone()
    t["items"] = conn.execute(
        """SELECT i.*, p.product_code, p.name AS product_name, un.unit_code,
                  i.qty_sent_base - i.qty_received_base - i.qty_cancelled_base AS qty_outstanding_base
           FROM stock_transfer_items i JOIN products p USING (product_id) JOIN units un USING (unit_id)
           WHERE i.transfer_id=%s ORDER BY i.item_id""", (tid,)).fetchall()
    return t


@app.post("/api/transfers", status_code=201)
def create_transfer(body: TransferIn, idempotency_key: Optional[str] = Header(None),
                    u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "MANAGER", "STOREKEEPER")
    require_wh(u, body.from_warehouse_id, "can_transact")
    if body.mode == "INSTANT":
        require_wh(u, body.to_warehouse_id, "can_transact")
    payload = body.model_dump()
    tid, replayed = engine.run_idempotent(conn, idempotency_key, u["user_id"], "CREATE_TRANSFER", _hash(payload),
                                          lambda: transfers.create_transfer(conn, payload, u["user_id"]))
    return {"transfer_id": tid, "replayed": replayed, "transfer": transfer_detail(conn, tid)}


@app.get("/api/transfers")
def list_transfers(status: Optional[str] = None, warehouse_id: Optional[int] = None, limit: int = 50, offset: int = 0,
                   u=Depends(current_user), conn=Depends(get_conn)):
    limit, offset = clean_page(limit, offset)
    whs = viewable_warehouses(conn, u)
    return conn.execute(
        """SELECT t.transfer_id, t.transfer_no, t.transfer_mode, t.status, t.transfer_date, t.created_at,
                  f.warehouse_code AS from_code, d.warehouse_code AS to_code, t.from_warehouse_id, t.to_warehouse_id,
                  (SELECT COALESCE(sum(qty_sent_base),0) FROM stock_transfer_items WHERE transfer_id=t.transfer_id) AS qty_sent,
                  (SELECT COALESCE(sum(qty_received_base),0) FROM stock_transfer_items WHERE transfer_id=t.transfer_id) AS qty_received
           FROM stock_transfers t JOIN warehouses f ON f.warehouse_id=t.from_warehouse_id
           JOIN warehouses d ON d.warehouse_id=t.to_warehouse_id
           WHERE (t.from_warehouse_id = ANY(%s) OR t.to_warehouse_id = ANY(%s))
             AND (%s::text IS NULL OR t.status=%s)
             AND (%s::int IS NULL OR t.from_warehouse_id=%s OR t.to_warehouse_id=%s)
           ORDER BY t.transfer_id DESC LIMIT %s OFFSET %s""",
        (whs, whs, status or None, status or None, warehouse_id, warehouse_id, warehouse_id, limit, offset)).fetchall()


@app.get("/api/transfers/{transfer_id}")
def get_transfer(transfer_id: int, u=Depends(current_user), conn=Depends(get_conn)):
    _transfer_row(conn, u, transfer_id)
    return transfer_detail(conn, transfer_id)


@app.post("/api/transfers/{transfer_id}/ship")
def ship_transfer(transfer_id: int, idempotency_key: Optional[str] = Header(None),
                  u=Depends(current_user), conn=Depends(get_conn)):
    t = _transfer_row(conn, u, transfer_id)
    require_role(u, "MANAGER")                      # approval step: managers only
    require_wh(u, t["from_warehouse_id"], "can_transact")
    tid, replayed = engine.run_idempotent(conn, idempotency_key, u["user_id"], "SHIP_TRANSFER", _hash({"id": transfer_id}),
                                          lambda: transfers.ship_transfer(conn, transfer_id, u["user_id"]))
    return {"transfer_id": tid, "replayed": replayed, "transfer": transfer_detail(conn, tid)}


@app.post("/api/transfers/{transfer_id}/receive")
def receive_transfer(transfer_id: int, body: ReceiveIn, idempotency_key: Optional[str] = Header(None),
                     u=Depends(current_user), conn=Depends(get_conn)):
    t = _transfer_row(conn, u, transfer_id)
    require_role(u, "MANAGER", "STOREKEEPER")
    require_wh(u, t["to_warehouse_id"], "can_transact")
    lines = [x.model_dump() for x in body.lines] if body.lines is not None else None
    tid, replayed = engine.run_idempotent(
        conn, idempotency_key, u["user_id"], "RECEIVE_TRANSFER", _hash({"id": transfer_id, "l": lines}),
        lambda: transfers.receive_transfer(conn, transfer_id, u["user_id"], lines))
    return {"transfer_id": tid, "replayed": replayed, "transfer": transfer_detail(conn, tid)}


@app.post("/api/transfers/{transfer_id}/cancel")
def cancel_transfer(transfer_id: int, body: CancelIn, idempotency_key: Optional[str] = Header(None),
                    u=Depends(current_user), conn=Depends(get_conn)):
    t = _transfer_row(conn, u, transfer_id)
    if not (t["status"] == "DRAFT" and t["created_by"] == u["user_id"]):
        require_role(u, "MANAGER")
    require_wh(u, t["from_warehouse_id"], "can_transact")
    tid, replayed = engine.run_idempotent(conn, idempotency_key, u["user_id"], "CANCEL_TRANSFER",
                                          _hash({"id": transfer_id, "r": body.reason}),
                                          lambda: transfers.cancel_transfer(conn, transfer_id, u["user_id"], body.reason))
    return {"transfer_id": tid, "replayed": replayed, "transfer": transfer_detail(conn, tid)}


# ------------------------------------------------------------------ document lines from Excel


@app.post("/api/document-lines/parse")
def parse_document_lines(file: UploadFile = File(...), u=Depends(current_user), conn=Depends(get_conn)):
    """Read product + quantity rows from an Excel/CSV file for the new-document form. Saves nothing."""
    if u["role_code"] == "VIEWER":
        raise Forbidden("บัญชีนี้ดูข้อมูลได้อย่างเดียว")
    data = file.file.read(linefile.MAX_BYTES + 1)
    return linefile.parse(conn, data, file.filename or "")


@app.get("/api/document-lines/template")
def document_line_template(with_products: bool = True, u=Depends(current_user), conn=Depends(get_conn)):
    data = linefile.template(conn, with_products)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="items-template.xlsx"'})


# ------------------------------------------------------------------ customer deposits (ฝากคลัง)


class DepositLineIn(BaseModel):
    sale_item_id: int
    qty: Decimal = Field(gt=0)
    unit_code: Optional[str] = None


class DepositIn(BaseModel):
    sale_document_id: int
    items: list[DepositLineIn]
    deposit_date: Optional[date] = None
    due_date: Optional[date] = None
    remark: Optional[str] = Field(None, max_length=500)
    customer_name: Optional[str] = Field(None, max_length=200)


class DeliveryLineIn(BaseModel):
    deposit_item_id: int
    qty: Decimal = Field(gt=0)
    unit_code: Optional[str] = None


class DeliverIn(BaseModel):
    lines: Optional[list[DeliveryLineIn]] = None       # None = deliver everything still deposited
    delivery_date: Optional[date] = None
    remark: Optional[str] = Field(None, max_length=500)


class DepositCancelIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    return_to_stock: bool = True


def _deposit_for_action(conn, u, deposit_id: int) -> dict:
    d = conn.execute("SELECT deposit_id, warehouse_id FROM customer_deposits WHERE deposit_id=%s", (deposit_id,)).fetchone()
    if not d:
        raise engine.NotFound("ไม่พบใบฝากสินค้า")
    require_doc_write(u, "SALE", d["warehouse_id"])
    return d


@app.get("/api/deposits")
def list_deposits(status: Optional[str] = None, warehouse_id: Optional[int] = None, customer_id: Optional[int] = None,
                  q: Optional[str] = None, open_only: bool = False, limit: int = 100, offset: int = 0,
                  u=Depends(current_user), conn=Depends(get_conn)):
    limit, offset = clean_page(limit, offset)
    whs = viewable_warehouses(conn, u)
    if warehouse_id is not None:
        whs = [w for w in whs if w == warehouse_id]
    like = f"%{q.strip().lower()}%" if q and q.strip() else None
    return conn.execute(
        """SELECT d.deposit_id, d.deposit_no, d.status, d.deposit_date, d.due_date, d.warehouse_id, w.warehouse_code,
                  w.name AS warehouse_name, s.doc_no AS sale_doc_no, d.sale_document_id,
                  COALESCE(c.name, d.customer_name) AS customer_display, c.customer_code,
                  (SELECT count(*) FROM customer_deposit_items x WHERE x.deposit_id = d.deposit_id) AS line_count,
                  (SELECT sum(x.qty_base - x.qty_delivered_base - x.qty_cancelled_base)::bigint
                     FROM customer_deposit_items x WHERE x.deposit_id = d.deposit_id) AS qty_remaining_base,
                  (SELECT sum(x.qty_base)::bigint FROM customer_deposit_items x WHERE x.deposit_id = d.deposit_id) AS qty_total_base
           FROM customer_deposits d JOIN warehouses w USING (warehouse_id)
           JOIN stock_documents s ON s.document_id = d.sale_document_id
           LEFT JOIN customers c ON c.customer_id = d.customer_id
           WHERE d.warehouse_id = ANY(%(whs)s)
             AND (%(st)s::text IS NULL OR d.status = %(st)s)
             AND (NOT %(open)s OR d.status IN ('OPEN','PARTIAL'))
             AND (%(cid)s::bigint IS NULL OR d.customer_id = %(cid)s)
             AND (%(like)s::text IS NULL OR lower(d.deposit_no) LIKE %(like)s OR lower(s.doc_no) LIKE %(like)s
                  OR lower(COALESCE(c.name, d.customer_name, '')) LIKE %(like)s OR lower(COALESCE(c.customer_code,'')) LIKE %(like)s)
           ORDER BY (d.status IN ('OPEN','PARTIAL')) DESC, d.deposit_id DESC LIMIT %(limit)s OFFSET %(offset)s""",
        {"whs": whs, "st": status, "open": open_only, "cid": customer_id, "like": like, "limit": limit,
         "offset": offset}).fetchall()


@app.get("/api/deposits/{deposit_id}")
def get_deposit(deposit_id: int, u=Depends(current_user), conn=Depends(get_conn)):
    d = deposits.detail(conn, deposit_id)
    if not d:
        raise engine.NotFound("ไม่พบใบฝากสินค้า")
    require_wh(u, d["warehouse_id"], "can_view")
    return d


@app.post("/api/deposits", status_code=201)
def create_deposit(body: DepositIn, idempotency_key: Optional[str] = Header(None),
                   u=Depends(current_user), conn=Depends(get_conn)):
    s = conn.execute("SELECT warehouse_id FROM stock_documents WHERE document_id=%s", (body.sale_document_id,)).fetchone()
    if not s:
        raise engine.NotFound("ไม่พบบิลขาย")
    require_doc_write(u, "SALE", s["warehouse_id"])
    payload = body.model_dump(mode="json")
    did, replayed = engine.run_idempotent(
        conn, idempotency_key, u["user_id"], "CREATE_DEPOSIT", _hash(payload),
        lambda: deposits.create_deposit(conn, body.sale_document_id, payload["items"], u["user_id"], body.deposit_date,
                                        body.due_date, body.remark, body.customer_name))
    return {"deposit_id": did, "replayed": replayed, "deposit": deposits.detail(conn, did)}


@app.post("/api/deposits/{deposit_id}/deliver")
def deliver_deposit(deposit_id: int, body: DeliverIn, idempotency_key: Optional[str] = Header(None),
                    u=Depends(current_user), conn=Depends(get_conn)):
    _deposit_for_action(conn, u, deposit_id)
    payload = body.model_dump(mode="json")
    dl, replayed = engine.run_idempotent(
        conn, idempotency_key, u["user_id"], "DELIVER_DEPOSIT", _hash({"id": deposit_id, **payload}),
        lambda: deposits.deliver(conn, deposit_id, payload["lines"], u["user_id"], body.delivery_date, body.remark))
    return {"delivery_id": dl, "replayed": replayed, "deposit": deposits.detail(conn, deposit_id)}


@app.post("/api/deposits/{deposit_id}/cancel")
def cancel_deposit(deposit_id: int, body: DepositCancelIn, u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "MANAGER")
    _deposit_for_action(conn, u, deposit_id)
    res = deposits.cancel(conn, deposit_id, u["user_id"], body.reason, body.return_to_stock)
    return {**res, "deposit": deposits.detail(conn, deposit_id)}


# ------------------------------------------------------------------ bill scanning (AI)


@app.get("/api/bill-scan/status")
def bill_scan_status(u=Depends(current_user)):
    eng = billscan.engine()
    ok = billscan.api_key() is not None if eng == "claude" else billocr.tesseract_path() is not None
    return {"engine": eng, "configured": ok, "free": eng == "tesseract",
            "model": billscan.model() if eng == "claude" else "tesseract"}


@app.post("/api/bill-scan", status_code=201)
def bill_scan(file: UploadFile = File(...), u=Depends(current_user), conn=Depends(get_conn)):
    if u["role_code"] == "VIEWER":
        raise Forbidden("บัญชีนี้ดูข้อมูลได้อย่างเดียว")
    data = file.file.read(billscan.MAX_IMAGE_BYTES + 1)
    if len(data) > billscan.MAX_IMAGE_BYTES:
        raise billscan.BadImage("รูปใหญ่เกิน 10 MB", code_hint="IMAGE_TOO_LARGE")
    mime = billscan.sniff_image(data)
    path, sha = billscan.save_image(data, mime)
    prev = conn.execute("SELECT scan_id, document_id FROM bill_scans WHERE file_sha256=%s AND document_id IS NOT NULL "
                        "ORDER BY scan_id DESC LIMIT 1", (sha,)).fetchone()
    eng = billscan.engine()
    model_name = billscan.model() if eng == "claude" else "tesseract"
    try:
        ai = billscan.call_ai(data, mime) if eng == "claude" else billocr.read_bill(conn, data)
    except StockError as e:
        conn.execute("""INSERT INTO bill_scans (file_path, file_sha256, mime_type, ai_model, status, error_message, created_by)
                        VALUES (%s,%s,%s,%s,'FAILED',%s,%s)""", (path, sha, mime, model_name, e.message, u["user_id"]))
        raise

    def can_use(wid: int) -> bool:
        try:
            require_doc_write(u, "SALE", wid)
            return True
        except StockError:
            return False

    draft = billscan.build_draft(conn, ai, can_use)
    draft["engine"] = eng
    draft["ocr_text"] = ai.get("ocr_text")
    if prev:
        doc = conn.execute("SELECT doc_no FROM stock_documents WHERE document_id=%s", (prev["document_id"],)).fetchone()
        draft["warnings"].insert(0, {"code": "IMAGE_ALREADY_USED", "document_id": prev["document_id"],
                                     "doc_no": doc["doc_no"] if doc else ""})
    with conn.transaction():
        sid = conn.execute(
            """INSERT INTO bill_scans (file_path, file_sha256, mime_type, ai_model, ai_result, created_by)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING scan_id""",
            (path, sha, mime, model_name, Jsonb(ai), u["user_id"])).fetchone()["scan_id"]
        audit(conn.cursor(), u["user_id"], "BILL_SCAN", "bill_scan", sid, None,
              new={"doc_no": draft["doc_no"], "lines": len(draft["items"])})
    return {"scan_id": sid, "draft": draft}


@app.get("/api/bill-scans/{scan_id}/image")
def bill_scan_image(scan_id: int, u=Depends(current_user), conn=Depends(get_conn)):
    s = conn.execute("""SELECT b.*, d.warehouse_id FROM bill_scans b LEFT JOIN stock_documents d USING (document_id)
                        WHERE b.scan_id=%s""", (scan_id,)).fetchone()
    if not s:
        raise engine.NotFound("ไม่พบรูปบิล")
    if s["warehouse_id"] is not None:
        require_wh(u, s["warehouse_id"], "can_view")
    elif s["created_by"] != u["user_id"] and not is_admin(u):
        raise Forbidden("ไม่มีสิทธิ์ดูรูปนี้")
    p = Path(s["file_path"])
    if not p.is_file():
        raise engine.NotFound("ไฟล์รูปบิลหายไปจากเซิร์ฟเวอร์")
    return FileResponse(p, media_type=s["mime_type"], headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ dashboard & reports (Phase 5)


@app.get("/api/dashboard")
def get_dashboard(warehouse_id: Optional[int] = None, days: int = 30, u=Depends(current_user), conn=Depends(get_conn)):
    whs = viewable_warehouses(conn, u)
    if warehouse_id is not None and warehouse_id not in whs:
        raise Forbidden("คุณไม่มีสิทธิ์ดูคลังสินค้านี้")
    return reports.dashboard(conn, whs, warehouse_id, days)


@app.get("/api/reports")
def list_reports(lang: str = "th", u=Depends(current_user)):
    lang = lang if lang in reports.L else "th"
    return [{"name": n, "title": reports.lab(lang, tk), "admin_only": adm,
             "columns": [{"key": k, "label": lk, "text": reports.lab(lang, lk), "kind": kind} for k, lk, kind in cols]}
            for n, (tk, cols, _, adm) in reports.REPORTS.items() if not adm or is_admin(u)]


@app.get("/api/reports/{name}")
def get_report(name: str, format: str = "json", lang: str = "th", warehouse_id: Optional[int] = None,
               date_from: Optional[date] = None, date_to: Optional[date] = None, product_id: Optional[int] = None,
               category_id: Optional[int] = None, q: Optional[str] = None, low_only: bool = False, days: Optional[int] = None,
               txn_type: Optional[str] = None, status: Optional[str] = None, action: Optional[str] = None,
               klass: Optional[str] = Query(None, alias="class"), limit: int = 500, offset: int = 0,
               u=Depends(current_user), conn=Depends(get_conn)):
    if name not in reports.REPORTS:
        raise engine.NotFound("ไม่พบรายงาน")
    if reports.REPORTS[name][3]:
        require_role(u, "ADMIN")
    if format not in ("json", "xlsx", "pdf"):
        raise engine.ValidationFailed("รูปแบบไฟล์ไม่ถูกต้อง", field="format")
    lang = lang if lang in reports.L else "th"
    whs = viewable_warehouses(conn, u)
    if warehouse_id is not None and warehouse_id not in whs:
        raise Forbidden("คุณไม่มีสิทธิ์ดูคลังสินค้านี้")
    params = {"warehouse_id": warehouse_id, "date_from": date_from, "date_to": date_to, "product_id": product_id,
              "category_id": category_id, "q": q, "low_only": low_only, "days": days, "txn_type": txn_type,
              "status": status, "action": action, "class": klass}
    try:
        rows, total, truncated = reports.run_report(conn, name, whs, params)
    except reports.ReportError as e:
        raise engine.ValidationFailed(str(e), code_hint="BAD_REPORT_PARAMS")
    desc = ", ".join(f"{k}={v}" for k, v in params.items() if v not in (None, "", False))
    if format == "json":
        limit, offset = clean_page(limit, offset)
        cols = reports.REPORTS[name][1]
        return {"name": name, "title": reports.lab(lang, reports.REPORTS[name][0]), "total": total, "truncated": truncated,
                "columns": [{"key": k, "label": lk, "text": reports.lab(lang, lk), "kind": kd} for k, lk, kd in cols],
                "labels": {c: reports.lab(lang, c) for c in ("OK", "LOW", "OUT", "FAST", "NORMAL", "SLOW", "DEAD", "OPEN", "PARTIAL", "COMPLETED", "CANCELLED")},
                "rows": rows[offset:offset + limit]}
    audit(conn.cursor(), u["user_id"], "EXPORT_REPORT", "report", name, new={"format": format, "filters": desc, "rows": total})
    stamp = date.today().strftime("%Y%m%d")
    if format == "xlsx":
        data = reports.to_xlsx(name, rows, lang, desc)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        try:
            data = reports.to_pdf(name, rows, lang, desc, truncated)
        except reports.ReportError as e:
            raise engine.ValidationFailed(str(e), code_hint="PDF_UNAVAILABLE")
        media = "application/pdf"
    return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}_{stamp}.{format}"'})


@app.get("/api/admin/integrity")
def integrity(u=Depends(current_user), conn=Depends(get_conn)):
    require_role(u, "ADMIN")
    result = reports.integrity_check(conn)
    audit(conn.cursor(), u["user_id"], "INTEGRITY_CHECK", "system", "integrity", new={"ok": result["ok"]})
    return result


# ------------------------------------------------------------------ frontend

STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")

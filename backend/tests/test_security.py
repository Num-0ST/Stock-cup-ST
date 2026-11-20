"""Phase 5 — security: lockout, password policy, token invalidation, headers, audit."""
from fastapi.testclient import TestClient

from app.main import app
from test_stock_engine import make_user, q


def login(username, password):
    c = TestClient(app, client=("203.0.113.9", 5000))
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    return c, r


def test_lockout_after_5_failures_and_admin_unlock(client, w):
    make_user(client, "VIEWER", [], "viewer1")
    for i in range(4):
        assert login("viewer1", "wrong-pass-1")[1].status_code == 401
    assert login("viewer1", "wrong-pass-1")[1].status_code == 401            # 5th failure locks
    c, r = login("viewer1", "password-1")                                     # correct password, but locked
    assert r.status_code == 429 and r.json()["error"]["code"] == "ACCOUNT_LOCKED"
    acts = [x["action"] for x in q("SELECT action FROM audit_logs ORDER BY audit_id")]
    assert "ACCOUNT_LOCKED" in acts and "LOGIN_BLOCKED" in acts and acts.count("LOGIN_FAILED") == 4
    uid = q("SELECT user_id FROM users WHERE username='viewer1'")[0]["user_id"]
    assert client.post(f"/api/users/{uid}/unlock").status_code == 200
    assert login("viewer1", "password-1")[1].status_code == 200
    assert q("SELECT failed_logins FROM users WHERE username='viewer1'")[0]["failed_logins"] == 0


def test_success_resets_counter_and_unknown_user_is_logged(client, w):
    make_user(client, "VIEWER", [], "viewer2")
    for _ in range(3):
        login("viewer2", "wrong-pass-1")
    assert login("viewer2", "password-1")[1].status_code == 200
    for _ in range(4):
        login("viewer2", "wrong-pass-1")
    assert login("viewer2", "password-1")[1].status_code == 200              # counter was reset, only 4 fails since
    assert login("nobody", "whatever1")[1].status_code == 401
    assert q("SELECT count(*) n FROM audit_logs WHERE action='LOGIN_FAILED' AND user_id IS NULL")[0]["n"] == 1
    assert q("SELECT ip_address FROM audit_logs WHERE action='LOGIN' ORDER BY audit_id DESC LIMIT 1")[0]["ip_address"].compressed == "203.0.113.9"


def test_password_policy(client, w):
    body = lambda pw, un="newuser": {"username": un, "password": pw, "full_name": "N", "role_code": "VIEWER", "warehouses": []}
    for bad in ["short1", "onlyletters", "12345678", "newuser"]:
        r = client.post("/api/users", json=body(bad))
        assert r.status_code == 422, bad
    assert client.post("/api/users", json=body("good-pass-9")).status_code == 201
    uid = q("SELECT user_id FROM users WHERE username='newuser'")[0]["user_id"]
    assert client.put(f"/api/users/{uid}", json={"password": "alllettersx"}).status_code == 422


def test_change_password_invalidates_old_tokens(client, w):
    make_user(client, "VIEWER", [], "viewer3")
    c, r = login("viewer3", "password-1")
    old = c.headers.get("Authorization")
    c.headers["Authorization"] = "Bearer " + r.json()["token"]
    assert c.get("/api/me").status_code == 200
    assert c.post("/api/me/password", json={"current_password": "nope-pass-1", "new_password": "brand-new-2"}).status_code == 422
    assert c.post("/api/me/password", json={"current_password": "password-1", "new_password": "weakweak"}).status_code == 422
    assert c.post("/api/me/password", json={"current_password": "password-1", "new_password": "password-1"}).status_code == 422
    import time; time.sleep(1.1)                                              # iat has 1-second resolution
    r2 = c.post("/api/me/password", json={"current_password": "password-1", "new_password": "brand-new-2"})
    assert r2.status_code == 200
    assert c.get("/api/me").status_code == 401                                # old token rejected
    c.headers["Authorization"] = "Bearer " + r2.json()["token"]
    assert c.get("/api/me").status_code == 200                                # fresh token works
    assert login("viewer3", "password-1")[1].status_code == 401
    assert login("viewer3", "brand-new-2")[1].status_code == 200


def test_admin_password_reset_and_deactivate_cut_sessions(client, w):
    make_user(client, "VIEWER", [], "viewer4")
    c, r = login("viewer4", "password-1")
    c.headers["Authorization"] = "Bearer " + r.json()["token"]
    uid = q("SELECT user_id FROM users WHERE username='viewer4'")[0]["user_id"]
    import time; time.sleep(1.1)
    assert client.put(f"/api/users/{uid}", json={"password": "reset-pass-3"}).status_code == 200
    assert c.get("/api/me").status_code == 401
    c2, r2 = login("viewer4", "reset-pass-3")
    c2.headers["Authorization"] = "Bearer " + r2.json()["token"]
    assert client.put(f"/api/users/{uid}", json={"is_active": False}).status_code == 200
    assert c2.get("/api/me").status_code == 401


def test_security_headers_and_no_cache(client):
    r = client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["cache-control"] == "no-store"
    assert "script-src 'self'" in client.get("/").headers["content-security-policy"]


def test_unauthenticated_and_tampered_tokens_rejected(client):
    c = TestClient(app)
    for path in ["/api/me", "/api/stock/balances", "/api/documents", "/api/transfers", "/api/imports", "/api/users"]:
        assert c.get(path).status_code == 401, path
    c.headers["Authorization"] = "Bearer " + client.headers["Authorization"].split(" ")[1][:-3] + "abc"
    assert c.get("/api/me").status_code == 401
    c.headers["Authorization"] = "Bearer not.a.jwt"
    assert c.get("/api/me").status_code == 401


def test_concurrent_first_start_creates_exactly_one_admin():
    """Several workers boot at once on an empty database (docker runs --workers 2)."""
    import threading
    from conftest import reset_db
    from app.main import seed_admin
    reset_db()
    errs = []

    def boot():
        try:
            seed_admin()
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    th = [threading.Thread(target=boot) for _ in range(6)]
    [t.start() for t in th]; [t.join() for t in th]
    assert not errs, errs
    assert q("SELECT count(*) n FROM users")[0]["n"] == 1

import os
import time

import bcrypt
import jwt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def _secret() -> str:
    s = os.environ.get("SECRET_KEY", "")
    if len(s) < 32:
        raise RuntimeError("SECRET_KEY must be set (at least 32 characters)")
    return s


def make_token(user_id: int, ttl_seconds: int = 12 * 3600) -> str:
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": now + ttl_seconds}, _secret(), algorithm="HS256")


def decode_token(token: str) -> int:
    return decode_token_full(token)[0]


def decode_token_full(token: str) -> tuple[int, int]:
    payload = jwt.decode(token, _secret(), algorithms=["HS256"])
    return int(payload["sub"]), int(payload["iat"])


def check_password_policy(password: str, username: str = "") -> str | None:
    """Return an error message, or None if the password is acceptable."""
    if len(password) < 8:
        return "รหัสผ่านต้องยาวอย่างน้อย 8 ตัวอักษร"
    if len(password) > 72:
        return "รหัสผ่านยาวเกินไป (สูงสุด 72 ตัวอักษร)"
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "รหัสผ่านต้องมีทั้งตัวอักษรและตัวเลข"
    if username and password.lower() == username.lower():
        return "รหัสผ่านต้องไม่เหมือนชื่อผู้ใช้"
    return None

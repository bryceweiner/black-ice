"""Single local account, signed session cookie, CSRF on unsafe methods."""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from ..config import get_settings

SESSION_COOKIE = "blackice_session"
CSRF_COOKIE = "blackice_csrf"
CSRF_HEADER = "x-csrf-token"
MAX_AGE = 60 * 60 * 24 * 30

_ph = PasswordHasher()


def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except (VerifyMismatchError, ValueError):
        return False


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="blackice-session")


def issue_session(username: str) -> tuple[str, str]:
    """Return (session token, csrf token)."""
    csrf = secrets.token_urlsafe(32)
    token = _serializer().dumps({"u": username, "c": csrf})
    return token, csrf


def read_session(token: str) -> dict | None:
    if not token:
        return None
    try:
        return _serializer().loads(token, max_age=MAX_AGE)
    except BadSignature:
        return None


def authenticate(username: str, password: str) -> bool:
    s = get_settings()
    if not s.admin_password_hash:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No ADMIN_PASSWORD_HASH configured. Run: blackice hash-password",
        )
    if not secrets.compare_digest(username, s.admin_username):
        return False
    return verify_password(password, s.admin_password_hash)


async def require_user(request: Request) -> str:
    """Dependency: valid session, plus CSRF match on unsafe methods."""
    data = read_session(request.cookies.get(SESSION_COOKIE, ""))
    if not data:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not secrets.compare_digest(sent, data.get("c", "")):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token mismatch")
    return data["u"]

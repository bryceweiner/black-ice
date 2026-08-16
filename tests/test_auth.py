import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from blackice.api import auth


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/read")
    async def read(user: str = Depends(auth.require_user)):
        return {"user": user}

    @app.post("/write")
    async def write(user: str = Depends(auth.require_user)):
        return {"user": user}

    return TestClient(app)


def _login(client) -> str:
    token, csrf = auth.issue_session("admin")
    client.cookies.set(auth.SESSION_COOKIE, token)
    return csrf


def test_rejects_anonymous(client):
    assert client.get("/read").status_code == 401
    assert client.post("/write").status_code == 401


def test_rejects_forged_session(client):
    client.cookies.set(auth.SESSION_COOKIE, "not-a-real-token")
    assert client.get("/read").status_code == 401


def test_safe_method_needs_no_csrf(client):
    _login(client)
    assert client.get("/read").json() == {"user": "admin"}


def test_unsafe_method_requires_csrf(client):
    _login(client)
    assert client.post("/write").status_code == 403


def test_unsafe_method_rejects_wrong_csrf(client):
    _login(client)
    r = client.post("/write", headers={auth.CSRF_HEADER: "wrong"})
    assert r.status_code == 403


def test_unsafe_method_accepts_matching_csrf(client):
    csrf = _login(client)
    r = client.post("/write", headers={auth.CSRF_HEADER: csrf})
    assert r.json() == {"user": "admin"}


def test_csrf_from_another_session_is_rejected(client):
    """The CSRF token is bound into the signed session, so a token minted for
    one session must not validate against another."""
    _login(client)
    _, other_csrf = auth.issue_session("admin")
    r = client.post("/write", headers={auth.CSRF_HEADER: other_csrf})
    assert r.status_code == 403


def test_password_roundtrip():
    h = auth.hash_password("hunter2")
    assert auth.verify_password("hunter2", h)
    assert not auth.verify_password("hunter3", h)
    assert not auth.verify_password("hunter2", "not-a-hash")

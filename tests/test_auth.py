"""访问控制：口令登录、会话 Cookie、Bearer 调用、公开路径、失败限流、正式环境禁止重置。"""
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.db import db

TOKEN = "demo-Kx9-2026"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "access_token", TOKEN)
    auth._fails.clear()
    db.reset()
    from app.main import app
    with TestClient(app, follow_redirects=False) as c:
        yield c


def test_open_when_token_not_set(monkeypatch):
    monkeypatch.setattr(settings, "access_token", "")
    from app.main import app
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/api/dashboard").status_code == 200
        assert c.get("/login").status_code in (302, 307)  # 未启用时登录页直接回首页
        assert c.get("/api/status").json()["auth_enabled"] is False


def test_everything_protected_except_public(client):
    for path in ["/api/dashboard", "/api/candidates", "/api/messages", "/api/export.csv", "/api/stream"]:
        assert client.get(path).status_code == 401, path
    assert client.post("/api/messages", json={"text": "张三一面过了"}).status_code == 401
    assert client.post("/api/reset").status_code == 401
    r = client.get("/")
    assert r.status_code in (302, 307) and r.headers["location"] == "/login"
    # 公开路径
    assert client.get("/healthz").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/wecom/callback", params={"msg_signature": "x", "timestamp": "1", "nonce": "n",
                                                 "echostr": "e"}).status_code != 401  # 由企微签名保护，不走口令


def test_login_cookie_flow(client):
    assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
    r = client.post("/api/login", json={"token": TOKEN})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "rk_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/").status_code == 200
    client.post("/api/logout")
    client.cookies.clear()
    assert client.get("/api/dashboard").status_code == 401


def test_bearer_token_for_scripts(client):
    assert client.get("/api/dashboard", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.get("/api/dashboard", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_forged_or_expired_session_rejected(client, monkeypatch):
    assert client.get("/api/dashboard", cookies={"rk_session": "9999999999.deadbeef"}).status_code == 401
    good = auth.make_session()
    monkeypatch.setattr(settings, "access_token", "rotated")  # 修改口令后旧会话全部失效
    assert client.get("/api/dashboard", cookies={"rk_session": good}).status_code == 401


def test_login_rate_limit(client):
    for _ in range(auth.MAX_FAILS):
        client.post("/api/login", json={"token": "x"})
    assert client.post("/api/login", json={"token": TOKEN}).status_code == 429


def test_reset_forbidden_in_production(client, monkeypatch):
    h = {"Authorization": f"Bearer {TOKEN}"}
    monkeypatch.setattr(settings, "seed_demo_data", False)
    assert client.post("/api/reset", headers=h).status_code == 403
    assert client.get("/api/status", headers=h).json()["demo_mode"] is False

"""访问控制：口令登录 + HMAC 签名会话 Cookie + 登录失败限流。

- 未设置 ACCESS_TOKEN：不启用（仅适合本机演示），界面上会提示。
- 设置后：页面与 /api/* 需要登录；脚本可用 `Authorization: Bearer <ACCESS_TOKEN>` 调用。
- /wecom/callback 不走口令（由企微消息签名 + AES 加密保护），/healthz 供容器健康检查。
"""
from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from .config import settings

COOKIE = "rk_session"
PUBLIC_EXACT = {"/healthz", "/login", "/api/login", "/favicon.ico"}
PUBLIC_PREFIX = ("/static/", "/wecom/")
MAX_FAILS, WINDOW = 8, 600  # 每个 IP 10 分钟内最多失败 8 次

_fails: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def _key() -> bytes:
    # 会话签名密钥由口令派生：修改口令即可让所有旧会话失效
    return hashlib.sha256(f"recruit-ai-session:{settings.access_token}".encode()).digest()


def make_session() -> str:
    exp = str(int(time.time()) + settings.session_hours * 3600)
    return f"{exp}.{hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()}"


def valid_session(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    exp, sig = value.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig, hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest())


def check_token(token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(token.encode(), settings.access_token.encode())


def is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX)


def is_authed(request: Request) -> bool:
    if not settings.auth_enabled:
        return True
    if valid_session(request.cookies.get(COOKIE)):
        return True
    auth = request.headers.get("authorization", "")
    return auth.lower().startswith("bearer ") and check_token(auth[7:].strip())


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def too_many_fails(ip: str) -> bool:
    with _lock:
        q = _fails[ip]
        while q and q[0] < time.time() - WINDOW:
            q.popleft()
        return len(q) >= MAX_FAILS


def record_fail(ip: str) -> None:
    with _lock:
        _fails[ip].append(time.time())


def reset_fails(ip: str) -> None:
    with _lock:
        _fails.pop(ip, None)

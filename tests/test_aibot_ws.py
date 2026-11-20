"""企微智能机器人 · 长连接模式：本地启动一个模拟的企微 WebSocket 服务器（真实 WebSocket），完整走一遍：

认证 → 群里 @ 消息推送 → 流式回复（同一 req_id 串行、等回执）→ 数据入库 → 重复推送排重 → 空消息回帮助
→ 简历文件（URL + 独立 aeskey 解密）→ 进入会话欢迎语 → 心跳无回执自动重连 → 认证失败 → 被新连接踢下线后不再重连
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from websockets.sync.server import serve

BOT_ID, SECRET = "bot_test", "secret_test"


class FakeWeComWS:
    def __init__(self):
        self.frames: list[dict] = []
        self.conns = 0
        self.auth_ok = True
        self.ack_ping = True
        self.ws = None
        self.lock = threading.Lock()

    def handler(self, ws):
        self.conns += 1
        auth = json.loads(ws.recv())
        self.frames.append(auth)
        ok = (self.auth_ok and auth.get("cmd") == "aibot_subscribe"
              and auth["body"] == {"bot_id": BOT_ID, "secret": SECRET})
        ws.send(json.dumps({"headers": {"req_id": auth["headers"]["req_id"]},
                            "errcode": 0 if ok else 40058, "errmsg": "ok" if ok else "invalid bot secret"}))
        if not ok:
            return
        self.ws = ws
        for raw in ws:
            f = json.loads(raw)
            with self.lock:
                self.frames.append(f)
            if f.get("cmd") == "ping" and not self.ack_ping:
                continue
            ws.send(json.dumps({"headers": {"req_id": f["headers"]["req_id"]}, "errcode": 0, "errmsg": "ok"}))

    def push(self, frame: dict) -> None:
        self.ws.send(json.dumps(frame, ensure_ascii=False))

    def replies(self, rid: str) -> list[dict]:
        with self.lock:
            return [f for f in self.frames if f.get("headers", {}).get("req_id") == rid and f.get("cmd") != "ping"]


def _wait(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def wecom():
    fake = FakeWeComWS()
    server = serve(fake.handler, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fake.url = f"ws://127.0.0.1:{server.socket.getsockname()[1]}"
    clients = []

    def connect(heartbeat=30):
        from app.wecom.aibot_ws import AibotWS
        c = AibotWS(BOT_ID, SECRET, fake.url, heartbeat=heartbeat, ack_timeout=2)
        c.start()
        clients.append(c)
        return c

    fake.connect = connect
    yield fake
    for c in clients:
        c.stop()
    server.shutdown()


def _msg(rid: str, msgid: str, body: dict) -> dict:
    return {"cmd": "aibot_msg_callback", "headers": {"req_id": rid},
            "body": {"msgid": msgid, "aibotid": BOT_ID, "chattype": "group", "chatid": "wr_chat",
                     "from": {"userid": "hr_li"}, **body}}


def _finished(fake, rid):
    return any(f["body"].get("stream", {}).get("finish") for f in fake.replies(rid))


def test_group_mention_flow(wecom):
    from app.db import db
    from app.wecom import aibot_ws
    wecom.connect()
    assert _wait(lambda: aibot_ws.status["state"] == "已连接")
    wecom.push(_msg("req_1", "msg_1", {"msgtype": "text", "text": {
        "content": "@招聘助手 韩梅梅 产品经理 猎聘推荐 一面过了，明天下午3点二面"}}))
    assert _wait(lambda: _finished(wecom, "req_1"))
    rs = wecom.replies("req_1")
    assert all(f["cmd"] == "aibot_respond_msg" and f["body"]["msgtype"] == "stream" for f in rs)
    assert rs[0]["body"]["stream"]["content"].startswith("🤔") and not rs[0]["body"]["stream"]["finish"]
    assert len({f["body"]["stream"]["id"] for f in rs}) == 1  # 同一条流式消息
    assert "韩梅梅" in rs[-1]["body"]["stream"]["content"]
    c = db.find_by_name("韩梅梅")[0]
    assert c["stage"] == "复试" and c["position"] == "产品经理"
    msg = db.q("SELECT * FROM messages WHERE source='wecom_group' ORDER BY id DESC LIMIT 2")
    assert msg and msg[-1]["operator"] == "hr_li"

    # 同一 msgid 再次推送（网络重试）→ 不重复处理
    wecom.push(_msg("req_2", "msg_1", {"msgtype": "text", "text": {"content": "韩梅梅 一面过了"}}))
    time.sleep(0.6)
    assert wecom.replies("req_2") == []

    # 只 @ 不说话 → 回帮助
    wecom.push(_msg("req_3", "msg_3", {"msgtype": "text", "text": {"content": "@招聘助手"}}))
    assert _wait(lambda: _finished(wecom, "req_3"))
    assert "我是招聘助手" in wecom.replies("req_3")[-1]["body"]["stream"]["content"]


def test_resume_file_with_per_message_aeskey(wecom):
    from app.db import db
    from app.wecom import aibot_ws
    from app.wecom.crypto import encrypt_media
    key = os.urandom(32)
    plain = (Path(__file__).resolve().parent.parent / "static/samples/resume_demo.txt").read_bytes()
    blob = encrypt_media(plain, key)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

    http = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        wecom.connect()
        assert _wait(lambda: aibot_ws.status["state"] == "已连接")
        wecom.push(_msg("req_f", "msg_f", {"chattype": "single", "msgtype": "file", "file": {
            "url": f"http://127.0.0.1:{http.server_port}/f", "aeskey": base64.b64encode(key).decode()}}))
        assert _wait(lambda: _finished(wecom, "req_f"))
        assert "陈思宇" in wecom.replies("req_f")[-1]["body"]["stream"]["content"]
        assert db.find_by_name("陈思宇")
    finally:
        http.shutdown()


def test_welcome_on_enter_chat(wecom):
    from app.wecom import aibot_ws
    wecom.connect()
    assert _wait(lambda: aibot_ws.status["state"] == "已连接")
    wecom.push({"cmd": "aibot_event_callback", "headers": {"req_id": "req_e"},
                "body": {"msgid": "e1", "msgtype": "event", "event": {"eventtype": "enter_chat"}}})
    assert _wait(lambda: wecom.replies("req_e"))
    f = wecom.replies("req_e")[0]
    assert f["cmd"] == "aibot_respond_welcome_msg" and f["body"]["msgtype"] == "text"


def test_heartbeat_timeout_reconnects(wecom):
    from app.wecom import aibot_ws
    wecom.connect(heartbeat=0.2)
    assert _wait(lambda: aibot_ws.status["state"] == "已连接")
    assert _wait(lambda: any(f.get("cmd") == "ping" for f in wecom.frames))
    wecom.ack_ping = False  # 服务端不再回心跳 → 客户端判定断线并重连
    assert _wait(lambda: wecom.conns >= 2, timeout=10)


def test_auth_failure(wecom):
    from app.wecom import aibot_ws
    wecom.auth_ok = False
    wecom.connect()
    assert _wait(lambda: aibot_ws.status["state"] == "认证失败")
    assert "40058" in aibot_ws.status["last_error"]


def test_probe(wecom):
    from app.wecom.aibot_ws import probe
    assert probe(BOT_ID, SECRET, wecom.url)[0]
    assert not probe(BOT_ID, "wrong", wecom.url)[0]


def test_kicked_by_new_connection_pauses_reconnecting(wecom):
    from app.wecom import aibot_ws
    c = wecom.connect()
    assert _wait(lambda: aibot_ws.status["state"] == "已连接")
    wecom.push({"cmd": "aibot_event_callback", "headers": {"req_id": "req_k"},
                "body": {"msgid": "k1", "msgtype": "event", "event": {"eventtype": "disconnected_event"}}})
    assert _wait(lambda: aibot_ws.status["state"] == "已被新连接替换")
    time.sleep(1.5)
    assert wecom.conns == 1 and c.kicked

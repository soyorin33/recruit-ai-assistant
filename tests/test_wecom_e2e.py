"""企业微信端到端测试：本地起一个「模拟企微服务器」（真实 HTTP），完整走一遍：

企微回调校验 → 加密消息推送 → 解密 → AI 处理 → 主动回复（message/send）
→ 智能表格写入（add_records / update_records）→ 简历文件（media/get）→ 群机器人推送（webhook）

除了「企微服务器」是本地模拟的，其余代码与生产环境完全一致（HTTP 请求、加解密、异步队列都是真实运行）。
"""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import db
from app.wecom import api
from app.wecom.crypto import WXBizMsgCrypt

CORP_ID, TOKEN = "ww_test_corp", "test_token"
AES_KEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
RESUME = "林小雨\n求职意向：前端开发\n电话：13700001111\n邮箱：lxy@example.com\n本科 浙江大学\n3年工作经验，熟悉 Vue、React、TypeScript"


class FakeWeCom:
    """记录所有请求的模拟企微服务器。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj=None, raw: bytes | None = None, headers: dict | None = None):
                body = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(200)
                for k, v in (headers or {"Content-Type": "application/json"}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                fake.calls.append((u.path, q))
                if u.path.endswith("/gettoken"):
                    ok = q.get("corpid") == CORP_ID and q.get("corpsecret") == "secret"
                    return self._send({"errcode": 0, "access_token": "FAKE_TOKEN", "expires_in": 7200} if ok
                                      else {"errcode": 40001, "errmsg": "invalid credential"})
                if u.path.endswith("/media/get"):
                    return self._send(raw=RESUME.encode(), headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Disposition": 'attachment; filename="resume.txt"'})
                self._send({"errcode": 404})

            def do_POST(self):
                u = urlparse(self.path)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                fake.calls.append((u.path, {**body, "_token": q.get("access_token")}))
                if u.path.endswith("/smartsheet/add_records"):
                    return self._send({"errcode": 0, "records": [{"record_id": f"rec{len(fake.calls)}",
                                                                  "values": body["records"][0]["values"]}]})
                self._send({"errcode": 0, "errmsg": "ok"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def find(self, suffix: str) -> list[dict]:
        return [b for p, b in self.calls if p.endswith(suffix)]

    def wait(self, suffix: str, n: int = 1, timeout: float = 5.0) -> list[dict]:
        end = time.time() + timeout
        while time.time() < end:
            if len(self.find(suffix)) >= n:
                return self.find(suffix)
            time.sleep(0.05)
        raise AssertionError(f"{suffix} 未在 {timeout}s 内被调用 {n} 次，实际调用：{[p for p, _ in self.calls]}")


@pytest.fixture()
def wecom(monkeypatch):
    fake = FakeWeCom()
    for k, v in {"wecom_corp_id": CORP_ID, "wecom_agent_id": "1000002", "wecom_secret": "secret",
                 "wecom_token": TOKEN, "wecom_aes_key": AES_KEY, "wecom_api_base": f"{fake.url}/cgi-bin",
                 "smartsheet_docid": "DOC1", "smartsheet_sheet_id": "SHEET1",
                 "wecom_webhook_url": f"{fake.url}/webhook"}.items():
        monkeypatch.setattr(settings, k, v)
    api.token_cache.invalidate()
    db.reset()
    from app.main import app
    with TestClient(app) as client:
        yield fake, client
    fake.server.shutdown()


def encrypted_post(client, content: str | None = None, msg_id: str = "1", msg_type: str = "text",
                   media_id: str | None = None, user: str = "zhangsan"):
    c = WXBizMsgCrypt(TOKEN, AES_KEY, CORP_ID)
    inner = (f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName><FromUserName><![CDATA[{user}]]></FromUserName>"
             f"<CreateTime>1700000000</CreateTime><MsgType><![CDATA[{msg_type}]]></MsgType>"
             + (f"<Content><![CDATA[{content}]]></Content>" if content else "")
             + (f"<MediaId><![CDATA[{media_id}]]></MediaId>" if media_id else "")
             + f"<MsgId>{msg_id}</MsgId><AgentID>1000002</AgentID></xml>")
    packed = c.encrypt_xml(inner, "1700000000", "abc")
    sig = re.search(r"<MsgSignature><!\[CDATA\[(.*?)\]\]>", packed).group(1)
    return client.post(f"/wecom/callback?msg_signature={sig}&timestamp=1700000000&nonce=abc",
                       content=packed, headers={"Content-Type": "text/xml"})


def test_url_verification(wecom):
    _, client = wecom
    c = WXBizMsgCrypt(TOKEN, AES_KEY, CORP_ID)
    echostr = c.encrypt("echo-12345")
    sig = c.signature("1700000000", "n1", echostr)
    r = client.get("/wecom/callback", params={"msg_signature": sig, "timestamp": "1700000000", "nonce": "n1",
                                               "echostr": echostr})
    assert r.status_code == 200 and r.text == "echo-12345"
    bad = client.get("/wecom/callback", params={"msg_signature": "x", "timestamp": "1", "nonce": "n", "echostr": echostr})
    assert bad.status_code == 403


def test_message_to_reply_and_smartsheet(wecom):
    fake, client = wecom
    r = encrypted_post(client, "张三 Java后端 BOSS来的 13812345678，一面过了，明天下午3点二面，面试官陈晨", msg_id="m1")
    assert r.status_code == 200 and r.text == ""  # 立即返回，异步处理

    # 1) 主动回复 HR
    send = fake.wait("/message/send")[0]
    assert send["touser"] == "zhangsan" and send["agentid"] == 1000002 and send["_token"] == "FAKE_TOKEN"
    assert "已建档" in send["text"]["content"] and "张三" in send["text"]["content"]

    # 2) 写入智能表格：按列标题写入，手机号脱敏
    add = fake.wait("/smartsheet/add_records")[0]
    assert add["docid"] == "DOC1" and add["sheet_id"] == "SHEET1"
    assert add["key_type"] == "CELL_VALUE_KEY_TYPE_FIELD_TITLE"
    values = add["records"][0]["values"]
    assert values["姓名"] == [{"type": "text", "text": "张三"}]
    assert values["当前阶段"][0]["text"] == "复试"
    assert values["手机"][0]["text"] == "138****5678"

    # 3) 再次更新同一人 → update_records，带上第一次返回的 record_id
    encrypted_post(client, "张三二面过了", msg_id="m2")
    upd = fake.wait("/smartsheet/update_records")[0]
    assert upd["records"][0]["record_id"].startswith("rec")
    assert upd["records"][0]["values"]["当前阶段"][0]["text"] == "Offer"

    # 4) 企微超时重推同一 MsgId → 幂等，不重复处理
    n_send = len(fake.find("/message/send"))
    encrypted_post(client, "张三二面过了", msg_id="m2")
    time.sleep(0.3)
    assert len(fake.find("/message/send")) == n_send
    assert len(db.events_for(db.find_by_name("张三")[0]["id"])) == 2

    # 5) 消息也同步出现在 Web 看板的聊天记录里，并标记来源
    msgs = client.get("/api/messages").json()["items"]
    assert any(m["source"] == "wecom" and m["operator"] == "zhangsan" for m in msgs)


def test_resume_file_message(wecom):
    fake, client = wecom
    encrypted_post(client, msg_type="file", media_id="MEDIA_1", msg_id="f1", user="lisi")
    send = fake.wait("/message/send")[0]
    assert send["touser"] == "lisi" and "简历解析完成" in send["text"]["content"]
    assert fake.find("/media/get")[0]["media_id"] == "MEDIA_1"
    c = db.find_by_name("林小雨")[0]
    assert c["position"] == "前端开发" and c["stage"] == "简历筛选"


def test_confirm_flow_over_wecom(wecom):
    fake, client = wecom
    encrypted_post(client, "王五 产品经理 猎聘 二面过了", msg_id="c1")
    fake.wait("/message/send", 1)
    encrypted_post(client, "王五一面挂了", msg_id="c2")  # 阶段回退 → 需要确认
    assert "回复「确认」" in fake.wait("/message/send", 2)[1]["text"]["content"]
    assert db.find_by_name("王五")[0]["stage"] == "Offer"
    encrypted_post(client, "确认", msg_id="c3")
    assert "已确认写入" in fake.wait("/message/send", 3)[2]["text"]["content"]
    assert db.find_by_name("王五")[0]["status"] == "已淘汰"


def test_report_pushed_to_group_bot(wecom):
    fake, client = wecom
    encrypted_post(client, "赵六 前端 内推 约了明天上午10点一面", msg_id="r1")
    fake.wait("/message/send")
    r = client.post("/api/report").json()
    assert r["pushed"] is True
    hook = fake.wait("/webhook")[0]
    assert hook["msgtype"] == "markdown" and "招聘日报" in hook["markdown"]["content"]


def test_wrong_secret_gives_clear_error(wecom, monkeypatch):
    fake, _ = wecom
    monkeypatch.setattr(settings, "wecom_secret", "wrong")
    api.token_cache.invalidate()
    with pytest.raises(api.WeComError, match="40001"):
        api.token_cache.get()

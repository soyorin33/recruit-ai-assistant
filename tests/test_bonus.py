"""加分项测试：规则 × 大模型交叉校验、智能体进度、简历 × JD 匹配、截图识别、企微群 @ 智能机器人（流式回复）。"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from test_agent import FakeLLM, reply, tool_call

from app import bus, jd, pipeline
from app.config import settings
from app.db import db
from app.wecom.crypto import WXBizMsgCrypt, WeComCryptoError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture()
def fake(monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_base_url", f.url)
    monkeypatch.setattr(settings, "llm_model", "fake-model")
    db.reset()
    yield f
    f.server.shutdown()


# ---------------- 交叉校验 ----------------
def test_cross_check_blocks_conflicting_stage(fake):
    # 原话是「一面过了 → 进入复试」，模型却判成 Offer：规则复核冲突 → 转人工确认
    fake.script = [tool_call("record_candidates", {"updates": [{"name": "张三", "position": "后端开发", "stage": "Offer"}],
                                                   "confidence": 0.95}), reply("已记录")]
    msg = pipeline.handle_message("张三 Java后端 一面过了，明天下午3点二面", operator="hr")
    pend = [a for a in msg["payload"]["actions"] if a["type"] == "pending"]
    assert pend and any("规则复核不一致" in r and "Offer" in r for r in pend[0]["reasons"])
    assert db.find_by_name("张三") == []


def test_cross_check_silent_when_rules_abstain(fake):
    pipeline.handle_message("周杰 后端 内推 简历", operator="hr")  # 规则模式先建档（此时 fake 无剧本 → 降级）
    fake.script = [tool_call("record_candidates", {"updates": [{"name": "周杰", "status": "已淘汰"}], "confidence": 0.9}),
                   reply("好的")]
    msg = pipeline.handle_message("刚面完周杰，技术还行但是沟通一般，先pass吧", operator="hr")
    assert not msg["payload"].get("pending_id")  # 规则读不出状态 → 不算冲突 → 正常写入
    assert db.find_by_name("周杰")[0]["status"] == "已淘汰"


# ---------------- 智能体进度 ----------------
def test_agent_progress_events(fake):
    start = bus.latest_id()
    fake.script = [tool_call("query_data", {"type": "funnel"}), reply("漏斗如下")]
    pipeline.handle_message("看下漏斗", operator="hr")
    texts = [e["data"]["text"] for e in bus.since(start) if e["kind"] == "progress"]
    assert texts[0] == "🤔 正在理解消息…" and "📊 正在统计数据…" in texts and "✍️ 正在组织回复…" in texts


# ---------------- 简历 × JD 匹配 ----------------
def test_jd_rules_match():
    m = jd.rules_match("熟悉 Java、Spring、MySQL，了解 Docker", "后端开发", jd.DEFAULT_JDS["后端开发"], years=2)
    assert m["missing"] == ["Redis"] and "Docker" in m["matched"]
    assert any("低于要求" in r for r in m["risks"]) and m["level"] in ("待定", "不推荐")
    assert len(m["questions"]) == 3 and "Redis" in m["questions"][0]
    assert jd.match("xx", "不存在的岗位") is None


def test_resume_with_llm_jd_scoring(fake):
    resume = {"name": "陈思宇", "phone": "13912345678", "position": "后端开发", "years": 5, "education": "本科",
              "skills": ["Java"], "summary": "5年Java后端", "confidence": 0.9}
    score = {"score": 88, "level": "推荐面试", "matched": ["Java", "Redis"], "missing": [], "highlights": ["高并发"],
             "risks": [], "questions": ["q1", "q2", "q3"]}
    fake.script = [reply(json.dumps(resume, ensure_ascii=False)), reply(json.dumps(score, ensure_ascii=False))]
    msg = pipeline.handle_resume("cv.txt", "陈思宇 后端 5年 Java Redis 高并发 13912345678".encode())
    c = db.find_by_name("陈思宇")[0]
    assert c["match_score"] == 88 and json.loads(c["match_detail"])["engine"] == "llm"
    assert "88 分（推荐面试）" in msg["text"]
    assert "后端开发" in fake.requests[1]["messages"][0]["content"]  # JD 进入了评分 Prompt


# ---------------- 截图识别 ----------------
def test_image_without_vision_model_gives_guidance(monkeypatch):
    monkeypatch.setattr(settings, "vision_model", "")
    db.reset()
    msg = pipeline.handle_image("a.png", PNG, operator="hr")
    assert "VISION_MODEL" in msg["text"] and db.all_candidates() == []


def test_image_resume_and_chat_screenshot(fake, monkeypatch):
    monkeypatch.setattr(settings, "vision_model", "fake-vl")

    def vision_resume(body):
        assert body["model"] == "fake-vl"
        parts = body["messages"][1]["content"]
        assert parts[1]["type"] == "image_url" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
        return reply(json.dumps({"type": "resume", "confidence": 0.9, "text": "Java Spring MySQL Redis 5年",
                                 "candidates": [{"name": "林可", "position": "Java后端", "years": 5,
                                                 "summary": "5年后端", "channel": "boss"}]}, ensure_ascii=False))
    jd_reply = reply(json.dumps({"score": 80, "level": "推荐面试", "matched": ["Java"], "missing": [],
                                 "highlights": [], "risks": [], "questions": ["a", "b", "c"]}, ensure_ascii=False))
    fake.script = [vision_resume, jd_reply]
    msg = pipeline.handle_image("简历截图.png", PNG, operator="hr")
    c = db.find_by_name("林可")[0]
    assert c["position"] == "后端开发" and c["channel"] == "BOSS直聘" and c["stage"] == "简历筛选"
    assert c["match_score"] == 80 and msg["text"].startswith("🖼️ 识别为简历截图")
    assert db.events_for(c["id"])[0]["engine"] == "vision"

    # 聊天截图：只补充沟通结论，不推断 / 回退阶段
    db.update_candidate(c["id"], {"stage": "复试"})
    fake.script = [reply(json.dumps({"type": "chat", "confidence": 0.85, "candidates": [
        {"name": "林可", "feedback": "期望薪资 30K，下月可到岗", "stage": "简历筛选"}]}, ensure_ascii=False))]
    pipeline.handle_image("聊天截图.png", PNG, operator="hr")
    c = db.get_candidate(c["id"])
    assert c["stage"] == "复试" and "30K" in c["feedback"]


def test_image_other_type(fake, monkeypatch):
    monkeypatch.setattr(settings, "vision_model", "fake-vl")
    fake.script = [reply('{"type": "other", "candidates": [], "confidence": 0.9}')]
    assert "没有识别到候选人" in pipeline.handle_image("cat.png", PNG, operator="hr")["text"]


# ---------------- 企微群 @ 智能机器人 ----------------
TOKEN, AES = "bot_token", "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"


@pytest.fixture()
def bot(monkeypatch):
    monkeypatch.setattr(settings, "wecom_aibot_token", TOKEN)
    monkeypatch.setattr(settings, "wecom_aibot_aes_key", AES)
    db.reset()
    from app.main import app
    with TestClient(app) as client:
        yield client


def post_bot(client, data: dict, nonce="n1"):
    c = WXBizMsgCrypt(TOKEN, AES, "")
    enc = c.encrypt(json.dumps(data, ensure_ascii=False))
    ts = "1700000000"
    r = client.post(f"/wecom/aibot?msg_signature={c.signature(ts, nonce, enc)}&timestamp={ts}&nonce={nonce}",
                    json={"encrypt": enc})
    if r.status_code != 200 or not r.content:
        return r, None
    env = r.json()
    assert env["nonce"] == nonce and c.signature(str(env["timestamp"]), nonce, env["encrypt"]) == env["msgsignature"]
    return r, json.loads(c.decrypt(env["encrypt"]))


def group_text(content, msgid="m1", user="zhangsan"):
    return {"msgid": msgid, "aibotid": "AIBOT", "chatid": "CHAT", "chattype": "group", "from": {"userid": user},
            "response_url": "https://example.invalid/resp", "msgtype": "text", "text": {"content": content}}


def test_bot_url_verification(bot):
    c = WXBizMsgCrypt(TOKEN, AES, "")
    echostr = c.encrypt("hello-bot")
    r = bot.get("/wecom/aibot", params={"msg_signature": c.signature("1", "n", echostr), "timestamp": "1",
                                        "nonce": "n", "echostr": echostr})
    assert r.text == "hello-bot"


def test_bot_group_text_fast_path_and_dedup(bot):
    _, out = post_bot(bot, group_text("@招聘助手 张三 Java后端 BOSS来的，一面过了，明天下午3点二面"))
    st = out["stream"]
    assert out["msgtype"] == "stream" and st["finish"] is True and "已建档" in st["content"]
    assert db.find_by_name("张三")[0]["stage"] == "复试"
    msgs = bot.get("/api/messages").json()["items"]
    assert any(m["source"] == "wecom_group" and m["text"].startswith("张三") for m in msgs)  # @前缀已去掉
    # 企微重复推送同一 msgid：返回同一条流，不重复处理
    _, again = post_bot(bot, group_text("@招聘助手 张三 Java后端 BOSS来的，一面过了，明天下午3点二面"))
    assert again["stream"]["id"] == st["id"] and len(db.events_for(db.find_by_name("张三")[0]["id"])) == 1
    # 多轮：同一个人在群里继续说「他」
    _, out2 = post_bot(bot, group_text("@招聘助手 他二面过了", msgid="m2"))
    assert "Offer" in out2["stream"]["content"]


def test_bot_slow_path_streams_progress(bot, monkeypatch):
    def slow(text, operator, source, progress=None):
        progress("🔍 正在查找候选人…")
        time.sleep(2.2)
        return {"text": "处理完成：张三在复试阶段"}
    monkeypatch.setattr(pipeline, "handle_message", slow)
    _, first = post_bot(bot, group_text("@招聘助手 张三什么情况", msgid="s1"))
    sid = first["stream"]["id"]
    assert first["stream"]["finish"] is False and first["stream"]["content"] == "🔍 正在查找候选人…"
    time.sleep(1.2)
    _, done = post_bot(bot, {"msgid": "s1-refresh", "aibotid": "AIBOT", "chattype": "group",
                             "from": {"userid": "zhangsan"}, "msgtype": "stream", "stream": {"id": sid}})
    assert done["stream"] == {"id": sid, "finish": True, "content": "处理完成：张三在复试阶段"}


class EncryptedFileServer:
    def __init__(self, payload: bytes):
        data = WXBizMsgCrypt(TOKEN, AES, "").encrypt_bytes(payload)

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/file?sign=x"


def test_bot_file_resume(bot):
    srv = EncryptedFileServer("姓名：周宁\n求职意向：前端开发\n电话：13600001111\n3年工作经验，熟悉 Vue、React、TypeScript".encode())
    _, out = post_bot(bot, {"msgid": "f1", "aibotid": "AIBOT", "chattype": "single", "from": {"userid": "lisi"},
                            "msgtype": "file", "file": {"url": srv.url}})
    assert out["stream"]["finish"] is True and "简历解析完成" in out["stream"]["content"]
    c = db.find_by_name("周宁")[0]
    assert c["position"] == "前端开发" and c["match_score"] is not None
    srv.server.shutdown()


def test_bot_image_with_vision(bot, fake, monkeypatch):
    monkeypatch.setattr(settings, "vision_model", "fake-vl")
    srv = EncryptedFileServer(PNG)
    fake.script = [reply(json.dumps({"type": "chat", "confidence": 0.9, "candidates": [
        {"name": "白露", "position": "前端", "channel": "BOSS", "feedback": "下周二可面试"}]}, ensure_ascii=False))]
    _, out = post_bot(bot, {"msgid": "i1", "aibotid": "AIBOT", "chattype": "single", "from": {"userid": "lisi"},
                            "msgtype": "image", "image": {"url": srv.url}})
    assert "识别为聊天截图" in out["stream"]["content"]
    assert db.find_by_name("白露")[0]["feedback"] == "下周二可面试"
    srv.server.shutdown()


def test_bot_only_mention_and_bad_signature(bot):
    _, out = post_bot(bot, group_text("@招聘助手", msgid="h1"))
    assert "我是招聘助手" in out["stream"]["content"] and out["stream"]["finish"] is True
    r = bot.post("/wecom/aibot?msg_signature=bad&timestamp=1&nonce=n", json={"encrypt": "xx"})
    assert r.status_code == 403


def test_media_decrypt_roundtrip():
    c = WXBizMsgCrypt(TOKEN, AES, "")
    for size in (0, 1, 31, 32, 33, 1000):
        data = bytes(range(256)) * 4
        assert c.decrypt_bytes(c.encrypt_bytes(data[:size])) == data[:size]
    with pytest.raises(WeComCryptoError):
        c.decrypt_bytes(b"\x00" * 32)

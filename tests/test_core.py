"""核心链路测试：规则抽取、大模型路径（模拟返回）、去重与状态机、问数、企微加解密。

运行：.venv/bin/python -m pytest -q
"""
import json

import pytest  # noqa: E402

from app import extractor, llm, nlq, pipeline  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import db  # noqa: E402
from app.wecom.crypto import WXBizMsgCrypt, WeComCryptoError  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    db.reset()
    yield


# ---------------- 规则引擎 ----------------
@pytest.mark.parametrize("text,expect", [
    ("张三 Java后端 BOSS来的，一面过了，明天下午3点二面，面试官陈晨",
     {"name": "张三", "position": "后端开发", "channel": "BOSS直聘", "stage": "复试", "interviewer": "陈晨"}),
    ("王小明一面挂了，基础偏弱", {"name": "王小明", "stage": "初试", "status": "已淘汰"}),
    ("赵磊接了offer", {"name": "赵磊", "stage": "Offer"}),
    ("孙浩今天入职了", {"name": "孙浩", "stage": "已入职", "status": "已入职"}),
    ("李娜拒了offer", {"name": "李娜", "stage": "Offer", "status": "已放弃"}),
])
def test_rules_extract(text, expect):
    r = extractor.rules_extract_message(text, [])
    assert r["intent"] == "update"
    c = r["candidates"][0]
    for k, v in expect.items():
        assert c.get(k) == v, (k, c)


def test_rules_multi_and_intents():
    r = extractor.rules_extract_message("王芳二面挂了，算法基础薄弱；赵磊接了offer", [{"name": "王芳"}])
    assert [c["name"] for c in r["candidates"]] == ["王芳", "赵磊"]
    assert "position" not in r["candidates"][0]  # 评价里的「算法」不应被识别成岗位
    assert extractor.rules_extract_message("本周进了几个二面？", [])["intent"] == "query"
    assert extractor.rules_extract_message("发一下今天的日报", [])["intent"] == "report"
    assert extractor.extract_message("确认", [])["intent"] == "confirm"


def test_parse_time():
    from datetime import datetime
    base = datetime(2026, 11, 20, 9, 0)  # 周五
    assert extractor.parse_time("明天下午3点", base) == "2026-11-21 15:00"
    assert extractor.parse_time("下周一上午", base) == "2026-11-23 10:00"
    assert extractor.parse_time("周三下午2点半", base) == "2026-11-25 14:30"
    assert extractor.parse_time("没有时间", base) is None


# ---------------- 大模型路径（模拟模型返回，验证解析/校验/降级） ----------------
def test_llm_path_cleans_output(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "fake")
    fake = {"intent": "update", "confidence": 0.93, "candidates": [
        {"name": "张三", "position": "Java后端", "channel": "boss", "stage": "复试", "status": "进行中",
         "interview_time": "2026-11-21 15:00", "hacker_field": "x"},
        {"name": "李四", "stage": "不存在的阶段"},
    ]}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: ("```json\n" + json.dumps(fake, ensure_ascii=False) + "\n```", {"model": "fake"}))
    r = extractor.extract_message("随便一句话", [])
    assert r["engine"] == "llm" and r["confidence"] == 0.93
    a, b = r["candidates"]
    assert a["position"] == "后端开发" and a["channel"] == "BOSS直聘" and "hacker_field" not in a
    assert "stage" not in b  # 越界枚举被丢弃


def test_llm_failure_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "fake")

    def boom(*a, **k):
        raise llm.LLMError("timeout")
    monkeypatch.setattr(llm, "chat", boom)
    r = extractor.extract_message("赵磊接了offer", [])
    assert r["engine"] == "rules" and r["fallback_reason"] and r["candidates"][0]["stage"] == "Offer"


# ---------------- 流水线：去重 + 状态机 + 人工确认 ----------------
def test_pipeline_create_update_confirm():
    pipeline.handle_message("张三 Java后端 BOSS来的，一面过了，明天下午3点二面", operator="hr")
    assert len(db.all_candidates()) == 1
    pipeline.handle_message("张三二面过了", operator="hr")
    c = db.find_by_name("张三")[0]
    assert c["stage"] == "Offer" and len(db.all_candidates()) == 1  # 同一人，不重复建档
    msg = pipeline.handle_message("张三一面挂了", operator="hr")  # 阶段回退 → 待确认
    assert msg["payload"]["pending_id"]
    assert db.find_by_name("张三")[0]["stage"] == "Offer"  # 未确认前不写入
    pipeline.handle_message("确认", operator="hr")
    c = db.find_by_name("张三")[0]
    assert c["stage"] == "初试" and c["status"] == "已淘汰"
    assert len(db.events_for(c["id"])) == 3


def test_same_name_different_position_needs_confirm():
    pipeline.handle_message("张三 前端 内推 简历", operator="hr")
    msg = pipeline.handle_message("张三 产品经理 猎聘投的简历", operator="hr")
    assert msg["payload"]["pending_id"]


def test_nlq_counts():
    pipeline.handle_message("张三 Java后端 一面过了，明天二面", operator="hr")
    pipeline.handle_message("李雷 前端 一面过了，明天二面", operator="hr")
    ans = nlq.answer("本周进了几个复试？")
    assert ans["query"]["count"] == 2
    ans = nlq.answer("本周各岗位进了几个复试？")
    assert sorted(r[0] for r in ans["table"]["rows"]) == ["前端开发", "后端开发"]


def test_resume():
    text = "陈思宇\n求职意向：Java 后端开发\n电话：13912345678\n邮箱：a@b.com\n本科 华南理工大学\n5年工作经验，熟悉 Java、Redis"
    msg = pipeline.handle_resume("cv.txt", text.encode())
    c = db.find_by_name("陈思宇")[0]
    assert c["phone"] == "13912345678" and c["position"] == "后端开发" and c["stage"] == "简历筛选"
    assert "5年" in (c["resume_summary"] or "")
    assert "138" not in msg["text"]


# ---------------- 企业微信加解密 ----------------
def test_wecom_crypto_roundtrip():
    key = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"  # 企微官方文档示例 Key
    c = WXBizMsgCrypt("QDG6eK", key, "wx5823bf96d3bd56c7")
    xml = "<xml><ToUserName><![CDATA[corp]]></ToUserName><FromUserName><![CDATA[zhangsan]]></FromUserName><MsgType><![CDATA[text]]></MsgType><Content><![CDATA[张三一面过了]]></Content><MsgId>1</MsgId></xml>"
    packed = c.encrypt_xml(xml, "1409659813", "nonce")
    import re
    enc = re.search(r"<Encrypt><!\[CDATA\[(.*?)\]\]>", packed).group(1)
    sig = re.search(r"<MsgSignature><!\[CDATA\[(.*?)\]\]>", packed).group(1)
    msg = c.decrypt_xml(sig, "1409659813", "nonce", packed.encode())
    assert msg["Content"] == "张三一面过了" and msg["FromUserName"] == "zhangsan"
    assert c.verify_url(sig, "1409659813", "nonce", enc) == xml
    with pytest.raises(WeComCryptoError):
        c.decrypt_xml("bad", "1409659813", "nonce", packed.encode())

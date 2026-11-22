"""人工兜底：手动录入 / 编辑（仍走去重与状态机），以及「待人工处理」队列（异常、没识别、文件失败、同步失败）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import manual, pipeline, sync
from app.db import db
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean():
    db.reset()
    yield


def create(fields, **kw):
    return client.post("/api/candidates", json={"fields": fields, "operator": "HR小王", **kw})


def edit(cid, fields, **kw):
    return client.patch(f"/api/candidates/{cid}", json={"fields": fields, "operator": "HR小王", **kw})


def test_manual_create_goes_through_rules_and_audit():
    r = create({"name": "钱多多", "phone": "+86 138-0000-1234", "position": "Java后端", "channel": "BOSS",
                "interview_time": "2026-11-25T15:00", "interviewer": "陈晨"}).json()
    assert r["ok"] and r["op"] == "create"
    c = db.get_candidate(r["candidate"]["id"])
    assert c["phone"] == "13800001234" and c["position"] == "后端开发" and c["channel"] == "BOSS直聘"
    assert c["stage"] == "简历筛选" and c["interview_time"] == "2026-11-25 15:00" and c["hr_owner"] == "HR小王"
    ev = db.events_for(c["id"])[0]
    assert ev["engine"] == "manual" and ev["source"] == "manual" and ev["raw_text"] == manual.MANUAL_RAW


def test_manual_create_dedup_and_new_position_confirm():
    create({"name": "钱多多", "phone": "13800001234", "position": "后端开发"})
    dup = create({"name": "钱多多", "phone": "13800001234", "position": "后端开发"}).json()
    assert not dup["ok"] and dup["duplicate"]["name"] == "钱多多" and "请改为编辑" in dup["message"]
    r = create({"name": "钱多多", "phone": "13800001234", "position": "产品经理"}).json()
    assert r.get("needs_confirm") and any("已在应聘「后端开发」" in x for x in r["reasons"])
    assert create({"name": "钱多多", "phone": "13800001234", "position": "产品经理"}, force=True).json()["ok"]
    assert len(db.find_by_name("钱多多")) == 2


def test_manual_edit_allows_channel_fix_clear_and_confirms_rollback():
    cid = create({"name": "钱多多", "position": "后端开发", "channel": "BOSS直聘", "stage": "复试",
                  "interviewer": "陈晨"}).json()["candidate"]["id"]
    r = edit(cid, {"channel": "猎聘", "interviewer": ""}).json()  # 手动修正渠道、清空面试官
    assert r["ok"] and db.get_candidate(cid)["channel"] == "猎聘" and db.get_candidate(cid)["interviewer"] is None
    r = edit(cid, {"stage": "初试"}).json()
    assert r.get("needs_confirm") and any("阶段回退" in x for x in r["reasons"])
    assert db.get_candidate(cid)["stage"] == "复试"
    assert edit(cid, {"stage": "初试"}, force=True).json()["ok"] and db.get_candidate(cid)["stage"] == "初试"
    assert edit(cid, {"stage": "已入职"}, force=True).json()["ok"]
    assert db.get_candidate(cid)["status"] == "已入职"


@pytest.mark.parametrize("fields,msg", [
    ({"position": "后端开发"}, "姓名必填"),
    ({"name": "钱多多", "stage": "终面"}, "阶段只能是"),
    ({"name": "钱多多", "interview_time": "下周三"}, "面试时间格式"),
])
def test_manual_validation(fields, msg):
    r = create(fields)
    assert r.status_code == 400 and msg in r.json()["detail"]


def test_exception_goes_to_manual_queue_and_retry(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("模型服务不可用")
    monkeypatch.setattr(pipeline, "_route", boom)
    msg = pipeline.handle_message("韩梅梅 产品经理 一面过了", operator="HR小李", source="wecom_group")
    assert "已转人工" in msg["text"] and msg["payload"]["failure_id"]
    items = client.get("/api/failures").json()["items"]
    assert len(items) == 1 and items[0]["kind_label"] == "消息处理异常" and items[0]["retryable"]
    assert items[0]["raw_text"] == "韩梅梅 产品经理 一面过了" and "模型服务不可用" in items[0]["error"]
    assert client.get("/api/status").json()["failures_open"] == 1

    fid = items[0]["id"]
    r = client.post(f"/api/failures/{fid}/retry", json={"operator": "HR小王"}).json()  # 仍然失败
    assert not r["ok"] and len(client.get("/api/failures").json()["items"]) == 1  # 不重复入队
    monkeypatch.undo()  # 系统恢复后重试成功
    r = client.post(f"/api/failures/{fid}/retry", json={"operator": "HR小王"}).json()
    assert r["ok"] and db.find_by_name("韩梅梅")
    assert client.get("/api/failures").json()["items"] == []


def test_unrecognized_message_and_manual_entry_resolves_it():
    msg = pipeline.handle_message("今天面的那个小伙子一面过了，下周二复试", operator="HR小王")
    assert "已转人工" in msg["text"]
    f = client.get("/api/failures").json()["items"][0]
    assert f["kind"] == "unrecognized"
    r = create({"name": "孙小勇", "position": "前端开发", "stage": "复试"}, failure_id=f["id"]).json()
    assert r["ok"] and client.get("/api/failures").json()["items"] == []
    row = db.get_failure(f["id"])
    assert row["state"] == "resolved" and "孙小勇" in row["note"]


def test_bad_resume_goes_to_queue_and_can_be_dismissed():
    msg = pipeline.handle_resume("扫描件.pdf", b"%PDF-1.4 not really a pdf", operator="HR小王")
    assert "已转人工" in msg["text"]
    f = client.get("/api/failures").json()["items"][0]
    assert f["kind"] == "resume" and not f["retryable"]
    assert client.post(f"/api/failures/{f['id']}/retry", json={}).status_code == 400  # 文件只能重发或手动录入
    client.post(f"/api/failures/{f['id']}/resolve", json={"operator": "HR小王", "note": "已电话联系候选人"})
    assert client.get("/api/failures").json()["items"] == []


def test_sync_failure_recorded_once_and_retry(monkeypatch):
    cid = create({"name": "钱多多", "position": "后端开发"}).json()["candidate"]["id"]
    for _ in range(3):  # 同一位候选人的同步反复失败，只保留一条待处理
        manual.record("sync", raw_text="腾讯文档：钱多多", error="400007", source="sync", ref_id=cid)
    items = client.get("/api/failures").json()["items"]
    assert len(items) == 1 and items[0]["kind_label"] == "表格同步失败"
    called = []
    monkeypatch.setattr(sync, "retry", lambda c: called.append(c) or True)
    assert client.post(f"/api/failures/{items[0]['id']}/retry", json={}).json()["ok"] and called == [cid]


def test_static_assets_are_versioned_and_not_stale():
    """页面里的脚本 / 样式带内容版本号，且要求浏览器每次确认：避免「新页面 + 旧脚本」导致按钮没反应。"""
    import hashlib
    import re
    from pathlib import Path
    r = client.get("/")
    assert r.headers["cache-control"] == "no-cache"
    m = re.search(r'src="/static/app\.js\?v=(\w+)"', r.text)
    js = (Path(__file__).resolve().parent.parent / "static/app.js").read_bytes()
    assert m and m.group(1) == hashlib.md5(js).hexdigest()[:10]
    assert re.search(r'href="/static/style\.css\?v=\w+"', r.text)
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"

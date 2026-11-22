"""去重与并发：同一人多渠道 / 多岗位 / 多位 HR 录入 / 同时录入，以及数据库唯一约束兜底。

同一套测试在 SQLite 和 MySQL 下都会跑（CI 中设置 TEST_DATABASE_URL 即用 MySQL）。
"""
from __future__ import annotations

import threading

import pytest

from app import pipeline
from app.db import DuplicateError, db, to_mysql_sql
from app.integrity import check


@pytest.fixture(autouse=True)
def clean():
    db.reset()
    yield


def say(text: str, op: str = "HR小王") -> dict:
    return pipeline.handle_message(text, operator=op)


def rows(name: str) -> list[dict]:
    return sorted(db.find_by_name(name), key=lambda c: c["id"])


def test_channel_keeps_first_touch():
    say("张伟 Java后端 BOSS直聘投递 手机13800138000")
    m = say("张伟 Java后端 猎聘也投了 手机13800138000")
    rs = rows("张伟")
    assert len(rs) == 1 and rs[0]["channel"] == "BOSS直聘"  # 不被后来的渠道覆盖
    assert "渠道以首次投递为准" in m["text"] and "猎聘" in m["text"]


def test_same_phone_new_position_adds_application_after_confirm():
    say("张伟 Java后端 BOSS直聘投递 手机13800138000")
    m = say("张伟 产品经理 拉勾投递 手机13800138000")
    assert m["payload"].get("pending_id") and "已在应聘「后端开发」" in m["text"]
    assert len(rows("张伟")) == 1 and rows("张伟")[0]["position"] == "后端开发"  # 确认前原记录不变
    say("确认")
    rs = rows("张伟")
    assert [(r["position"], r["channel"]) for r in rs] == [("后端开发", "BOSS直聘"), ("产品经理", "拉勾")]
    assert not check(db)["同手机号同岗位重复"]


def test_same_phone_without_position_and_two_applications_asks():
    say("张伟 Java后端 BOSS直聘投递 手机13800138000")
    say("张伟 产品经理 拉勾投递 手机13800138000")
    say("确认")
    m = say("张伟 一面过了 手机13800138000")
    assert m["payload"].get("pending_id") and "2 条应聘记录" in m["text"]


def test_other_hr_gets_owner_notice():
    say("刘洋 前端开发 BOSS直聘 一面过了", op="HR小王")
    m = say("刘洋 前端开发 约了周五下午2点二面", op="HR小李")
    assert "由 HR小王 负责" in m["text"]
    rs = rows("刘洋")
    assert len(rs) == 1 and rs[0]["hr_owner"] == "HR小王"


def test_concurrent_entry_creates_one_record():
    threads = [threading.Thread(target=say, args=("赵敏 算法工程师 内推 手机13900001111", f"HR{i}")) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(rows("赵敏")) == 1


def test_unique_constraint_and_apply_fallback():
    say("孙悦 测试开发 BOSS直聘 手机13700007777")
    existing = rows("孙悦")[0]
    # 绕过代码层的查重直接插入 → 数据库唯一约束拦截
    with pytest.raises(DuplicateError):
        db.create_candidate({"name": "孙悦", "phone": "13700007777", "position": existing["position"]})
    # 模拟另一个进程刚刚建好了同一条记录：apply_plan 收到唯一约束错误后回退为已有记录，不重复建档
    plan = {"op": "create", "name": "孙悦", "fields": {"name": "孙悦", "phone": "13700007777",
                                                     "position": existing["position"]}, "changes": {}}
    got = pipeline.apply_plan(plan, operator="HR小李", raw_text="", engine="test", source="web")
    assert got["id"] == existing["id"] and plan["op"] == "noop" and "没有重复建档" in plan["notes"][0]
    assert len(rows("孙悦")) == 1


def test_same_person_different_position_allowed_by_constraint():
    db.create_candidate({"name": "周宁", "phone": "13600006666", "position": "后端开发"})
    db.create_candidate({"name": "周宁", "phone": "13600006666", "position": "产品经理"})  # 不抛异常
    db.create_candidate({"name": "无手机", "position": "后端开发"})
    db.create_candidate({"name": "无手机", "position": "后端开发"})  # 手机号为空不受唯一约束限制
    assert len(rows("周宁")) == 2 and len(rows("无手机")) == 2


def test_mysql_placeholder_translation():
    assert to_mysql_sql("SELECT * FROM t WHERE a=? AND b=?") == "SELECT * FROM t WHERE a=%s AND b=%s"
    assert to_mysql_sql("SELECT name FROM t WHERE name LIKE 'idx_%' AND x=?") == \
        "SELECT name FROM t WHERE name LIKE 'idx_%%' AND x=%s"
    assert to_mysql_sql("SELECT '?' AS q, ? AS v") == "SELECT '?' AS q, %s AS v"


def test_position_change_on_existing_record_needs_confirmation():
    say("韩梅梅 产品经理 猎聘推荐 一面过了")
    cid = rows("韩梅梅")[0]["id"]
    plan = pipeline.plan_change({"candidate_id": cid, "name": "韩梅梅", "position": "运营专员"})
    assert any("应聘岗位将从「产品经理」改为「运营专员」" in r and "新增一条应聘记录" in r for r in plan["reasons"])

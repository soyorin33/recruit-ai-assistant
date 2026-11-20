"""数据一致性：演示数据在任意「当前时间」下生成都不能出现时间倒挂；业务操作后审计链保持完整。"""
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from app import pipeline, seed
from app.db import DB, db
from app.integrity import check, summary

# 覆盖一周内不同日期、不同钟点（含凌晨、周末、工作时间前后）
NOWS = [datetime(2026, 11, 16, 0, 5) + timedelta(hours=h) for h in range(0, 24 * 7, 5)]


@pytest.mark.parametrize("fake_now", NOWS, ids=lambda d: d.strftime("%a-%H%M"))
def test_seed_has_no_time_travel(monkeypatch, fake_now):
    monkeypatch.setattr(seed, "now", lambda: fake_now)
    d = DB(os.path.join(tempfile.mkdtemp(), "seed.db"))
    seed.seed(d, "welcome")
    bad = {k: v for k, v in check(d).items() if v}
    assert not bad, bad
    assert all(c["updated_at"] <= fake_now.strftime("%Y-%m-%d %H:%M:%S") for c in d.all_candidates())


def test_schema_indexes_and_wal():
    s = summary(db)
    assert s["sqlite_integrity"] == "ok" and s["journal_mode"] == "wal"
    assert {"idx_candidates_name", "idx_candidates_phone", "idx_events_candidate"} <= set(s["indexes"])


def test_pipeline_keeps_audit_chain():
    db.reset()
    for t in ["张三 Java后端 BOSS来的，一面过了，明天下午3点二面", "张三二面过了", "张三一面挂了", "确认",
              "李雷 前端 内推 约了明天上午10点一面", "她的面试官换成林峰", "王芳 产品经理 猎聘 简历"]:
        pipeline.handle_message(t, operator="hr")
    bad = {k: v for k, v in check(db).items() if v}
    assert not bad, bad

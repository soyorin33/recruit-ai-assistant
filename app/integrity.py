"""数据一致性检查：枚举合法性、时间顺序、重复、孤儿记录，以及「事件流水回放 == 当前状态」的审计链完整性。

用法：python -m scripts.check_db（也被测试复用）
"""
from __future__ import annotations

import json

from .db import DB
from .domain import STAGES, STATUSES


def check(db: DB) -> dict[str, list]:
    cands = db.all_candidates()
    issues: dict[str, list] = {
        "非法阶段": [c["name"] for c in cands if c["stage"] not in STAGES],
        "非法状态": [c["name"] for c in cands if c["status"] not in STATUSES],
        "已入职但阶段不一致": [c["name"] for c in cands if (c["status"] == "已入职") != (c["stage"] == "已入职")],
        "更新时间早于建档时间": [c["name"] for c in cands if c["updated_at"] < c["created_at"]],
        "同手机号多人": [r["phone"] for r in db.q(
            "SELECT phone FROM candidates WHERE phone IS NOT NULL AND phone<>'' GROUP BY phone HAVING COUNT(*)>1")],
        "孤儿事件": [r["id"] for r in db.q("SELECT id FROM events WHERE candidate_id NOT IN (SELECT id FROM candidates)")],
        "孤儿同步映射": [r["candidate_id"] for r in db.q(
            "SELECT candidate_id FROM sync_map WHERE candidate_id NOT IN (SELECT id FROM candidates) UNION ALL "
            "SELECT candidate_id FROM tdocs_map WHERE candidate_id NOT IN (SELECT id FROM candidates)")],
        "缺少建档事件": [], "事件时间早于建档": [], "事件时间晚于最后更新": [], "流水回放与当前状态不一致": [],
    }
    for c in cands:
        events = db.q("SELECT * FROM events WHERE candidate_id=? ORDER BY ts, id", (c["id"],))
        if not events or events[0]["action"] != "create":
            issues["缺少建档事件"].append(c["name"])
        if events and events[0]["ts"] < c["created_at"]:
            issues["事件时间早于建档"].append(c["name"])
        if events and events[-1]["ts"] > c["updated_at"]:
            issues["事件时间晚于最后更新"].append(c["name"])
        stage = status = None
        for e in events:
            ch = json.loads(e["changes"] or "{}")
            if "stage" in ch and ch["stage"][1]:
                stage = ch["stage"][1]
            if "status" in ch and ch["status"][1]:
                status = ch["status"][1]
        if events and (stage, status) != (c["stage"], c["status"]):
            issues["流水回放与当前状态不一致"].append(f"{c['name']}：回放 {stage}/{status}，当前 {c['stage']}/{c['status']}")
    return issues


def summary(db: DB) -> dict:
    integrity = db.one("PRAGMA integrity_check")
    return {
        "sqlite_integrity": list(integrity.values())[0] if integrity else None,
        "journal_mode": list(db.one("PRAGMA journal_mode").values())[0],
        "tables": {t: db.one(f"SELECT COUNT(*) n FROM {t}")["n"]
                   for t in ("candidates", "events", "pending", "messages", "sync_map", "tdocs_map")},
        "indexes": sorted(r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")),
    }

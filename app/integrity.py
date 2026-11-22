"""数据一致性检查：枚举合法性、时间顺序、重复、孤儿记录，以及「事件流水回放 == 当前状态」的审计链完整性。

用法：python -m scripts.check_db（也被测试复用）
"""
from __future__ import annotations

import json

from .db import DB, TABLES
from .domain import STAGES, STATUSES


def check(db: DB) -> dict[str, list]:
    cands = db.all_candidates()
    issues: dict[str, list] = {
        "非法阶段": [c["name"] for c in cands if c["stage"] not in STAGES],
        "非法状态": [c["name"] for c in cands if c["status"] not in STATUSES],
        "已入职但阶段不一致": [c["name"] for c in cands if (c["status"] == "已入职") != (c["stage"] == "已入职")],
        "更新时间早于建档时间": [c["name"] for c in cands if c["updated_at"] < c["created_at"]],
        # 同一个人可以投多个岗位（手机号相同、岗位不同是合法的）；同手机号 + 同岗位才是重复建档
        "同手机号同岗位重复": [f"{r['phone']}·{r['position']}" for r in db.q(
            "SELECT phone, position FROM candidates WHERE phone IS NOT NULL AND phone<>'' "
            "GROUP BY phone, position HAVING COUNT(*)>1")],
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
    if db.dialect == "mysql":
        version = db.one("SELECT VERSION() AS v")["v"]
        checks = db.q("CHECK TABLE " + ", ".join(TABLES))
        integrity = "ok" if all(r.get("Msg_text") == "OK" for r in checks) else \
            "；".join(f"{r.get('Table')}: {r.get('Msg_text')}" for r in checks if r.get("Msg_text") != "OK")
        backend, mode = f"MySQL {version}", "InnoDB"
    else:
        row = db.one("PRAGMA integrity_check")
        integrity = list(row.values())[0] if row else None
        backend, mode = "SQLite", list(db.one("PRAGMA journal_mode").values())[0]
    return {
        "backend": backend,
        "integrity": integrity,
        "mode": mode,  # SQLite 为日志模式（wal），MySQL 为存储引擎
        "tables": {t: db.one(f"SELECT COUNT(*) n FROM {t}")["n"] for t in TABLES},
        "indexes": db.index_names(),
    }

"""候选人变更 → 外部表格 的异步同步队列（失败重试，不阻塞 HR 的消息处理）。

同步目标（各自独立开关、独立的行 ID 映射与状态）：
- wecom：企微智能表格（需要自建应用权限）
- tdocs ：腾讯文档智能表格（官方 MCP，只需个人 Token）
"""
from __future__ import annotations

import logging
import queue
import threading
import time

from . import bus
from .config import settings
from .db import db, ts

log = logging.getLogger("sync")


def _wecom_upsert(cand: dict, rid: str | None) -> str:
    from .wecom import api
    return api.smartsheet_upsert(cand, rid)


def _tdocs_upsert(cand: dict, rid: str | None) -> str:
    from . import tdocs
    return tdocs.upsert(cand, rid)


TARGETS = {
    "wecom": {"label": "企微智能表格", "enabled": lambda: settings.smartsheet_enabled, "upsert": _wecom_upsert},
    "tdocs": {"label": "腾讯文档", "enabled": lambda: settings.tdocs_enabled, "upsert": _tdocs_upsert},
}


def _new_status() -> dict:
    return {"enabled": False, "synced": 0, "failed": 0, "last_error": None, "last_sync_at": None}


statuses: dict[str, dict] = {name: _new_status() for name in TARGETS}
status = statuses["wecom"]  # 兼容旧字段：/api/status 的 smartsheet
_queues: dict[str, queue.Queue] = {}


def enqueue(candidate_id: int) -> None:
    for name, q in _queues.items():
        q.put((candidate_id, 0))


def _worker(name: str, q: queue.Queue) -> None:
    target, st = TARGETS[name], statuses[name]
    while True:
        cid, attempt = q.get()
        cand = db.get_candidate(cid)
        if not cand:
            continue
        try:
            rid = target["upsert"](cand, db.get_record_id(cid, name))
            db.set_record_id(cid, rid, name)
            st.update(synced=st["synced"] + 1, last_sync_at=ts(), last_error=None)
        except Exception as e:  # noqa: BLE001  网络抖动 / 限频：指数退避重试 3 次
            log.warning("同步%s失败（第 %s 次）：%s", target["label"], attempt + 1, e)
            st["last_error"] = str(e)[:200]
            if attempt < 3:
                threading.Timer(2 ** attempt, q.put, args=((cid, attempt + 1),)).start()
            else:
                st["failed"] += 1
        bus.publish("sync", {})
        time.sleep(0.2)  # 简单限速，避免触发接口频率限制


def start() -> None:
    for name, target in TARGETS.items():
        statuses[name]["enabled"] = target["enabled"]()
        if statuses[name]["enabled"] and name not in _queues:
            _queues[name] = q = queue.Queue()
            threading.Thread(target=_worker, args=(name, q), daemon=True, name=f"sync-{name}").start()
    # 腾讯文档首次接入（映射为空）时自动全量写入，部署后表格里马上就有数据
    if "tdocs" in _queues and not db.one("SELECT candidate_id FROM tdocs_map LIMIT 1"):
        full_sync()


def full_sync() -> int:
    """全量同步（首次接入时把历史数据灌入表格）。"""
    rows = db.q("SELECT id FROM candidates ORDER BY id")
    for r in rows:
        enqueue(r["id"])
    return len(rows)


def after_reset() -> None:
    """演示数据重置后：清空腾讯文档里的旧记录，再把新数据全量写入（后台执行）。"""
    if "tdocs" not in _queues:
        return

    def job() -> None:
        from . import tdocs
        try:
            n = tdocs.clear_records()
            log.info("已清空腾讯文档中的 %s 条旧记录", n)
        except Exception as e:  # noqa: BLE001
            statuses["tdocs"]["last_error"] = f"清空旧记录失败：{e}"[:200]
        full_sync()

    threading.Thread(target=job, daemon=True, name="tdocs-reset").start()

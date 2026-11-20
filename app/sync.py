"""候选人变更 → 企微智能表格 的异步同步队列（失败重试，不阻塞 HR 的消息处理）。"""
from __future__ import annotations

import logging
import queue
import threading
import time

from . import bus
from .config import settings
from .db import db, ts

log = logging.getLogger("sync")
_q: queue.Queue[tuple[int, int]] = queue.Queue()
status = {"enabled": False, "synced": 0, "failed": 0, "last_error": None, "last_sync_at": None}


def enqueue(candidate_id: int) -> None:
    if settings.smartsheet_enabled:
        _q.put((candidate_id, 0))


def _worker() -> None:
    from .wecom import api
    while True:
        cid, attempt = _q.get()
        cand = db.get_candidate(cid)
        if not cand:
            continue
        try:
            rid = api.smartsheet_upsert(cand, db.get_record_id(cid))
            db.set_record_id(cid, rid)
            status.update(synced=status["synced"] + 1, last_sync_at=ts(), last_error=None)
        except Exception as e:  # 网络抖动 / 限频：指数退避重试 3 次
            log.warning("同步智能表格失败（第 %s 次）：%s", attempt + 1, e)
            status["last_error"] = str(e)[:200]
            if attempt < 3:
                threading.Timer(2 ** attempt, _q.put, args=((cid, attempt + 1),)).start()
            else:
                status["failed"] += 1
        bus.publish("sync", {})
        time.sleep(0.2)  # 简单限速，避免触发接口频率限制


def start() -> None:
    status["enabled"] = settings.smartsheet_enabled
    if settings.smartsheet_enabled:
        threading.Thread(target=_worker, daemon=True, name="smartsheet-sync").start()


def full_sync() -> int:
    """全量同步（首次接入时把历史数据灌入智能表格）。"""
    rows = db.q("SELECT id FROM candidates")
    for r in rows:
        enqueue(r["id"])
    return len(rows)

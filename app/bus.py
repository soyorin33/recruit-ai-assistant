"""进程内事件总线：数据一变更就推送给所有打开的看板（SSE），实现「实时同步」。"""
from __future__ import annotations

import itertools
import threading
from collections import deque

_lock = threading.Lock()
_seq = itertools.count(1)
_events: deque[dict] = deque(maxlen=500)


def publish(kind: str, data: dict | None = None) -> None:
    with _lock:
        _events.append({"id": next(_seq), "kind": kind, "data": data or {}})


def since(last_id: int) -> list[dict]:
    with _lock:
        return [e for e in _events if e["id"] > last_id]


def latest_id() -> int:
    with _lock:
        return _events[-1]["id"] if _events else 0

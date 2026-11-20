"""SQLite 数据层：候选人主表 + 变更事件流水 + 待确认队列 + 消息记录。

生产环境中「智能表格」是 HR 看到的单一数据源；本地 SQLite 作为
处理缓存 / 事件审计 / 离线兜底，二者通过 sync 模块保持一致。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import now, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT, email TEXT,
    position TEXT, channel TEXT,
    stage TEXT NOT NULL DEFAULT '简历筛选',
    status TEXT NOT NULL DEFAULT '进行中',
    interviewer TEXT, interview_time TEXT,
    feedback TEXT, resume_summary TEXT,
    hr_owner TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    operator TEXT, action TEXT,
    changes TEXT,           -- {"field": [old, new]}
    stage_to TEXT, status_to TEXT,
    raw_text TEXT, engine TEXT, source TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_candidate ON events(candidate_id, ts);
CREATE INDEX IF NOT EXISTS idx_candidates_name ON candidates(name);
CREATE INDEX IF NOT EXISTS idx_candidates_phone ON candidates(phone);
CREATE INDEX IF NOT EXISTS idx_candidates_updated ON candidates(updated_at);
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    operator TEXT, source TEXT,
    raw_text TEXT, proposal TEXT, reasons TEXT,
    state TEXT NOT NULL DEFAULT 'open'   -- open / confirmed / cancelled
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    role TEXT NOT NULL,      -- user / bot
    operator TEXT, source TEXT,
    text TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS sync_map (
    candidate_id INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL
);
"""

TS_FMT = "%Y-%m-%d %H:%M:%S"


def ts(dt: datetime | None = None) -> str:
    return (dt or now()).strftime(TS_FMT)


class DB:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            # WAL：看板读取与消息写入互不阻塞；busy_timeout：自检 / 评测脚本并发访问时等待而不是报错
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ---------- 通用 ----------
    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def exec(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def reset(self) -> None:
        with self.lock:
            for t in ("candidates", "events", "pending", "messages", "sync_map"):
                self.conn.execute(f"DELETE FROM {t}")
            self.conn.execute("DELETE FROM sqlite_sequence")
            self.conn.commit()

    # ---------- 候选人 ----------
    def get_candidate(self, cid: int) -> dict | None:
        return self.one("SELECT * FROM candidates WHERE id=?", (cid,))

    def all_candidates(self) -> list[dict]:
        return self.q("SELECT * FROM candidates ORDER BY updated_at DESC")

    def find_by_phone(self, phone: str) -> list[dict]:
        return self.q("SELECT * FROM candidates WHERE phone=?", (phone,))

    def find_by_name(self, name: str) -> list[dict]:
        return self.q("SELECT * FROM candidates WHERE name=? ORDER BY updated_at DESC", (name,))

    def create_candidate(self, fields: dict, at: datetime | None = None) -> int:
        t = ts(at)
        data = {**fields, "created_at": t, "updated_at": t}
        cols = ",".join(data)
        marks = ",".join("?" for _ in data)
        return self.exec(f"INSERT INTO candidates ({cols}) VALUES ({marks})", tuple(data.values()))

    def update_candidate(self, cid: int, fields: dict, at: datetime | None = None) -> None:
        data = {**fields, "updated_at": ts(at)}
        sets = ",".join(f"{k}=?" for k in data)
        self.exec(f"UPDATE candidates SET {sets} WHERE id=?", (*data.values(), cid))

    # ---------- 事件 ----------
    def add_event(self, candidate_id: int, action: str, changes: dict, *, operator: str = "",
                  raw_text: str = "", engine: str = "", source: str = "", at: datetime | None = None) -> int:
        stage_to = changes.get("stage", [None, None])[1]
        status_to = changes.get("status", [None, None])[1]
        return self.exec(
            "INSERT INTO events (candidate_id, ts, operator, action, changes, stage_to, status_to, raw_text, engine, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (candidate_id, ts(at), operator, action, json.dumps(changes, ensure_ascii=False),
             stage_to, status_to, raw_text, engine, source),
        )

    def events_for(self, candidate_id: int) -> list[dict]:
        rows = self.q("SELECT * FROM events WHERE candidate_id=? ORDER BY ts DESC, id DESC", (candidate_id,))
        for r in rows:
            r["changes"] = json.loads(r["changes"] or "{}")
        return rows

    # ---------- 待确认 ----------
    def add_pending(self, operator: str, source: str, raw_text: str, proposal: dict, reasons: list[str]) -> int:
        return self.exec(
            "INSERT INTO pending (created_at, operator, source, raw_text, proposal, reasons) VALUES (?,?,?,?,?,?)",
            (ts(), operator, source, raw_text, json.dumps(proposal, ensure_ascii=False),
             json.dumps(reasons, ensure_ascii=False)),
        )

    def get_pending(self, pid: int) -> dict | None:
        row = self.one("SELECT * FROM pending WHERE id=?", (pid,))
        if row:
            row["proposal"] = json.loads(row["proposal"])
            row["reasons"] = json.loads(row["reasons"])
        return row

    def latest_open_pending(self, operator: str) -> dict | None:
        row = self.one("SELECT id FROM pending WHERE operator=? AND state='open' ORDER BY id DESC LIMIT 1", (operator,))
        return self.get_pending(row["id"]) if row else None

    def close_pending(self, pid: int, state: str) -> None:
        self.exec("UPDATE pending SET state=? WHERE id=?", (state, pid))

    # ---------- 消息 ----------
    def add_message(self, role: str, text: str, *, operator: str = "", source: str = "web",
                    payload: dict | None = None) -> dict:
        mid = self.exec(
            "INSERT INTO messages (ts, role, operator, source, text, payload) VALUES (?,?,?,?,?,?)",
            (ts(), role, operator, source, text, json.dumps(payload or {}, ensure_ascii=False)),
        )
        return self.get_message(mid)

    def get_message(self, mid: int) -> dict:
        row = self.one("SELECT * FROM messages WHERE id=?", (mid,))
        row["payload"] = json.loads(row["payload"] or "{}")
        return row

    def recent_messages(self, limit: int = 60) -> list[dict]:
        rows = self.q("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["payload"] = json.loads(r["payload"] or "{}")
        return list(reversed(rows))

    # ---------- 智能表格映射 ----------
    def get_record_id(self, cid: int) -> str | None:
        row = self.one("SELECT record_id FROM sync_map WHERE candidate_id=?", (cid,))
        return row["record_id"] if row else None

    def set_record_id(self, cid: int, record_id: str) -> None:
        self.exec("INSERT OR REPLACE INTO sync_map (candidate_id, record_id) VALUES (?,?)", (cid, record_id))


db = DB(settings.db_path)


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)

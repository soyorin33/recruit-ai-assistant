"""数据层：候选人主表 + 变更事件流水 + 待确认队列 + 消息记录。支持 MySQL 与 SQLite 两种后端。

- 设置 DATABASE_URL=mysql://用户:密码@主机:3306/库名 时使用 MySQL（docker-compose 部署默认）
- 未设置时使用 SQLite 文件 DB_PATH（本机零配置快速体验、单元测试）
业务代码只写标准 SQL 和 ? 占位符，方言差异（占位符、建表语句、UPSERT、自增重置、表结构查询）都收敛在本文件。
时间统一存为「YYYY-MM-DD HH:MM:SS」文本：两种数据库下排序、比较、截取行为一致。

去重相关的约束：同一手机号 + 同一岗位只能有一条应聘记录（唯一索引 uq_candidates_phone_position），
作为代码层加锁之外的最后一道防线（例如将来多进程部署时）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import now, settings

log = logging.getLogger("db")

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT, email TEXT,
    position TEXT, channel TEXT,
    stage TEXT NOT NULL DEFAULT '简历筛选',
    status TEXT NOT NULL DEFAULT '进行中',
    interviewer TEXT, interview_time TEXT,
    feedback TEXT, resume_summary TEXT,
    match_score INTEGER, match_detail TEXT,
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
CREATE TABLE IF NOT EXISTS push_chats (
    chatid TEXT PRIMARY KEY,           -- 群聊 chatid；单聊为成员 userid
    chattype TEXT NOT NULL DEFAULT 'group',
    enabled INTEGER NOT NULL DEFAULT 1, -- 在群里发「关闭推送」/「开启推送」切换
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tdocs_map (
    candidate_id INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failures (      -- 待人工处理：系统没能自动处理的消息 / 文件 / 同步
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,                     -- message / unrecognized / resume / image / sync
    source TEXT, operator TEXT,
    raw_text TEXT, error TEXT,
    ref_id INTEGER,                         -- 同步失败时为候选人编号
    state TEXT NOT NULL DEFAULT 'open',     -- open / resolved
    resolved_by TEXT, resolved_at TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_failures_state ON failures(state, id);
"""

# MySQL：建索引 / 主键的文本列必须是 VARCHAR；排序规则用 utf8mb4_bin（区分大小写，与 SQLite 一致——
# 腾讯文档 record_id、企微 chatid 都区分大小写）
_T = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"
MYSQL_SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS candidates (
        id INT PRIMARY KEY AUTO_INCREMENT,
        name VARCHAR(100) NOT NULL,
        phone VARCHAR(32), email VARCHAR(255),
        position VARCHAR(100), channel VARCHAR(100),
        stage VARCHAR(32) NOT NULL DEFAULT '简历筛选',
        status VARCHAR(32) NOT NULL DEFAULT '进行中',
        interviewer VARCHAR(255), interview_time VARCHAR(19),
        feedback TEXT, resume_summary TEXT,
        match_score INT, match_detail TEXT,
        hr_owner VARCHAR(100),
        created_at VARCHAR(19) NOT NULL, updated_at VARCHAR(19) NOT NULL,
        KEY idx_candidates_name (name), KEY idx_candidates_phone (phone),
        KEY idx_candidates_updated (updated_at),
        UNIQUE KEY uq_candidates_phone_position (phone, position)
    ) {_T}""",
    f"""CREATE TABLE IF NOT EXISTS events (
        id INT PRIMARY KEY AUTO_INCREMENT,
        candidate_id INT NOT NULL,
        ts VARCHAR(19) NOT NULL,
        operator VARCHAR(100), action VARCHAR(32),
        changes MEDIUMTEXT,
        stage_to VARCHAR(32), status_to VARCHAR(32),
        raw_text MEDIUMTEXT, engine VARCHAR(32), source VARCHAR(32),
        KEY idx_events_ts (ts), KEY idx_events_candidate (candidate_id, ts)
    ) {_T}""",
    f"""CREATE TABLE IF NOT EXISTS pending (
        id INT PRIMARY KEY AUTO_INCREMENT,
        created_at VARCHAR(19) NOT NULL,
        operator VARCHAR(100), source VARCHAR(32),
        raw_text MEDIUMTEXT, proposal MEDIUMTEXT, reasons TEXT,
        state VARCHAR(16) NOT NULL DEFAULT 'open'
    ) {_T}""",
    f"""CREATE TABLE IF NOT EXISTS messages (
        id INT PRIMARY KEY AUTO_INCREMENT,
        ts VARCHAR(19) NOT NULL,
        role VARCHAR(16) NOT NULL,
        operator VARCHAR(100), source VARCHAR(32),
        text MEDIUMTEXT, payload MEDIUMTEXT
    ) {_T}""",
    f"CREATE TABLE IF NOT EXISTS sync_map (candidate_id INT PRIMARY KEY, record_id VARCHAR(128) NOT NULL) {_T}",
    f"""CREATE TABLE IF NOT EXISTS push_chats (
        chatid VARCHAR(128) PRIMARY KEY,
        chattype VARCHAR(16) NOT NULL DEFAULT 'group',
        enabled TINYINT NOT NULL DEFAULT 1,
        first_seen VARCHAR(19) NOT NULL, last_seen VARCHAR(19) NOT NULL
    ) {_T}""",
    f"CREATE TABLE IF NOT EXISTS tdocs_map (candidate_id INT PRIMARY KEY, record_id VARCHAR(128) NOT NULL) {_T}",
    f"""CREATE TABLE IF NOT EXISTS failures (
        id INT PRIMARY KEY AUTO_INCREMENT,
        ts VARCHAR(19) NOT NULL,
        kind VARCHAR(32) NOT NULL,
        source VARCHAR(32), operator VARCHAR(100),
        raw_text MEDIUMTEXT, error TEXT,
        ref_id INT,
        state VARCHAR(16) NOT NULL DEFAULT 'open',
        resolved_by VARCHAR(100), resolved_at VARCHAR(19), note TEXT,
        KEY idx_failures_state (state, id)
    ) {_T}""",
]
TABLES = ("candidates", "events", "pending", "messages", "sync_map", "push_chats", "tdocs_map", "failures")
RESET_TABLES = ("candidates", "events", "pending", "messages", "sync_map", "tdocs_map", "failures")  # 推送目标群不随演示重置清空

TS_FMT = "%Y-%m-%d %H:%M:%S"


def ts(dt: datetime | None = None) -> str:
    return (dt or now()).strftime(TS_FMT)


class DuplicateError(Exception):
    """违反唯一约束（如同一手机号 + 同一岗位重复建档）。"""


def to_mysql_sql(sql: str) -> str:
    """? 占位符 → %s；字面量 % → %%（PyMySQL 用 % 格式化）。单引号字符串里的 ? 不替换。"""
    out, in_str = [], False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
            out.append(ch)
        elif ch == "%":
            out.append("%%")
        elif ch == "?" and not in_str:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


def parse_mysql_url(url: str) -> dict:
    u = urlparse(url)
    return {"host": u.hostname or "127.0.0.1", "port": u.port or 3306, "user": unquote(u.username or "root"),
            "password": unquote(u.password or ""), "database": (u.path or "/recruit").lstrip("/") or "recruit"}


class DB:
    def __init__(self, target: str):
        """target：mysql://… 连接串，或 SQLite 文件路径。"""
        self.lock = threading.RLock()
        self.dialect = "mysql" if target.startswith(("mysql://", "mysql+pymysql://")) else "sqlite"
        if self.dialect == "mysql":
            self.cfg = parse_mysql_url(target)
            self._connect_mysql(wait=60)
            with self.lock:
                for stmt in MYSQL_SCHEMA:
                    self._raw(stmt)
                self._migrate()
        else:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(target, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            with self.lock:
                # WAL：看板读取与消息写入互不阻塞；busy_timeout：自检 / 评测脚本并发访问时等待而不是报错
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA busy_timeout=5000")
                self.conn.executescript(SQLITE_SCHEMA)
                self._migrate()
                self.conn.commit()

    # ---------- 连接 ----------
    def _connect_mysql(self, wait: int = 0) -> None:
        import pymysql
        deadline = time.time() + wait
        while True:
            try:
                self.conn = pymysql.connect(**self.cfg, charset="utf8mb4", autocommit=True, connect_timeout=10,
                                            cursorclass=pymysql.cursors.DictCursor)
                return
            except pymysql.err.OperationalError as e:
                if time.time() >= deadline:
                    raise
                log.info("等待 MySQL 就绪：%s", e)
                time.sleep(2)

    def _raw(self, sql: str, args: tuple = ()) -> tuple[list[dict], int]:
        """执行一条 SQL，返回 (结果行, lastrowid)。MySQL 断线（如超过 wait_timeout）时自动重连重试一次。"""
        if self.dialect == "sqlite":
            try:
                cur = self.conn.execute(sql, args)
            except sqlite3.IntegrityError as e:
                raise DuplicateError(str(e)) from e
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            self.conn.commit()
            return rows, cur.lastrowid
        import pymysql
        for attempt in (0, 1):
            try:
                with self.conn.cursor() as cur:
                    cur.execute(to_mysql_sql(sql), args)
                    rows = list(cur.fetchall()) if cur.description else []
                    return rows, cur.lastrowid
            except pymysql.err.IntegrityError as e:
                if e.args and e.args[0] == 1062:  # Duplicate entry
                    raise DuplicateError(str(e)) from e
                raise
            except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
                if attempt:
                    raise
                log.warning("MySQL 连接已断开，重连中")
                self._connect_mysql(wait=10)
        return [], 0

    # ---------- 结构迁移 ----------
    def columns(self, table: str) -> set[str]:
        if self.dialect == "mysql":
            rows, _ = self._raw("SELECT COLUMN_NAME AS c FROM information_schema.COLUMNS "
                                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=?", (table,))
            return {r["c"] for r in rows}
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def index_names(self) -> list[str]:
        if self.dialect == "mysql":
            rows, _ = self._raw("SELECT DISTINCT INDEX_NAME AS n FROM information_schema.STATISTICS "
                                "WHERE TABLE_SCHEMA=DATABASE() AND INDEX_NAME<>'PRIMARY'")
        else:
            rows, _ = self._raw("SELECT name AS n FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
        return sorted(r["n"] for r in rows)

    def _migrate(self) -> None:
        """轻量迁移：给旧版本数据库补上新增的列和唯一约束。"""
        cols = self.columns("candidates")
        for name, typ in (("match_score", "INTEGER"), ("match_detail", "TEXT")):
            if name not in cols:
                self._raw(f"ALTER TABLE candidates ADD COLUMN {name} {typ}")
        if "uq_candidates_phone_position" not in self.index_names():
            sql = ("CREATE UNIQUE INDEX uq_candidates_phone_position ON candidates(phone, position)")
            try:
                self._raw(sql)
            except Exception as e:  # noqa: BLE001  旧数据里已有重复：不阻止启动，由 check_db 报告
                log.warning("唯一约束（手机号 + 岗位）未能创建，旧数据中存在重复：%s", e)

    # ---------- 通用 ----------
    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return self._raw(sql, args)[0]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def exec(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            return self._raw(sql, args)[1]

    def reset(self) -> None:
        with self.lock:
            for t in RESET_TABLES:
                if self.dialect == "mysql":
                    self._raw(f"TRUNCATE TABLE {t}")  # 同时重置自增编号
                else:
                    self._raw(f"DELETE FROM {t}")
            if self.dialect == "sqlite":
                self._raw("DELETE FROM sqlite_sequence")

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
    # 同步映射：候选人 → 外部表格的行 ID（target: wecom=企微智能表格，tdocs=腾讯文档智能表格）
    MAP_TABLES = {"wecom": "sync_map", "tdocs": "tdocs_map"}

    def get_record_id(self, cid: int, target: str = "wecom") -> str | None:
        row = self.one(f"SELECT record_id FROM {self.MAP_TABLES[target]} WHERE candidate_id=?", (cid,))
        return row["record_id"] if row else None

    def set_record_id(self, cid: int, record_id: str, target: str = "wecom") -> None:
        verb = "REPLACE INTO" if self.dialect == "mysql" else "INSERT OR REPLACE INTO"
        self.exec(f"{verb} {self.MAP_TABLES[target]} (candidate_id, record_id) VALUES (?,?)", (cid, record_id))

    # 日报 / 提醒的推送目标：招聘助手被 @ 过的群（不随演示数据重置而清空）
    def touch_push_chat(self, chatid: str, chattype: str = "group", auto_enable: bool = True) -> bool:
        """记录会话；返回是否为新会话。新群默认开启推送，单聊默认不推送（需发「开启推送」）。"""
        now_ = ts()
        if self.one("SELECT chatid FROM push_chats WHERE chatid=?", (chatid,)):
            self.exec("UPDATE push_chats SET last_seen=? WHERE chatid=?", (now_, chatid))
            return False
        self.exec("INSERT INTO push_chats (chatid, chattype, enabled, first_seen, last_seen) VALUES (?,?,?,?,?)",
                  (chatid, chattype, int(auto_enable), now_, now_))
        return True

    def set_push_enabled(self, chatid: str, chattype: str, enabled: bool) -> None:
        self.touch_push_chat(chatid, chattype, auto_enable=enabled)
        self.exec("UPDATE push_chats SET enabled=? WHERE chatid=?", (int(enabled), chatid))

    def push_chats(self) -> list[dict]:
        return self.q("SELECT * FROM push_chats WHERE enabled=1 ORDER BY first_seen")

    # ---------- 待人工处理 ----------
    def add_failure(self, kind: str, *, raw_text: str = "", error: str = "", source: str = "", operator: str = "",
                    ref_id: int | None = None) -> int:
        # 同一条同步失败只保留一条未处理记录，避免反复失败刷屏
        if ref_id is not None:
            row = self.one("SELECT id FROM failures WHERE kind=? AND ref_id=? AND state='open'", (kind, ref_id))
            if row:
                self.exec("UPDATE failures SET ts=?, error=? WHERE id=?", (ts(), error[:1000], row["id"]))
                return row["id"]
        return self.exec("INSERT INTO failures (ts, kind, source, operator, raw_text, error, ref_id) VALUES (?,?,?,?,?,?,?)",
                         (ts(), kind, source, operator, raw_text[:5000], error[:1000], ref_id))

    def get_failure(self, fid: int) -> dict | None:
        return self.one("SELECT * FROM failures WHERE id=?", (fid,))

    def list_failures(self, state: str = "open", limit: int = 100) -> list[dict]:
        return self.q("SELECT * FROM failures WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit))

    def resolve_failure(self, fid: int, operator: str, note: str = "") -> None:
        self.exec("UPDATE failures SET state='resolved', resolved_by=?, resolved_at=?, note=? WHERE id=? AND state='open'",
                  (operator, ts(), note[:500], fid))

    def clear_record_ids(self, target: str) -> None:
        self.exec(f"DELETE FROM {self.MAP_TABLES[target]}")


db = DB(settings.database_url or settings.db_path)


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)

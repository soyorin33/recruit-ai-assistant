"""把 SQLite 中的数据原样迁移到 MySQL（保留所有编号，腾讯文档 / 企微表格的行映射因此继续有效）。

用法（在项目根目录）：
    python -m scripts.migrate_to_mysql                     # 从 DB_PATH 迁到 DATABASE_URL
    python -m scripts.migrate_to_mysql data/recruit.db mysql://用户:密码@主机:3306/recruit
目标库里已有候选人数据时拒绝执行，加 --force 会先清空目标库的这些表再迁移。
迁移后自动运行一致性检查，并核对每张表的行数。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import DB, TABLES  # noqa: E402
from app.integrity import check  # noqa: E402


def migrate(sqlite_path: str, mysql_url: str, force: bool = False) -> dict[str, int]:
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = DB(mysql_url)  # 自动建表
    if dst.dialect != "mysql":
        raise SystemExit("目标必须是 mysql:// 连接串")
    if dst.one("SELECT id FROM candidates LIMIT 1") and not force:
        raise SystemExit("目标 MySQL 里已有候选人数据；确认要覆盖请加 --force")
    counts = {}
    for t in TABLES:
        if force:
            dst.exec(f"DELETE FROM {t}")
        cols_dst = dst.columns(t)
        rows = [dict(r) for r in src.execute(f"SELECT * FROM {t}")] if _has_table(src, t) else []
        for r in rows:
            r = {k: v for k, v in r.items() if k in cols_dst}
            dst.exec(f"INSERT INTO {t} ({','.join(r)}) VALUES ({','.join('?' for _ in r)})", tuple(r.values()))
        counts[t] = len(rows)
    for t, n in counts.items():  # 行数核对
        got = dst.one(f"SELECT COUNT(*) n FROM {t}")["n"]
        if got != n:
            raise SystemExit(f"{t} 行数不一致：SQLite {n}，MySQL {got}")
    bad = {k: v for k, v in check(dst).items() if v}
    if bad:
        print("⚠️ 迁移完成，但一致性检查发现问题：", bad)
    return counts


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlite_path", nargs="?", default=settings.db_path)
    ap.add_argument("mysql_url", nargs="?", default=settings.database_url)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if not Path(args.sqlite_path).exists():
        sys.exit(f"SQLite 文件不存在：{args.sqlite_path}")
    if not args.mysql_url.startswith("mysql"):
        sys.exit("请提供 MySQL 连接串（或在 .env 中设置 DATABASE_URL）")
    counts = migrate(args.sqlite_path, args.mysql_url, args.force)
    print("✅ 迁移完成：" + "，".join(f"{t} {n}" for t, n in counts.items()))
    print(f"   目标：{args.mysql_url.split('@')[-1]}")


if __name__ == "__main__":
    main()

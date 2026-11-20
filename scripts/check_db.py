"""数据库一致性检查（只读）。用法：python -m scripts.check_db [数据库路径]"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import DB  # noqa: E402
from app.integrity import check, summary  # noqa: E402


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else settings.db_path
    if not Path(path).exists():
        sys.exit(f"数据库不存在：{path}")
    db = DB(path)
    s = summary(db)
    print(f"数据库：{path}")
    print(f"SQLite 完整性：{s['sqlite_integrity']}｜日志模式：{s['journal_mode']}")
    print("表行数：" + "，".join(f"{k} {v}" for k, v in s["tables"].items()))
    print("索引：" + "、".join(s["indexes"]))
    issues = check(db)
    bad = {k: v for k, v in issues.items() if v}
    for k in issues:
        print(f"{'❌' if issues[k] else '✅'} {k}" + (f"：{issues[k][:5]}" if issues[k] else ""))
    print(f"\n共 {len(issues)} 项检查，{len(issues) - len(bad)} 项通过")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

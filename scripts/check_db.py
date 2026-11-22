"""数据库一致性检查（只读）。用法：python -m scripts.check_db [数据库路径或 mysql:// 连接串]

不带参数时检查当前配置的数据库（DATABASE_URL，未设置则为 SQLite 文件 DB_PATH）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import DB  # noqa: E402
from app.integrity import check, summary  # noqa: E402


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else (settings.database_url or settings.db_path)
    is_mysql = target.startswith("mysql")
    if not is_mysql and not Path(target).exists():
        sys.exit(f"数据库不存在：{target}")
    db = DB(target)
    s = summary(db)
    shown = target.split("@")[-1] if is_mysql else target  # 不打印账号密码
    print(f"数据库：{s['backend']}（{shown}）")
    print(f"完整性：{s['integrity']}｜{'存储引擎' if is_mysql else '日志模式'}：{s['mode']}")
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

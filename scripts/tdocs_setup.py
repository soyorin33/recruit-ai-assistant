"""一键在你的腾讯文档里创建「招聘候选人总表」（智能表格），并把 ID 写入 .env。

前提：.env 里已填好 TDOCS_TOKEN（腾讯文档 MCP 个人 Token）。
用法（在项目根目录）：
    python -m scripts.tdocs_setup            # 新建表格（或补齐已有表格的列），写入 .env
    python -m scripts.tdocs_setup --tools    # 只连接并列出腾讯文档 MCP 提供的全部工具，用于排查
    python -m scripts.tdocs_setup --no-write # 不改 .env，只打印结果
之后启动服务，候选人数据会自动全量写入，并随每次变更实时更新。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BASE_DIR, settings  # noqa: E402
from app.tdocs import COLUMNS, MCPClient, TDocsError, field_specs  # noqa: E402

TITLE = "招聘候选人总表"


def setup(c: MCPClient, file_id: str | None = None, title: str = TITLE) -> dict:
    notes: list[str] = []
    created, url = False, None
    if not file_id:
        r = c.call("manage.create_file", {"title": title, "file_type": "smartsheet"})
        file_id, url, created = r["file_id"], r.get("url"), True
    sheets = c.call("smartsheet.list_tables", {"file_id": file_id}).get("sheets") or []
    if not sheets:
        raise TDocsError("这个智能表格里没有工作表")
    sheet_id = sheets[0]["sheet_id"]
    ids = {"file_id": file_id, "sheet_id": sheet_id}

    fields = c.call("smartsheet.list_fields", ids).get("fields") or []
    have = {f.get("field_title") for f in fields}
    missing = [s for s in field_specs() if s["field_title"] not in have]
    if missing:
        c.call("smartsheet.add_fields", {**ids, "fields": missing})

    if created:  # 新建的智能表格自带默认列和空行，清理掉（失败不影响使用）
        ours = {title for _, title, _ in COLUMNS}
        extra = [f["field_id"] for f in fields if f.get("field_title") not in ours and f.get("field_id")]
        if extra:
            try:
                c.call("smartsheet.delete_fields", {**ids, "field_ids": extra})
            except TDocsError as e:
                notes.append(f"默认列没删掉（可在腾讯文档里手动删除）：{e}")
        try:
            recs = c.call("smartsheet.list_records", {**ids, "limit": 100}).get("records") or []
            rids = [r["record_id"] for r in recs if r.get("record_id")]
            if rids:
                c.call("smartsheet.delete_records", {**ids, "record_ids": rids})
        except TDocsError as e:
            notes.append(f"默认空行没删掉：{e}")
    return {**ids, "url": url, "created": created, "added_fields": len(missing), "notes": notes}


def write_env(path: Path, updates: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for key, value in updates.items():
        line = f"{key}={value}"
        if re.search(rf"^{key}=.*$", text, flags=re.M):
            text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
        else:
            text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    path.write_text(text, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tools", action="store_true", help="只列出可用工具")
    ap.add_argument("--no-write", action="store_true", help="不写入 .env")
    args = ap.parse_args()
    if not settings.tdocs_token:
        sys.exit("请先在 .env 里填写 TDOCS_TOKEN（腾讯文档 MCP 个人 Token）")
    c = MCPClient(settings.tdocs_mcp_url, settings.tdocs_token)
    try:
        if args.tools:
            tools = c.list_tools()
            print(f"腾讯文档 MCP 共提供 {len(tools)} 个工具：")
            for t in tools:
                print("  -", t)
            return
        res = setup(c, settings.tdocs_file_id or None)
    except TDocsError as e:
        sys.exit(f"❌ {e}")
    print(("✅ 已新建" if res["created"] else "✅ 已检查") + f"「{TITLE}」，新增 {res['added_fields']} 列")
    print(f"   file_id={res['file_id']}  sheet_id={res['sheet_id']}")
    if res["url"]:
        print(f"   打开：{res['url']}")
    for n in res["notes"]:
        print("   ⚠️", n)
    if not args.no_write:
        updates = {"TDOCS_FILE_ID": res["file_id"], "TDOCS_SHEET_ID": res["sheet_id"]}
        if res["url"]:
            updates["TDOCS_URL"] = res["url"]
        write_env(BASE_DIR / ".env", updates)
        print("   已写入 .env，重启服务后候选人数据会自动全量同步到这张表")


if __name__ == "__main__":
    main()

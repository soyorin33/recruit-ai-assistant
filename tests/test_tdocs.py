"""腾讯文档 MCP 同步：本地启动一个模拟的腾讯文档 MCP 服务器（真实 HTTP + JSON-RPC），完整走一遍：

initialize（会话 ID）→ tools/list（工具名用下划线风格，验证自动匹配）→ 新建智能表格 → 建列 → 清理默认列 / 空行
→ 候选人变更经异步队列写入 → 更新同一行 → 演示数据重置时清空旧记录 → VIP 错误码提示 → SSE 格式响应解析
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

TOKEN = "test-tdocs-token"


class FakeTDocs:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.sessions: set[str] = set()
        self.fail_with: str | None = None
        self.sse = False

    # ---- 工具实现 ----
    def tool(self, name: str, a: dict) -> dict:
        self.calls.append((name, a))
        if self.fail_with:
            return {"error": self.fail_with, "trace_id": "t"}
        if name == "manage_create_file":
            fid = "f_" + uuid.uuid4().hex[:6]
            self.files[fid] = {"sheet_id": "s1", "fields": [{"field_id": "d1", "field_title": "文本", "field_type": "text"}],
                               "records": {"r_default": {"文本": "x"}}}
            return {"file_id": fid, "url": f"https://docs.qq.com/smartsheet/{fid}", "error": ""}
        f = self.files[a["file_id"]]
        if name == "smartsheet_list_tables":
            return {"sheets": [{"sheet_id": f["sheet_id"], "title": "工作表1"}]}
        assert a["sheet_id"] == f["sheet_id"]
        if name == "smartsheet_list_fields":
            return {"fields": f["fields"], "total": len(f["fields"])}
        if name == "smartsheet_add_fields":
            for spec in a["fields"]:
                f["fields"].append({"field_id": "fld" + uuid.uuid4().hex[:4], **spec})
            return {"fields": a["fields"]}
        if name == "smartsheet_delete_fields":
            f["fields"] = [x for x in f["fields"] if x["field_id"] not in a["field_ids"]]
            return {}
        titles = {x["field_title"]: x["field_type"] for x in f["fields"]}
        if name in ("smartsheet_add_records", "smartsheet_update_records"):
            out = []
            for rec in a["records"]:
                row = {}
                for fv in rec["field_values"]:
                    assert fv["field"] in titles, f"不存在的列：{fv['field']}"
                    row[fv["field"]] = fv
                if name == "smartsheet_add_records":
                    rid = "r_" + uuid.uuid4().hex[:6]
                    f["records"][rid] = row
                    out.append({"record_id": rid})
                else:
                    f["records"][rec["record_id"]].update(row)
            return {"records": out}
        if name == "smartsheet_list_records":
            ids = list(f["records"])
            off, lim = a.get("offset", 0), a.get("limit", 100)
            page = ids[off:off + lim]
            return {"records": [{"record_id": r, "field_values": []} for r in page],
                    "has_more": off + lim < len(ids), "next": off + lim, "total": len(ids)}
        if name == "smartsheet_delete_records":
            for r in a["record_ids"]:
                f["records"].pop(r, None)
            return {}
        raise AssertionError(name)


def make_handler(fake: FakeTDocs):
    tools = ["manage_create_file", "smartsheet_list_tables", "smartsheet_list_fields", "smartsheet_add_fields",
             "smartsheet_delete_fields", "smartsheet_add_records", "smartsheet_update_records",
             "smartsheet_list_records", "smartsheet_delete_records", "get_content"]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401
            pass

        def _send(self, obj: dict, headers: dict | None = None):
            if fake.sse:
                body = f"event: message\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()
                ctype = "text/event-stream"
            else:
                body, ctype = json.dumps(obj, ensure_ascii=False).encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.headers.get("Authorization") != TOKEN:
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if "id" not in msg:  # 通知
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            method = msg["method"]
            if method == "initialize":
                sid = uuid.uuid4().hex
                fake.sessions.add(sid)
                return self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-tdocs"}}}, {"Mcp-Session-Id": sid})
            if self.headers.get("Mcp-Session-Id") not in fake.sessions:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "tools/list":
                return self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "tools": [{"name": t, "inputSchema": {"type": "object"}} for t in tools]}})
            if method == "tools/call":
                p = msg["params"]
                if p["name"] not in tools:
                    return self._send({"jsonrpc": "2.0", "id": msg["id"],
                                       "error": {"code": -32601, "message": "tool not found"}})
                data = fake.tool(p["name"], p["arguments"])
                return self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                    "isError": bool(data.get("error"))}})
            self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": method}})

    return H


@pytest.fixture()
def fake():
    fk = FakeTDocs()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fk))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fk.url = f"http://127.0.0.1:{srv.server_port}/openapi/mcp"
    yield fk
    srv.shutdown()


@pytest.fixture()
def tdocs_on(fake):
    """把配置指向模拟服务器，并新建好表格；结束后还原，避免影响其他测试。"""
    from app import sync, tdocs
    from app.config import settings
    from scripts.tdocs_setup import setup
    old = (settings.tdocs_token, settings.tdocs_mcp_url, settings.tdocs_file_id, settings.tdocs_sheet_id)
    settings.tdocs_token, settings.tdocs_mcp_url = TOKEN, fake.url
    tdocs._client = None
    res = setup(tdocs.client())
    settings.tdocs_file_id, settings.tdocs_sheet_id = res["file_id"], res["sheet_id"]
    yield fake, res
    settings.tdocs_token, settings.tdocs_mcp_url, settings.tdocs_file_id, settings.tdocs_sheet_id = old
    sync._queues.pop("tdocs", None)
    sync.statuses["tdocs"]["enabled"] = False
    tdocs._client = None


def _wait(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_setup_creates_sheet_and_cleans_defaults(tdocs_on):
    fake, res = tdocs_on
    f = fake.files[res["file_id"]]
    from app.tdocs import COLUMNS
    assert [x["field_title"] for x in f["fields"]] == [t for _, t, _ in COLUMNS]  # 默认列已删、列顺序正确
    assert f["records"] == {}  # 默认空行已删
    stage = next(x for x in f["fields"] if x["field_title"] == "当前阶段")
    assert stage["field_type"] == "singleSelect"
    assert [o["text"] for o in stage["property_single_select"]["options"]][:2] == ["简历筛选", "初试"]
    assert res["url"].startswith("https://docs.qq.com/")
    # 再跑一次：幂等，不重复建列
    from app import tdocs
    from scripts.tdocs_setup import setup
    again = setup(tdocs.client(), res["file_id"])
    assert again["added_fields"] == 0 and not again["created"]


def test_upsert_add_then_update_with_masking(tdocs_on):
    fake, res = tdocs_on
    from app import tdocs
    cand = {"id": 7, "name": "测试甲", "phone": "13812345678", "position": "Java后端", "stage": "初试",
            "status": "进行中", "match_score": 82, "feedback": None}
    rid = tdocs.upsert(cand, None)
    row = fake.files[res["file_id"]]["records"][rid]
    assert row["手机"]["text_value"]["items"][0]["text"] == "138****5678"
    assert row["当前阶段"]["option_value"]["items"][0]["text"] == "初试"
    assert row["匹配度"]["number_value"] == 82 and row["编号"]["number_value"] == 7
    assert "面试评价" not in row  # 空值不写
    assert tdocs.upsert({**cand, "stage": "复试"}, rid) == rid
    assert fake.files[res["file_id"]]["records"][rid]["当前阶段"]["option_value"]["items"][0]["text"] == "复试"
    assert len(fake.files[res["file_id"]]["records"]) == 1


def test_pipeline_change_syncs_to_tdocs(tdocs_on):
    fake, res = tdocs_on
    from app import pipeline, sync
    from app.db import db
    sync.start()
    assert sync.statuses["tdocs"]["enabled"]
    pipeline.handle_message("陆文涛 Java后端 BOSS来的 一面过了 手机13900001111", operator="HR小王")
    cid = db.find_by_name("陆文涛")[0]["id"]
    assert _wait(lambda: db.get_record_id(cid, "tdocs"))
    rid = db.get_record_id(cid, "tdocs")
    assert _wait(lambda: rid in fake.files[res["file_id"]]["records"])
    row = fake.files[res["file_id"]]["records"][rid]
    assert row["姓名"]["text_value"]["items"][0]["text"] == "陆文涛"
    assert row["当前阶段"]["option_value"]["items"][0]["text"] == "复试"
    pipeline.handle_message("陆文涛 复试也过了，发offer", operator="HR小王")
    assert _wait(lambda: fake.files[res["file_id"]]["records"][rid]["当前阶段"]["option_value"]["items"][0]["text"] == "Offer")
    assert db.get_record_id(cid, "tdocs") == rid  # 更新同一行，不新增
    assert db.get_record_id(cid, "wecom") is None  # 两个同步目标的映射互不干扰


def test_reset_clears_remote_then_resyncs(tdocs_on):
    fake, res = tdocs_on
    from app import sync, tdocs
    from app.db import db
    for i in range(3):
        tdocs.upsert({"id": 900 + i, "name": f"旧数据{i}"}, None)
    sync.start()
    n = db.one("SELECT COUNT(*) n FROM candidates")["n"]
    db.clear_record_ids("tdocs")
    sync.after_reset()
    recs = fake.files[res["file_id"]]["records"]
    assert _wait(lambda: len(recs) == n and db.one("SELECT COUNT(*) n FROM tdocs_map")["n"] == n)
    assert not any(v.get("姓名", {}).get("text_value", {}).get("items", [{}])[0].get("text", "").startswith("旧数据")
                   for v in recs.values())


def test_sse_responses_and_session_expiry(fake):
    from app.tdocs import MCPClient
    fake.sse = True
    c = MCPClient(fake.url, TOKEN)
    assert "smartsheet_add_records" in c.list_tools()
    r = c.call("manage.create_file", {"title": "x", "file_type": "smartsheet"})
    fake.sessions.clear()  # 服务端会话过期 → 客户端自动重新 initialize
    assert c.call("smartsheet.list_tables", {"file_id": r["file_id"]})["sheets"][0]["sheet_id"] == "s1"


def test_error_hints(fake):
    from app.tdocs import MCPClient, TDocsError
    c = MCPClient(fake.url, "wrong-token")
    with pytest.raises(TDocsError, match="鉴权失败"):
        c.list_tools()
    c = MCPClient(fake.url, TOKEN)
    fake.fail_with = "400007 vip required"
    with pytest.raises(TDocsError, match="VIP"):
        c.call("manage.create_file", {"title": "x", "file_type": "smartsheet"})
    fake.fail_with = None
    with pytest.raises(TDocsError, match="没有提供工具"):
        c.call("smartsheet.nonexistent_tool", {})


def test_write_env(tmp_path):
    from scripts.tdocs_setup import write_env
    p = tmp_path / ".env"
    p.write_text("LLM_API_KEY=abc\nTDOCS_FILE_ID=old\n")
    write_env(p, {"TDOCS_FILE_ID": "new", "TDOCS_SHEET_ID": "s1"})
    assert p.read_text() == "LLM_API_KEY=abc\nTDOCS_FILE_ID=new\nTDOCS_SHEET_ID=s1\n"

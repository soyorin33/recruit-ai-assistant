"""腾讯文档（docs.qq.com）官方 MCP 接入：候选人总表实时写入腾讯文档「智能表格」。

MCP（Model Context Protocol）是让 AI 应用调用外部工具的标准协议。腾讯文档官方提供了 MCP 服务：
  地址 https://docs.qq.com/openapi/mcp，请求头 Authorization: <个人 Token>，传输方式 Streamable HTTP。
这里的后端就是一个 MCP 客户端：initialize → tools/list → tools/call，协议是 JSON-RPC 2.0，
响应可能是 JSON，也可能是 SSE（text/event-stream），两种都要能解析。

用到的工具（参数以 tools/list 返回的 Schema 为准）：
  manage.create_file（新建智能表格） smartsheet.list_tables / list_fields / add_fields
  smartsheet.add_records / update_records / list_records / delete_records / delete_fields
工具名有的客户端显示为 smartsheet.add_records，有的为 smartsheet_add_records，这里自动匹配。
"""
from __future__ import annotations

import json
import logging
import threading

import httpx

from .config import settings
from .domain import STAGES, STATUSES, mask_phone

log = logging.getLogger("tdocs")
PROTOCOL_VERSION = "2025-03-26"

ERROR_HINTS = {
    "400006": "Token 鉴权失败：请重新生成 TDOCS_TOKEN",
    "400007": "腾讯文档 VIP 权限不足：该操作需要腾讯文档会员",
    "400008": "腾讯文档积分不足",
}


class TDocsError(Exception):
    pass


def _hint(text: str) -> str:
    for code, hint in ERROR_HINTS.items():
        if code in text:
            return f"{hint}（{text[:200]}）"
    return text[:300]


class MCPClient:
    """最小的 MCP Streamable HTTP 客户端（只用到 initialize / tools/list / tools/call）。"""

    def __init__(self, url: str, token: str, timeout: float = 30):
        self.url, self.token, self.timeout = url, token, timeout
        self.session_id: str | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._initialized = False
        self.tools: dict[str, dict] = {}

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "Authorization": self.token, "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, payload: dict) -> httpx.Response:
        try:
            r = httpx.post(self.url, json=payload, headers=self._headers(), timeout=self.timeout)
        except httpx.HTTPError as e:
            raise TDocsError(f"连接腾讯文档 MCP 失败：{type(e).__name__}: {e}") from e
        if r.status_code in (401, 403):
            raise TDocsError(_hint(f"鉴权失败 HTTP {r.status_code}: {r.text}"))
        if r.status_code == 404 and self.session_id:  # 会话过期：按规范需要重新 initialize
            raise _SessionExpired()
        if r.status_code >= 400:
            raise TDocsError(_hint(f"HTTP {r.status_code}: {r.text}"))
        return r

    @staticmethod
    def _parse(r: httpx.Response, want_id: int) -> dict:
        if "text/event-stream" in r.headers.get("content-type", ""):
            for block in r.text.split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
                if not data:
                    continue
                msg = json.loads(data)
                if msg.get("id") == want_id:
                    return msg
            raise TDocsError("SSE 响应中没有找到对应的 JSON-RPC 结果")
        return r.json()

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        rid = self._id
        r = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        if method == "initialize" and r.headers.get("mcp-session-id"):
            self.session_id = r.headers["mcp-session-id"]
        msg = self._parse(r, rid)
        if "error" in msg:
            err = msg["error"]
            raise TDocsError(_hint(f"{err.get('code')}: {err.get('message')} {err.get('data') or ''}"))
        return msg.get("result") or {}

    def _ensure(self) -> None:
        if self._initialized:
            return
        self.session_id = None
        self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                 "clientInfo": {"name": "recruit-ai-assistant", "version": "1.0"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools, cursor = [], None
        while True:
            res = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = {t["name"]: t for t in tools}
        self._initialized = True

    def resolve(self, name: str) -> str:
        """smartsheet.add_records ↔ smartsheet_add_records ↔ add_records（不同服务端命名风格）。"""
        if name in self.tools:
            return name
        variants = [name.replace(".", "_"), name.replace(".", "-"), name.replace(".", "__")]
        for v in variants:
            if v in self.tools:
                return v
        short = name.split(".")[-1]
        hits = [t for t in self.tools if t.endswith(short) and name.split(".")[0] in t]
        if len(hits) == 1:
            return hits[0]
        raise TDocsError(f"腾讯文档 MCP 没有提供工具 {name}（可用工具：{', '.join(sorted(self.tools))[:300]}）")

    def call(self, name: str, args: dict) -> dict:
        with self._lock:
            for attempt in (0, 1):
                try:
                    self._ensure()
                    res = self._rpc("tools/call", {"name": self.resolve(name), "arguments": args})
                    break
                except _SessionExpired:
                    self._initialized = False
                    if attempt:
                        raise TDocsError("MCP 会话反复过期")
        data = res.get("structuredContent")
        if data is None:
            text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = {"text": text}
        if res.get("isError") or (isinstance(data, dict) and data.get("error")):
            detail = data.get("error") if isinstance(data, dict) and data.get("error") else json.dumps(data, ensure_ascii=False)
            raise TDocsError(_hint(f"{name} 调用失败：{detail}"))
        return data if isinstance(data, dict) else {"result": data}

    def list_tools(self) -> list[str]:
        with self._lock:
            self._ensure()
            return sorted(self.tools)


class _SessionExpired(Exception):
    pass


# ======================= 智能表格：候选人总表 =======================
# (字段 key, 列标题, 腾讯文档字段类型)。阶段 / 状态用单选，方便在腾讯文档里按阶段分组、建看板视图。
COLUMNS = [
    ("id", "编号", "number"), ("name", "姓名", "text"), ("phone", "手机", "text"),
    ("position", "应聘岗位", "text"), ("channel", "渠道", "text"), ("stage", "当前阶段", "singleSelect"),
    ("status", "状态", "singleSelect"), ("interviewer", "面试官", "text"), ("interview_time", "面试时间", "text"),
    ("match_score", "匹配度", "number"), ("feedback", "面试评价", "text"), ("resume_summary", "简历摘要", "text"),
    ("hr_owner", "负责HR", "text"), ("updated_at", "最后更新", "text"),
]
OPTIONS = {"stage": STAGES, "status": STATUSES}


def field_specs() -> list[dict]:
    specs = []
    for key, title, ftype in COLUMNS:
        spec = {"field_title": title, "field_type": ftype}
        if ftype == "text":
            spec["property_text"] = {}
        elif ftype == "number":
            spec["property_number"] = {"decimal_places": 0}
        elif ftype == "singleSelect":
            spec["property_single_select"] = {"options": [{"text": o, "style": i % 10 + 1}
                                                          for i, o in enumerate(OPTIONS[key])]}
        specs.append(spec)
    return specs


def record_values(cand: dict) -> list[dict]:
    """候选人 → FieldValueEntry 列表（空值不写；手机号脱敏）。"""
    out = []
    for key, title, ftype in COLUMNS:
        v = cand.get(key)
        if key == "phone":
            v = mask_phone(v)
        if v in (None, ""):
            continue
        if ftype == "number":
            try:
                out.append({"field": title, "number_value": float(v)})
            except (TypeError, ValueError):
                continue
        elif ftype == "singleSelect":
            out.append({"field": title, "option_value": {"items": [{"text": str(v)}]}})
        else:
            out.append({"field": title, "text_value": {"items": [{"text": str(v), "type": "text"}]}})
    return out


_client: MCPClient | None = None


def client() -> MCPClient:
    global _client
    if _client is None:
        _client = MCPClient(settings.tdocs_mcp_url, settings.tdocs_token)
    return _client


def _ids() -> dict:
    return {"file_id": settings.tdocs_file_id, "sheet_id": settings.tdocs_sheet_id}


def upsert(cand: dict, record_id: str | None) -> str:
    values = record_values(cand)
    if record_id:
        client().call("smartsheet.update_records", {**_ids(), "records": [
            {"record_id": record_id, "field_values": values}]})
        return record_id
    data = client().call("smartsheet.add_records", {**_ids(), "records": [{"field_values": values}]})
    recs = data.get("records") or []
    rid = recs[0].get("record_id") if recs and isinstance(recs[0], dict) else None
    if not rid:
        raise TDocsError(f"add_records 没有返回 record_id：{json.dumps(data, ensure_ascii=False)[:200]}")
    return rid


def list_record_ids() -> list[str]:
    ids, offset = [], 0
    while True:
        data = client().call("smartsheet.list_records", {**_ids(), "offset": offset, "limit": 100,
                                                         "field_titles": ["编号"]})
        ids += [r["record_id"] for r in data.get("records") or [] if r.get("record_id")]
        if not data.get("has_more"):
            return ids
        offset = data.get("next") or offset + 100


def clear_records() -> int:
    """清空表格里的全部记录（演示数据重置时使用，避免旧数据和新数据重复）。"""
    ids = list_record_ids()
    for i in range(0, len(ids), 100):
        client().call("smartsheet.delete_records", {**_ids(), "record_ids": ids[i:i + 100]})
    return len(ids)


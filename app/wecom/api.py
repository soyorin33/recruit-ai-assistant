"""企业微信服务端 API：access_token、应用消息、临时素材、智能表格、群机器人 Webhook。"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from ..config import settings

log = logging.getLogger("wecom")


def _base() -> str:
    return settings.wecom_api_base


class WeComError(RuntimeError):
    pass


class TokenCache:
    def __init__(self):
        self._token, self._expire = "", 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expire - 300:
                return self._token
            r = httpx.get(f"{_base()}/gettoken", params={"corpid": settings.wecom_corp_id,
                                                      "corpsecret": settings.wecom_secret}, timeout=10)
            data = r.json()
            if data.get("errcode", 0) != 0:
                raise WeComError(f"获取 access_token 失败：{data}")
            self._token, self._expire = data["access_token"], time.time() + data.get("expires_in", 7200)
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = ""


token_cache = TokenCache()


def _post(path: str, body: dict, retry: bool = True) -> dict:
    r = httpx.post(f"{_base()}/{path}", params={"access_token": token_cache.get()}, json=body, timeout=15)
    data = r.json()
    if data.get("errcode") in (40014, 42001) and retry:  # token 失效，刷新后重试一次
        token_cache.invalidate()
        return _post(path, body, retry=False)
    if data.get("errcode", 0) != 0:
        raise WeComError(f"{path} 调用失败：{data}")
    return data


# ---------------- 应用消息 ----------------
def send_text(to_user: str, content: str) -> None:
    _post("message/send", {"touser": to_user, "msgtype": "text", "agentid": int(settings.wecom_agent_id or 0),
                           "text": {"content": content[:2000]}})


def download_media(media_id: str) -> tuple[str, bytes]:
    r = httpx.get(f"{_base()}/media/get", params={"access_token": token_cache.get(), "media_id": media_id}, timeout=30)
    if r.headers.get("content-type", "").startswith("application/json"):
        raise WeComError(f"下载素材失败：{r.text}")
    disp = r.headers.get("content-disposition", "")
    filename = disp.split("filename=")[-1].strip('"') if "filename=" in disp else f"{media_id}.pdf"
    return filename, r.content


# ---------------- 智能表格 ----------------
# 文档：https://developer.work.weixin.qq.com/document/path/99907 （添加记录）/ 99908（更新记录）
# 约定：智能表格中按 SMARTSHEET_COLUMNS 的标题建好列，类型均为「文本」。
SMARTSHEET_COLUMNS = {
    "id": "编号", "name": "姓名", "phone": "手机", "position": "应聘岗位", "channel": "渠道",
    "stage": "当前阶段", "status": "状态", "interviewer": "面试官", "interview_time": "面试时间",
    "feedback": "面试评价", "resume_summary": "简历摘要", "hr_owner": "负责HR", "updated_at": "最后更新",
}


def _values(cand: dict) -> dict:
    from ..domain import mask_phone
    vals = {}
    for key, title in SMARTSHEET_COLUMNS.items():
        v = cand.get(key)
        if key == "phone":
            v = mask_phone(v)
        vals[title] = [{"type": "text", "text": str(v) if v not in (None, "") else ""}]
    return vals


def smartsheet_get_fields() -> list[dict]:
    data = _post("wedoc/smartsheet/get_fields", {"docid": settings.smartsheet_docid,
                                                 "sheet_id": settings.smartsheet_sheet_id})
    return data.get("fields", [])


def smartsheet_upsert(cand: dict, record_id: str | None) -> str:
    body = {"docid": settings.smartsheet_docid, "sheet_id": settings.smartsheet_sheet_id,
            "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE"}
    if record_id:
        _post("wedoc/smartsheet/update_records", {**body, "records": [{"record_id": record_id,
                                                                       "values": _values(cand)}]})
        return record_id
    data = _post("wedoc/smartsheet/add_records", {**body, "records": [{"values": _values(cand)}]})
    return data["records"][0]["record_id"]


# ---------------- 群机器人 ----------------
def webhook_markdown(content: str) -> None:
    r = httpx.post(settings.wecom_webhook_url, json={"msgtype": "markdown", "markdown": {"content": content[:4000]}},
                   timeout=10)
    data = r.json()
    if data.get("errcode", 0) != 0:
        raise WeComError(f"群机器人推送失败：{data}")

"""企业微信「智能机器人」API 模式适配：HR 在企微群里 @招聘助手 即可记录 / 查询。

协议要点（企微开发者文档「接收消息」「被动回复消息」「回调和回复的加解密方案」）：
- 回调与回复均为 JSON：收到 {"encrypt": ...}，回复 {"encrypt", "msgsignature", "timestamp", "nonce"}；
  企业内部智能机器人的 ReceiveId 为空字符串。
- 用「流式消息」被动回复：首次回复带上自定义 stream.id；之后企微会带着 stream.id 反复回调刷新，
  直到回复 finish=true（最长 6 分钟）。我们借此把智能体的每一步进度实时显示在群里。
- 图片 / 文件以 URL 推送（5 分钟有效），内容用与回调相同的 AESKey 做 AES-256-CBC 加密。
- msgid 可能重复回调，需要排重。

注：from.userid 只有在机器人创建者为超级管理员时才是明文 UserID，否则为加密 ID，此处仅作为对话隔离的标识。
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import OrderedDict

import httpx

from .. import pipeline
from ..config import settings
from .crypto import WXBizMsgCrypt

log = logging.getLogger("aibot")
FAST_WAIT = 1.5  # 规则模式通常几毫秒就完成：先等一小会儿，能直接给出最终结果就不走流式刷新
STREAM_TTL = 600


class StreamStore:
    """stream.id → 当前内容；msgid → stream.id（排重）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.streams: OrderedDict[str, dict] = OrderedDict()
        self.by_msgid: OrderedDict[str, str] = OrderedDict()

    def create(self, msgid: str, content: str) -> str:
        sid = uuid.uuid4().hex
        with self._lock:
            self._gc()
            self.streams[sid] = {"content": content, "finish": False, "ts": time.time()}
            self.by_msgid[msgid] = sid
        return sid

    def update(self, sid: str, content: str, finish: bool = False) -> None:
        with self._lock:
            if sid in self.streams:
                self.streams[sid].update(content=content, finish=finish)

    def get(self, sid: str) -> dict | None:
        with self._lock:
            return dict(self.streams[sid]) if sid in self.streams else None

    def sid_for(self, msgid: str) -> str | None:
        with self._lock:
            return self.by_msgid.get(msgid)

    def _gc(self) -> None:
        cutoff = time.time() - STREAM_TTL
        for sid in [s for s, v in self.streams.items() if v["ts"] < cutoff]:
            self.streams.pop(sid, None)
        while len(self.by_msgid) > 1000:
            self.by_msgid.popitem(last=False)


store = StreamStore()


def crypt() -> WXBizMsgCrypt:
    return WXBizMsgCrypt(settings.wecom_aibot_token, settings.wecom_aibot_aes_key, "")


def stream_reply(sid: str, nonce: str) -> dict:
    st = store.get(sid) or {"content": "这条消息已过期，请重新发送。", "finish": True}
    return crypt().encrypt_json({"msgtype": "stream", "stream": {
        "id": sid, "finish": st["finish"], "content": st["content"][:20000]}}, nonce)


def _download(item: dict) -> bytes:
    r = httpx.get(item["url"], timeout=30)
    r.raise_for_status()
    return crypt().decrypt_bytes(r.content)


def run(data: dict, progress, download) -> str:
    """与传输方式无关的消息处理（URL 回调、长连接共用）：走与网页 / 自建应用完全相同的流水线。

    download(item) 负责下载并解密 data["image"] / data["file"]，返回明文字节。
    """
    user = (data.get("from") or {}).get("userid") or "group-user"
    mtype = data.get("msgtype")
    if mtype == "image":
        msg = pipeline.handle_image("群聊图片.png", download(data["image"]), operator=user,
                                    source="wecom_group", progress=progress)
    elif mtype == "file":
        content = download(data["file"])
        from ..vision import sniff_mime
        if sniff_mime(content):
            msg = pipeline.handle_image("群聊图片.png", content, operator=user, source="wecom_group")
        else:
            name = "简历.pdf" if content.startswith(b"%PDF") else "简历.txt"
            msg = pipeline.handle_resume(name, content, operator=user, source="wecom_group")
    else:
        msg = pipeline.handle_message(extract_text(data), operator=user, source="wecom_group", progress=progress)
    return msg["text"]


def prepare(data: dict) -> tuple[str, dict]:
    """归一化一条消息，返回 (kind, data)：kind 为 process（需处理）/ help（空消息回帮助）/ ignore。"""
    mtype = data.get("msgtype")
    if mtype == "mixed" and not extract_text(data):  # 只有图片的图文混排 → 按图片处理
        imgs = [i for i in data.get("mixed", {}).get("msg_item", []) if i.get("msgtype") == "image"]
        if imgs:
            data, mtype = {**data, "msgtype": "image", "image": imgs[0]["image"]}, "image"
    if mtype not in ("text", "voice", "mixed", "image", "file"):
        return "ignore", data
    if mtype in ("text", "voice", "mixed") and not extract_text(data):
        return "help", data
    return "process", data


def _process(sid: str, data: dict) -> None:
    """后台执行，把进度与结果写入流（URL 回调模式下企微会反复来取）。"""
    try:
        store.update(sid, run(data, lambda t: store.update(sid, t), _download), finish=True)
    except Exception as e:
        log.exception("智能机器人消息处理失败")
        store.update(sid, f"处理失败：{e}", finish=True)


def extract_text(data: dict) -> str:
    mtype = data.get("msgtype")
    if mtype == "text":
        text = data.get("text", {}).get("content", "")
    elif mtype == "voice":
        text = data.get("voice", {}).get("content", "")
    elif mtype == "mixed":
        text = " ".join(i.get("text", {}).get("content", "") for i in data.get("mixed", {}).get("msg_item", [])
                        if i.get("msgtype") == "text")
    else:
        text = ""
    return re.sub(r"^\s*(@\S+\s*)+", "", text).strip()  # 去掉开头的 @机器人


def handle(data: dict, nonce: str) -> dict | None:
    """处理一次解密后的回调，返回加密后的被动回复（None 表示回复空包）。"""
    if data.get("msgtype") == "stream":  # 流式刷新：返回最新进度 / 最终结果
        return stream_reply(data.get("stream", {}).get("id", ""), nonce)
    msgid = data.get("msgid") or uuid.uuid4().hex
    if sid := store.sid_for(msgid):  # 重复回调：返回同一条流，不重复处理
        return stream_reply(sid, nonce)
    kind, data = prepare(data)
    if kind == "ignore":
        return None
    if kind == "help":
        sid = store.create(msgid, pipeline.HELP_TEXT)
        store.update(sid, pipeline.HELP_TEXT, finish=True)
        return stream_reply(sid, nonce)
    sid = store.create(msgid, "🤔 收到，正在处理…")
    t = threading.Thread(target=_process, args=(sid, data), daemon=True, name=f"aibot-{sid[:6]}")
    t.start()
    t.join(FAST_WAIT)
    return stream_reply(sid, nonce)

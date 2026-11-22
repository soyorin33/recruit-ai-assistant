"""企业微信「智能机器人」长连接模式：服务器主动连到企微，群里 @招聘助手 的消息经 WebSocket 推送过来。

与 URL 回调模式相比：不需要公网域名、HTTPS、备案，也不需要 Token / EncodingAESKey，只要 Bot ID + Secret。

协议（对照官方 Node SDK @wecom/aibot-node-sdk 1.0.7 实现）：
- 连接 wss://openws.work.weixin.qq.com，发送 {"cmd": "aibot_subscribe", "headers": {"req_id"}, "body": {"bot_id", "secret"}}
- 每 30 秒 {"cmd": "ping"}；连续 2 次没有回执视为断线，指数退避重连（最长 30 秒）
- 收到 {"cmd": "aibot_msg_callback", "headers": {"req_id"}, "body": {msgid, msgtype, from, text|image|file|...}}
- 用同一个 req_id 回复 {"cmd": "aibot_respond_msg", "body": {"msgtype": "stream", "stream": {id, finish, content}}}，
  同一 req_id 的回复要串行发送：发一条、等回执、再发下一条
- 没有 cmd 的帧是回执：{"headers": {"req_id"}, "errcode", "errmsg"}，按 req_id 前缀区分认证 / 心跳 / 回复
- 图片 / 文件给的是 URL + 每条消息独立的 aeskey（AES-256-CBC，IV 取密钥前 16 字节）
- 主动推送 {"cmd": "aibot_send_msg", "body": {"chatid", "msgtype": "markdown", "markdown": {"content"}}}：
  日报 / 跟进提醒由招聘助手自己发到群里，群里只需要一个机器人
- 同一个机器人只能保持一条长连接：别处建立新连接时，旧连接会收到 disconnected_event，此时暂停 5 分钟再重连
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict

import httpx

from .. import pipeline
from ..config import settings
from . import aibot
from .crypto import decrypt_media, media_key

log = logging.getLogger("aibot-ws")

CMD_SUBSCRIBE = "aibot_subscribe"
CMD_PING = "ping"
CMD_RESPOND = "aibot_respond_msg"
CMD_WELCOME = "aibot_respond_welcome_msg"
CMD_CALLBACK = "aibot_msg_callback"
CMD_EVENT = "aibot_event_callback"
CMD_SEND = "aibot_send_msg"  # 主动推送：向群（chatid）或单聊（userid）发 markdown，不依赖回调
PROGRESS_INTERVAL = 0.8  # 进度刷新的最小间隔（秒），避免刷屏和触发频控
KICK_RETRY = 300  # 被别处的新连接踢下线后，等 5 分钟再重连（避免两个实例互踢，也避免一次误操作让服务永久离线）

status = {"enabled": False, "state": "未启用", "connected_at": None, "last_error": None, "received": 0}
PUSH_ON_RE = re.compile(r"^(开启|打开|订阅)(日报)?(推送|提醒)$")
PUSH_OFF_RE = re.compile(r"^(关闭|取消|停止)(日报)?(推送|提醒)$")


def truncate_bytes(text: str, limit: int = 4000) -> str:
    """企微 markdown 消息按 UTF-8 字节限长（约 4096），超长时截断并提示。"""
    raw = text.encode()
    if len(raw) <= limit:
        return text
    suffix = "\n\n……（内容较长，完整版见看板）"
    return raw[:limit - len(suffix.encode())].decode(errors="ignore").rstrip() + suffix


class AuthError(Exception):
    pass


def req_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def download(item: dict) -> bytes:
    r = httpx.get(item["url"], timeout=30, follow_redirects=True)
    r.raise_for_status()
    return decrypt_media(r.content, media_key(item["aeskey"]))


class AibotWS:
    def __init__(self, bot_id: str, secret: str, url: str, heartbeat: float = 30, ack_timeout: float = 5):
        self.bot_id, self.secret, self.url = bot_id, secret, url
        self.heartbeat, self.ack_timeout = heartbeat, ack_timeout
        self.ws = None
        self._send_lock = threading.Lock()
        self._acks: dict[str, tuple[threading.Event, dict]] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._stop = threading.Event()
        self._missed = 0
        self.kicked = False

    # ---------- 生命周期 ----------
    def start(self) -> None:
        threading.Thread(target=self.run_forever, daemon=True, name="aibot-ws").start()

    def stop(self) -> None:
        self._stop.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass

    def run_forever(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            authed = False
            try:
                status["state"] = "连接中"
                authed = self._session()
            except AuthError as e:
                status.update(state="认证失败", last_error=str(e))
                log.error("智能机器人认证失败：%s（请核对 Bot ID / Secret）", e)
            except Exception as e:  # noqa: BLE001  网络异常、心跳超时等
                status.update(state="已断开", last_error=f"{type(e).__name__}: {e}"[:200])
                log.warning("智能机器人长连接断开：%s", e)
            if self._stop.is_set():
                break
            if self.kicked:
                status.update(state="已被新连接替换",
                              last_error=f"同一个机器人在别处建立了新的长连接，{KICK_RETRY // 60} 分钟后重试")
                log.warning(status["last_error"])
                self._stop.wait(KICK_RETRY)
                self.kicked = False
                continue
            delay = 1.0 if authed else min(delay * 2, 30)
            self._stop.wait(delay)
        status["state"] = "已停止"

    def _session(self) -> bool:
        """一次完整的连接：认证 → 心跳 → 收消息，直到断开。返回是否曾认证成功。"""
        from websockets.sync.client import connect

        authed = threading.Event()
        with connect(self.url, open_timeout=10, ping_interval=None, max_size=8 * 1024 * 1024) as ws:
            self.ws, self._missed = ws, 0
            self._send({"cmd": CMD_SUBSCRIBE, "headers": {"req_id": req_id(CMD_SUBSCRIBE)},
                        "body": {"bot_id": self.bot_id, "secret": self.secret}})
            hb = threading.Thread(target=self._heartbeat_loop, args=(ws, authed), daemon=True, name="aibot-ws-hb")
            hb.start()
            try:
                for raw in ws:
                    try:
                        frame = json.loads(raw)
                    except ValueError:
                        continue
                    self._on_frame(frame, authed)
            finally:
                self.ws = None
                for ev, _ in list(self._acks.values()):
                    ev.set()  # 断线时释放所有等待回执的线程
        return authed.is_set()

    def _heartbeat_loop(self, ws, authed: threading.Event) -> None:
        while not self._stop.wait(self.heartbeat):
            if self.ws is not ws:
                return
            if not authed.is_set():
                continue
            if self._missed >= 2:
                log.warning("连续 2 次心跳无回执，主动断开重连")
                ws.close()
                return
            self._missed += 1
            try:
                self._send({"cmd": CMD_PING, "headers": {"req_id": req_id(CMD_PING)}})
            except Exception:  # noqa: BLE001
                return

    # ---------- 收发 ----------
    def _send(self, frame: dict) -> None:
        ws = self.ws
        if ws is None:
            raise ConnectionError("长连接未建立")
        with self._send_lock:
            ws.send(json.dumps(frame, ensure_ascii=False))

    def _on_frame(self, frame: dict, authed: threading.Event) -> None:
        cmd = frame.get("cmd") or ""
        rid = (frame.get("headers") or {}).get("req_id", "")
        if cmd == CMD_CALLBACK:
            status["received"] += 1
            threading.Thread(target=self._on_message, args=(rid, frame.get("body") or {}), daemon=True,
                             name=f"aibot-ws-{rid[-6:]}").start()
        elif cmd == CMD_EVENT:
            self._on_event(rid, frame.get("body") or {})
        elif rid.startswith(CMD_SUBSCRIBE):
            if frame.get("errcode", 0) != 0:
                raise AuthError(f"{frame.get('errmsg')} (errcode={frame.get('errcode')})")
            authed.set()
            status.update(state="已连接", connected_at=time.strftime("%Y-%m-%d %H:%M:%S"), last_error=None)
            log.info("智能机器人长连接认证成功")
        elif rid.startswith(CMD_PING):
            self._missed = 0
        elif rid in self._acks:
            ev, holder = self._acks[rid]
            holder["frame"] = frame
            ev.set()

    def reply(self, rid: str, body: dict, cmd: str = CMD_RESPOND) -> dict | None:
        """发送一条回复并等待回执（同一 req_id 由同一个线程顺序调用，天然串行）。"""
        ev, holder = threading.Event(), {}
        self._acks[rid] = (ev, holder)
        try:
            self._send({"cmd": cmd, "headers": {"req_id": rid}, "body": body})
            if not ev.wait(self.ack_timeout):
                log.warning("回复回执超时：%s", rid)
                return None
        finally:
            self._acks.pop(rid, None)
        ack = holder.get("frame")
        if ack and ack.get("errcode", 0) != 0:
            log.warning("回复被拒绝：%s %s", ack.get("errcode"), ack.get("errmsg"))
        return ack

    def _stream(self, rid: str, sid: str, content: str, finish: bool) -> None:
        self.reply(rid, {"msgtype": "stream", "stream": {"id": sid, "finish": finish,
                                                         "content": aibot.clean_markdown(content)[:20000]}})

    # ---------- 业务 ----------
    def _on_message(self, rid: str, body: dict) -> None:
        msgid = body.get("msgid") or rid
        if msgid in self._seen:  # 排重：网络重试可能导致同一条消息推送两次
            return
        self._seen[msgid] = None
        while len(self._seen) > 1000:
            self._seen.popitem(last=False)

        chatid, chattype = self._remember_chat(body)
        kind, body = aibot.prepare(body)
        if kind == "ignore":
            return
        sid = uuid.uuid4().hex
        text = aibot.extract_text(body) if kind == "process" else ""
        if chatid and (PUSH_ON_RE.match(text) or PUSH_OFF_RE.match(text)):
            on = bool(PUSH_ON_RE.match(text))
            from ..db import db
            db.set_push_enabled(chatid, chattype, on)
            where = "这个群" if chattype == "group" else "你"
            self._stream(rid, sid, (f"✅ 已开启：每天 {settings.alert_time} 跟进提醒、{settings.report_time} 招聘日报会推送给{where}。"
                                    if on else f"已关闭：不再给{where}推送日报和提醒。发「开启推送」可恢复。"), True)
            return
        try:
            if kind == "help":
                self._stream(rid, sid, pipeline.HELP_TEXT, True)
                return
            self._stream(rid, sid, "🤔 收到，正在处理…", False)
            last = [0.0]

            def progress(text: str) -> None:
                if time.time() - last[0] >= PROGRESS_INTERVAL:
                    last[0] = time.time()
                    self._stream(rid, sid, text, False)

            final = aibot.run(body, progress, download)
        except Exception as e:  # noqa: BLE001
            log.exception("智能机器人消息处理失败")
            final = f"处理失败：{e}"
        try:
            self._stream(rid, sid, final, True)
        except Exception:  # noqa: BLE001
            log.exception("最终结果发送失败")

    @staticmethod
    def _remember_chat(body: dict) -> tuple[str | None, str]:
        """群里被 @ 过就记住这个群（作为日报推送目标）；单聊记住用户但默认不推送。"""
        chattype = body.get("chattype") or "single"
        chatid = body.get("chatid") if chattype == "group" else (body.get("from") or {}).get("userid")
        if chatid:
            try:
                from ..db import db
                if db.touch_push_chat(chatid, chattype, auto_enable=chattype == "group"):
                    log.info("记住新的%s：%s", "群聊" if chattype == "group" else "单聊", chatid[:12])
            except Exception:  # noqa: BLE001
                log.exception("记录推送目标失败")
        return chatid, chattype

    def send_markdown(self, chatid: str, content: str) -> None:
        """主动推送一条 markdown 到群 / 单聊，等待回执；失败抛异常。"""
        ack = self.reply(req_id(CMD_SEND), {"chatid": chatid, "msgtype": "markdown",
                                            "markdown": {"content": truncate_bytes(aibot.clean_markdown(content))}},
                         CMD_SEND)
        if ack is None:
            raise TimeoutError("主动推送无回执")
        if ack.get("errcode", 0) != 0:
            raise RuntimeError(f"主动推送被拒绝：errcode={ack.get('errcode')} {ack.get('errmsg')}")

    def _on_event(self, rid: str, body: dict) -> None:
        etype = (body.get("event") or {}).get("eventtype")
        if etype == "enter_chat":  # 用户打开与机器人的单聊：5 秒内可回欢迎语
            threading.Thread(target=self.reply, args=(rid, {"msgtype": "text", "text": {"content": pipeline.HELP_TEXT}},
                                                      CMD_WELCOME), daemon=True).start()
        elif etype == "disconnected_event":
            self.kicked = True
            if self.ws:
                self.ws.close()


def probe(bot_id: str, secret: str, url: str, timeout: float = 8) -> tuple[bool, str]:
    """只做一次认证就断开（自检用）。注意：会把正在运行的服务实例的长连接挤掉。"""
    from websockets.sync.client import connect
    rid = req_id(CMD_SUBSCRIBE)
    with connect(url, open_timeout=timeout, ping_interval=None) as ws:
        ws.send(json.dumps({"cmd": CMD_SUBSCRIBE, "headers": {"req_id": rid},
                            "body": {"bot_id": bot_id, "secret": secret}}))
        end = time.time() + timeout
        while time.time() < end:
            frame = json.loads(ws.recv(timeout=max(0.1, end - time.time())))
            if (frame.get("headers") or {}).get("req_id") == rid:
                return frame.get("errcode", 0) == 0, f"errcode={frame.get('errcode')} {frame.get('errmsg')}"
    return False, "认证无响应"


client: AibotWS | None = None


def start() -> None:
    global client
    status["enabled"] = settings.aibot_ws_enabled
    if not settings.aibot_ws_enabled:
        return
    client = AibotWS(settings.wecom_aibot_id, settings.wecom_aibot_secret, settings.wecom_aibot_ws_url)
    client.start()


def connected() -> bool:
    return bool(client and client.ws is not None and status["state"] == "已连接")


def push_markdown(content: str) -> dict:
    """把日报 / 提醒推送到所有开启推送的会话。返回 {sent, failed, errors}。"""
    from ..db import db
    chats = db.push_chats()
    result = {"sent": 0, "failed": 0, "errors": [], "targets": len(chats)}
    if not chats or not connected():
        return result
    for c in chats:
        try:
            client.send_markdown(c["chatid"], content)
            result["sent"] += 1
        except Exception as e:  # noqa: BLE001
            result["failed"] += 1
            result["errors"].append(str(e)[:120])
            log.warning("推送到 %s 失败：%s", c["chatid"][:12], e)
    return result


def stop() -> None:
    if client:
        client.stop()

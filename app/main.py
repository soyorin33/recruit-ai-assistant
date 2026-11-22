"""FastAPI 入口：Web 演示台 API + SSE 实时推送 + 企业微信回调 + 定时日报。"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from collections import deque
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analytics, auth, bus, manual, pipeline, report, sync
from .config import BASE_DIR, display_name, now, settings
from .db import db
from .domain import CHANNELS, FIELD_LABELS, POSITIONS, STAGES, STATUSES
from .seed import seed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

STATIC = BASE_DIR / "static"


# ======================= 生命周期 =======================
@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = await _startup()
    yield
    task.cancel()
    from .wecom import aibot_ws
    aibot_ws.stop()


def _apply_user_names() -> None:
    """配置了 WECOM_USER_NAMES 后，把历史记录里的企微账号 ID 换成中文名（并同步到腾讯文档）。"""
    for uid, name in settings.wecom_user_names.items():
        ids = [r["id"] for r in db.q("SELECT id FROM candidates WHERE hr_owner=?", (uid,))]
        if ids:
            db.exec("UPDATE candidates SET hr_owner=? WHERE hr_owner=?", (name, uid))
            for cid in ids:
                sync.enqueue(cid)
        db.exec("UPDATE messages SET operator=? WHERE operator=?", (name, uid))
        if ids:
            log.info("负责HR：%s → %s（%s 名候选人）", uid, name, len(ids))


async def _startup() -> asyncio.Task:
    if settings.seed_demo_data and not db.one("SELECT id FROM candidates LIMIT 1"):
        seed(db, pipeline.HELP_TEXT)
        log.info("已生成演示数据")
    if not db.one("SELECT id FROM messages LIMIT 1"):  # 正式环境首次启动也给出使用说明
        db.add_message("bot", pipeline.HELP_TEXT, operator="招聘助手", payload={"kind": "chat"})
    sync.start()
    _apply_user_names()  # 在同步队列启动后执行，改名才会同步到腾讯文档
    from .wecom import aibot_ws
    aibot_ws.start()  # 智能机器人长连接（配置了 WECOM_AIBOT_ID / SECRET 时）
    task = asyncio.create_task(_scheduler())
    log.info("LLM：%s｜企微应用：%s｜智能表格：%s｜腾讯文档：%s｜群机器人：%s｜群 @ 助手：%s",
             settings.llm_model if settings.llm_enabled else "未配置（规则引擎模式）",
             settings.wecom_app_enabled, settings.smartsheet_enabled, settings.tdocs_enabled,
             settings.webhook_enabled, "长连接" if settings.aibot_ws_enabled else
             ("URL 回调" if settings.aibot_enabled else "未配置"))
    return task


async def _scheduler() -> None:
    """每日定时：ALERT_TIME 推送跟进提醒，REPORT_TIME 推送 AI 日报。"""
    done: dict[str, str] = {}
    while True:
        n = now()
        hm, day = n.strftime("%H:%M"), n.strftime("%Y-%m-%d")
        try:
            if hm == settings.report_time and done.get("report") != day:
                done["report"] = day
                rep = await asyncio.to_thread(report.build_daily)
                pushed = await asyncio.to_thread(report.push, rep["markdown"])
                db.add_message("bot", rep["text"] + ("\n\n（已推送到企微群）" if pushed else ""),
                               operator="招聘助手", payload={"kind": "report", "report": rep})
                bus.publish("message", {})
            if hm == settings.alert_time and done.get("alert") != day:
                done["alert"] = day
                md = await asyncio.to_thread(report.build_alerts)
                if md:
                    await asyncio.to_thread(report.push, md)
        except Exception:
            log.exception("定时任务失败")
        await asyncio.sleep(20)


app = FastAPI(title="招聘助手 · AI 招聘数据自动化", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def access_control(request: Request, call_next):
    """设置了 ACCESS_TOKEN 时，除登录页、健康检查、企微回调和静态资源外，全部需要登录。"""
    path = request.url.path
    if settings.auth_enabled and not auth.is_public(path) and not auth.is_authed(request):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "未登录或登录已过期"}, status_code=401)
        return RedirectResponse("/login")
    return await call_next(request)


# ======================= 登录 =======================
class LoginIn(BaseModel):
    token: str


@app.get("/login")
def login_page() -> Response:
    if not settings.auth_enabled:
        return RedirectResponse("/")
    return FileResponse(STATIC / "login.html")


@app.post("/api/login")
def login(body: LoginIn, request: Request) -> Response:
    if not settings.auth_enabled:
        return JSONResponse({"ok": True})
    ip = auth.client_ip(request)
    if auth.too_many_fails(ip):
        return JSONResponse({"detail": "尝试次数过多，请 10 分钟后再试"}, status_code=429)
    if not auth.check_token(body.token.strip()):
        auth.record_fail(ip)
        return JSONResponse({"detail": "口令不正确"}, status_code=401)
    auth.reset_fails(ip)
    resp = JSONResponse({"ok": True})
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(auth.COOKIE, auth.make_session(), max_age=settings.session_hours * 3600,
                    httponly=True, samesite="lax", secure=https)
    return resp


@app.post("/api/logout")
def logout() -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


# ======================= Web 演示台 API =======================
class MessageIn(BaseModel):
    text: str
    operator: str = "HR小王"


class PendingIn(BaseModel):
    confirm: bool
    operator: str = "HR小王"


class ManualIn(BaseModel):
    """手动录入 / 编辑：fields 为字段名 → 值（空字符串表示清空）；force=True 表示已看过风险提示仍要保存。"""
    fields: dict
    operator: str = "HR小王"
    force: bool = False
    failure_id: int | None = None


class FailureIn(BaseModel):
    operator: str = "HR小王"
    note: str = ""


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz() -> dict:
    db.one("SELECT 1 AS ok")
    return {"ok": True}


def _aibot_ws_status() -> dict:
    from .wecom import aibot_ws
    return {k: aibot_ws.status[k] for k in ("enabled", "state", "connected_at", "last_error", "received")}


@app.get("/api/status")
def status() -> dict:
    return {
        "llm": {"enabled": settings.llm_enabled, "model": settings.llm_model if settings.llm_enabled else None,
                "base_url": settings.llm_base_url if settings.llm_enabled else None},
        "wecom_app": settings.wecom_app_enabled,
        "smartsheet": {**sync.status, "enabled": settings.smartsheet_enabled},
        "webhook": settings.webhook_enabled,
        "push": {"chats": len(db.push_chats()), "webhook": settings.webhook_enabled,
                 "report_time": settings.report_time, "alert_time": settings.alert_time, "last": report.last_push},
        "aibot": settings.aibot_enabled or settings.aibot_ws_enabled,
        "aibot_ws": _aibot_ws_status(),
        "tdocs": {**sync.statuses["tdocs"], "enabled": settings.tdocs_enabled, "url": settings.tdocs_url or None},
        "vision": {"enabled": settings.vision_enabled, "model": settings.vision_model or None},
        "confidence_threshold": settings.confidence_threshold,
        "stale_days": settings.stale_days,
        "failures_open": db.one("SELECT COUNT(*) n FROM failures WHERE state='open'")["n"],
        "auth_enabled": settings.auth_enabled,
        "demo_mode": settings.demo_mode,
        "report_time": settings.report_time,
        "now": now().strftime("%Y-%m-%d %H:%M"),
    }


@app.get("/api/dashboard")
def dashboard() -> dict:
    return analytics.dashboard()


@app.get("/api/candidates")
def candidates() -> dict:
    return {"items": analytics.candidates_table(), "labels": FIELD_LABELS}


@app.get("/api/jds")
def jds() -> dict:
    from .jd import load_jds
    return {"items": load_jds()}


@app.get("/api/candidates/{cid}")
def candidate(cid: int) -> dict:
    c = db.get_candidate(cid)
    if not c:
        raise HTTPException(404, "候选人不存在")
    return {"candidate": pipeline.public_candidate(c), "events": db.events_for(cid)}


# ======================= 人工兜底：手动录入 / 编辑、待人工处理 =======================
@app.get("/api/meta")
def meta() -> dict:
    return {"stages": STAGES, "statuses": STATUSES, "positions": POSITIONS, "channels": CHANNELS,
            "labels": FIELD_LABELS}


def _manual(call):
    try:
        return call()
    except manual.ManualError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/candidates")
def manual_create(body: ManualIn) -> dict:
    return _manual(lambda: manual.save(body.fields, operator=body.operator, force=body.force,
                                       failure_id=body.failure_id))


@app.patch("/api/candidates/{cid}")
def manual_update(cid: int, body: ManualIn) -> dict:
    return _manual(lambda: manual.save(body.fields, operator=body.operator, candidate_id=cid, force=body.force,
                                       failure_id=body.failure_id))


@app.get("/api/failures")
def failures() -> dict:
    return {"items": manual.list_open()}


@app.post("/api/failures/{fid}/retry")
def failure_retry(fid: int, body: FailureIn) -> dict:
    return _manual(lambda: manual.retry(fid, body.operator))


@app.post("/api/failures/{fid}/resolve")
def failure_resolve(fid: int, body: FailureIn) -> dict:
    manual.resolve(fid, body.operator, body.note)
    return {"ok": True}


@app.get("/api/messages")
def messages(limit: int = 60) -> dict:
    return {"items": db.recent_messages(limit)}


@app.post("/api/messages")
def post_message(body: MessageIn) -> dict:
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "消息不能为空")
    return pipeline.handle_message(text[:2000], operator=body.operator, source="web")


@app.post("/api/resume")
async def upload_resume(file: UploadFile = File(...), operator: str = Form("HR小王"),
                        channel: str | None = Form(None)) -> dict:
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件过大（上限 10MB）")
    from .vision import sniff_mime
    if sniff_mime(content):  # 图片 → 截图识别
        return await asyncio.to_thread(pipeline.handle_image, file.filename or "image.png", content, operator, "web")
    return await asyncio.to_thread(pipeline.handle_resume, file.filename or "resume.txt", content,
                                   operator, "web", channel)


@app.get("/api/pending")
def pending_list() -> dict:
    rows = db.q("SELECT id FROM pending WHERE state='open' ORDER BY id DESC")
    return {"items": [db.get_pending(r["id"]) for r in rows]}


@app.post("/api/pending/{pid}")
def resolve(pid: int, body: PendingIn) -> dict:
    return pipeline.resolve_pending(pid, body.confirm, body.operator)


@app.post("/api/report")
def make_report(push: bool = True) -> dict:
    rep = report.build_daily()
    pushed = report.push(rep["markdown"]) if push else False
    msg = db.add_message("bot", rep["text"] + ("\n\n（已推送到企微群）" if pushed else ""), operator="招聘助手",
                         payload={"kind": "report", "report": rep})
    bus.publish("message", {})
    return {"message": msg, "pushed": pushed}


@app.post("/api/alerts/push")
def push_alerts() -> dict:
    md = report.build_alerts()
    return {"markdown": md, "pushed": bool(md) and report.push(md)}


@app.post("/api/sync/full")
def full_sync() -> dict:
    if not settings.smartsheet_enabled:
        raise HTTPException(400, "未配置智能表格")
    return {"queued": sync.full_sync()}


@app.get("/api/export.csv")
def export_csv() -> Response:
    """一键导出（可直接导入腾讯文档 / Excel）。"""
    cols = ["id", "name", "phone", "position", "channel", "stage", "status", "interviewer", "interview_time",
            "feedback", "resume_summary", "hr_owner", "created_at", "updated_at"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([FIELD_LABELS.get(c, c) for c in cols])
    for r in analytics.candidates_table():
        w.writerow([r.get(c) or "" for c in cols])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=recruit.csv"})


@app.post("/api/reset")
def reset() -> dict:
    if not settings.demo_mode:
        raise HTTPException(403, "正式环境不允许重置数据（SEED_DEMO_DATA=0）")
    db.reset()
    seed(db, pipeline.HELP_TEXT)
    sync.after_reset()  # 腾讯文档：清空旧记录后重新全量写入
    bus.publish("reset", {})
    return {"ok": True}


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    """SSE：任何数据变化（来自网页或企业微信）都会实时推送到所有打开的看板。"""
    async def gen():
        last = bus.latest_id()
        idle = 0
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            evs = bus.since(last)
            if evs:
                last = evs[-1]["id"]
                progress = [e["data"] for e in evs if e["kind"] == "progress"]
                yield f"data: {json.dumps({'id': last, 'kinds': sorted({e['kind'] for e in evs}), 'progress': progress}, ensure_ascii=False)}\n\n"
                idle = 0
            else:
                idle += 1
                if idle % 30 == 0:  # 每 15 秒一次心跳：前端超过 40 秒没收到任何事件就自动重连
                    yield f"data: {json.dumps({'id': last, 'kinds': ['ping'], 'progress': []})}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ======================= 企业微信「智能机器人」（群里 @ 机器人） =======================
def _aibot():
    if not settings.aibot_enabled:
        raise HTTPException(503, "未配置企业微信智能机器人")
    from .wecom import aibot
    return aibot


@app.get("/wecom/aibot")
def aibot_verify(msg_signature: str, timestamp: str, nonce: str, echostr: str) -> PlainTextResponse:
    from .wecom.crypto import WeComCryptoError
    try:
        return PlainTextResponse(_aibot().crypt().verify_url(msg_signature, timestamp, nonce, echostr))
    except WeComCryptoError as e:
        raise HTTPException(403, str(e))


@app.post("/wecom/aibot")
async def aibot_receive(request: Request, msg_signature: str, timestamp: str, nonce: str) -> Response:
    """群里 @招聘助手 的消息。用流式消息回复：先返回「正在处理」，企微随后带 stream.id 回调拉取进度和结果。"""
    from .wecom.crypto import WeComCryptoError
    bot = _aibot()
    try:
        data = bot.crypt().decrypt_json(msg_signature, timestamp, nonce, json.loads(await request.body()))
    except (WeComCryptoError, ValueError) as e:
        log.warning("智能机器人回调解密失败：%s", e)
        raise HTTPException(403, "invalid message")
    reply = await asyncio.to_thread(bot.handle, data, nonce)
    return JSONResponse(reply) if reply else PlainTextResponse("")


# ======================= 企业微信回调 =======================
_seen_msg_ids: deque[str] = deque(maxlen=500)


def _crypt():
    from .wecom.crypto import WXBizMsgCrypt
    if not settings.wecom_app_enabled:
        raise HTTPException(503, "未配置企业微信应用")
    return WXBizMsgCrypt(settings.wecom_token, settings.wecom_aes_key, settings.wecom_corp_id)


@app.get("/wecom/callback")
def wecom_verify(msg_signature: str, timestamp: str, nonce: str, echostr: str) -> PlainTextResponse:
    """在企微后台配置「接收消息」URL 时的校验请求。"""
    from .wecom.crypto import WeComCryptoError
    try:
        return PlainTextResponse(_crypt().verify_url(msg_signature, timestamp, nonce, echostr))
    except WeComCryptoError as e:
        raise HTTPException(403, str(e))


def _wecom_text(user: str, content: str) -> None:
    from .wecom import api
    msg = pipeline.handle_message(content, operator=user, source="wecom")
    try:
        api.send_text(user, msg["text"])
    except Exception:
        log.exception("回复企微消息失败")


def _wecom_file(user: str, media_id: str) -> None:
    from .vision import sniff_mime
    from .wecom import api
    try:
        filename, content = api.download_media(media_id)
        handler = pipeline.handle_image if sniff_mime(content) else pipeline.handle_resume
        msg = handler(filename, content, operator=user, source="wecom")
        api.send_text(user, msg["text"])
    except Exception:
        log.exception("处理企微文件失败")


@app.post("/wecom/callback")
async def wecom_receive(request: Request, background: BackgroundTasks, msg_signature: str, timestamp: str,
                        nonce: str) -> PlainTextResponse:
    """接收 HR 发给「招聘助手」应用的消息。先立即返回（企微要求 5 秒内响应），再异步处理并主动回复。"""
    from .wecom.crypto import WeComCryptoError
    try:
        msg = _crypt().decrypt_xml(msg_signature, timestamp, nonce, await request.body())
    except (WeComCryptoError, Exception) as e:
        log.warning("企微回调解密失败：%s", e)
        raise HTTPException(403, "invalid message")
    mid = msg.get("MsgId") or f"{msg.get('FromUserName')}-{msg.get('CreateTime')}"
    if mid in _seen_msg_ids:  # 企微超时会重试推送，按 MsgId 幂等去重
        return PlainTextResponse("")
    _seen_msg_ids.append(mid)
    user, mtype = display_name(msg.get("FromUserName", "")), msg.get("MsgType")
    if mtype == "text" and msg.get("Content", "").strip():
        background.add_task(_wecom_text, user, msg["Content"].strip())
    elif mtype in ("file", "image") and msg.get("MediaId"):
        background.add_task(_wecom_file, user, msg["MediaId"])
    elif mtype == "voice" and msg.get("Recognition"):  # 开启语音识别后可直接拿到文字
        background.add_task(_wecom_text, user, msg["Recognition"])
    return PlainTextResponse("")

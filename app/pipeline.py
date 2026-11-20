"""核心处理流水线：消息 → AI 抽取 → 实体对齐（去重） → 状态机校验 → 自动写入 / 人工确认 → 同步 & 推送。"""
from __future__ import annotations

import io
import logging
from datetime import datetime

from . import bus
from .config import settings
from .db import db
from .domain import (
    FIELD_LABELS, STAGE_INDEX, STATUS_ACTIVE, STATUS_DROPPED, STATUS_HIRED, STATUS_REJECTED, mask_phone,
)
from .extractor import extract_message, extract_resume

log = logging.getLogger("pipeline")
CLOSED = {STATUS_REJECTED, STATUS_DROPPED, STATUS_HIRED}
WEEK = "一二三四五六日"

HELP_TEXT = (
    "我是招聘助手，直接用大白话告诉我候选人进展即可，例如：\n"
    "· 张三 Java后端 BOSS来的，今天一面过了，周五下午二面，面试官李四\n"
    "· 王芳二面挂了，算法基础薄弱；赵磊接了offer\n"
    "· 本周各岗位进了几个复试？\n"
    "· 发一下今天的日报\n"
    "也可以直接把简历（PDF/TXT）发给我，自动建档。"
)


# ---------------- 工具 ----------------
def fmt_time(t: str | None) -> str:
    if not t:
        return ""
    try:
        d = datetime.strptime(t, "%Y-%m-%d %H:%M")
        return f"{d:%m-%d} 周{WEEK[d.weekday()]} {d:%H:%M}"
    except ValueError:
        return t


def public_candidate(c: dict) -> dict:
    """对外展示时手机号脱敏。"""
    return {**c, "phone": mask_phone(c.get("phone"))}


def known_candidates() -> list[dict]:
    return db.q("SELECT name, position, stage, status FROM candidates ORDER BY updated_at DESC LIMIT 80")


# ---------------- 实体对齐 + 变更计划 ----------------
def plan_change(c: dict) -> dict:
    reasons: list[str] = []
    existing = None
    if c.get("phone"):
        hits = db.find_by_phone(c["phone"])
        existing = hits[0] if hits else None
    if existing is None:
        hits = db.find_by_name(c["name"])
        if c.get("position") and len(hits) >= 1:
            same_pos = [h for h in hits if h.get("position") in (c["position"], None, "")]
            if not same_pos:
                reasons.append(
                    f"已有同名候选人「{hits[0]['name']}·{hits[0].get('position')}」，岗位不同，将新建为另一位候选人")
            hits = same_pos
        if len(hits) > 1:
            reasons.append(f"存在 {len(hits)} 位同名候选人，默认匹配最近更新的一位（#{hits[0]['id']}）")
        existing = hits[0] if hits else None

    if existing is None:
        fields = {"stage": "简历筛选", "status": STATUS_ACTIVE, **c}
        if fields.get("stage") == "已入职":
            fields["status"] = STATUS_HIRED
        return {"op": "create", "name": c["name"], "fields": fields,
                "changes": {k: [None, v] for k, v in fields.items()}, "reasons": reasons}

    new = dict(c)
    old_stage, old_status = existing["stage"], existing["status"]
    if new.get("stage") == "已入职" or new.get("status") == STATUS_HIRED:
        new["stage"], new["status"] = "已入职", STATUS_HIRED
    if "stage" in new and new["stage"] != old_stage and "status" not in new:
        new["status"] = STATUS_ACTIVE
    if "stage" in new and new["stage"] != old_stage and "interview_time" not in new:
        new["interview_time"] = None  # 进入新阶段，旧的面试时间作废
    if new.get("feedback") and existing.get("feedback") and new["feedback"] not in existing["feedback"]:
        new["feedback"] = (existing["feedback"] + "；" + new["feedback"])[-150:]

    # ---- 状态机校验 ----
    if "stage" in new and new["stage"] != old_stage:
        delta = STAGE_INDEX[new["stage"]] - STAGE_INDEX[old_stage]
        if delta < 0:
            reasons.append(f"阶段回退：{old_stage} → {new['stage']}")
        elif delta >= 2 and new["stage"] != "已入职":
            reasons.append(f"跳过了中间阶段：{old_stage} → {new['stage']}")
    if old_status in CLOSED and new.get("status", old_status) == STATUS_ACTIVE and (
            "stage" in c or "status" in c):
        reasons.append(f"该候选人已是「{old_status}」，确认要重新激活吗？")

    changes = {k: [existing.get(k), v] for k, v in new.items()
               if k != "name" and (existing.get(k) or None) != (v or None)}
    return {"op": "update" if changes else "noop", "name": existing["name"], "candidate_id": existing["id"],
            "fields": {k: v[1] for k, v in changes.items()}, "changes": changes, "reasons": reasons,
            "before": {"stage": old_stage, "status": old_status, "position": existing.get("position")}}


def apply_plan(plan: dict, *, operator: str, raw_text: str, engine: str, source: str) -> dict:
    if plan["op"] == "create":
        cid = db.create_candidate({**plan["fields"], "hr_owner": operator})
        db.add_event(cid, "create", plan["changes"], operator=operator, raw_text=raw_text, engine=engine, source=source)
    elif plan["op"] == "update":
        cid = plan["candidate_id"]
        db.update_candidate(cid, plan["fields"])
        db.add_event(cid, "update", plan["changes"], operator=operator, raw_text=raw_text, engine=engine, source=source)
    else:
        return db.get_candidate(plan["candidate_id"])
    cand = db.get_candidate(cid)
    from . import sync  # 延迟导入，避免循环依赖
    sync.enqueue(cid)
    bus.publish("candidate", {"id": cid, "op": plan["op"]})
    return cand


def describe_plan(plan: dict, cand: dict | None = None) -> str:
    f = plan["fields"]
    if plan["op"] == "create":
        bits = [f.get("position") or "岗位待补充", f.get("channel"), f.get("stage")]
        if f.get("status") not in (None, STATUS_ACTIVE):
            bits.append(f["status"])
        line = f"{plan['name']}｜" + "｜".join(b for b in bits if b)
        if f.get("interview_time"):
            line += f"（{fmt_time(f['interview_time'])}" + (f"，面试官{f['interviewer']}" if f.get("interviewer") else "") + "）"
        return line
    if plan["op"] == "noop":
        return f"{plan['name']} 信息无变化"
    parts = []
    for k, (old, new) in plan["changes"].items():
        if k == "interview_time":
            new, old = fmt_time(new), fmt_time(old)
        if k == "phone":
            new, old = mask_phone(new), mask_phone(old)
        if k == "feedback":
            parts.append(f"评价：{str(new)[-40:]}")
        elif new in (None, ""):
            continue
        else:
            parts.append(f"{FIELD_LABELS.get(k, k)}：{old + ' → ' if old else ''}{new}")
    return f"{plan['name']}　" + "；".join(parts)


# ---------------- 消息入口 ----------------
def handle_message(text: str, operator: str = "HR", source: str = "web") -> dict:
    """处理一条 HR 消息，返回机器人回复消息（已入库并推送）。"""
    db.add_message("user", text, operator=operator, source=source)
    bus.publish("message", {})
    try:
        payload = _dispatch(text, operator, source)
    except Exception as e:  # 兜底：任何异常都要给 HR 明确反馈，而不是沉默
        log.exception("处理消息失败")
        payload = {"kind": "error", "text": f"处理失败：{e}，请稍后重试或换个说法。"}
    msg = db.add_message("bot", payload.pop("text"), operator="招聘助手", source=source, payload=payload)
    bus.publish("message", {})
    return msg


def _dispatch(text: str, operator: str, source: str) -> dict:
    ex = extract_message(text, known_candidates())
    meta = {k: ex.get(k) for k in ("intent", "candidates", "confidence", "engine", "llm_meta", "fallback_reason")}
    intent = ex["intent"]

    if intent in ("confirm", "cancel"):
        pending = db.latest_open_pending(operator)
        if not pending:
            return {"kind": "chat", "text": "当前没有待确认的记录。", "extraction": meta}
        return resolve_pending(pending["id"], intent == "confirm", operator, return_payload=True)

    if intent == "query":
        from . import nlq
        ans = nlq.answer(ex.get("question") or text)
        return {"kind": "query", "text": ans["text"], "table": ans.get("table"), "chart": ans.get("chart"),
                "extraction": meta, "query": ans.get("query")}

    if intent == "report":
        from . import report
        rep = report.build_daily()
        pushed = report.push(rep["markdown"]) if settings.webhook_enabled else False
        tail = "\n\n（已同步推送到企微群）" if pushed else ""
        return {"kind": "report", "text": rep["text"] + tail, "report": rep, "extraction": meta}

    if intent != "update" or not ex["candidates"]:
        return {"kind": "chat", "text": HELP_TEXT, "extraction": meta}

    return process_candidates(ex["candidates"], ex["confidence"], ex["engine"], text, operator, source, meta)


def process_candidates(cands: list[dict], confidence: float, engine: str, raw_text: str,
                       operator: str, source: str, meta: dict) -> dict:
    actions, lines = [], []
    low_conf = confidence < settings.confidence_threshold
    to_confirm = []
    for c in cands:
        plan = plan_change(c)
        if plan["op"] != "noop" and (low_conf or plan["reasons"]):
            reasons = plan["reasons"] + ([f"AI 置信度 {confidence:.2f} 低于阈值 {settings.confidence_threshold}"]
                                         if low_conf else [])
            to_confirm.append({**plan, "reasons": reasons})
            continue
        cand = apply_plan(plan, operator=operator, raw_text=raw_text, engine=engine, source=source)
        icon = {"create": "🆕 已建档", "update": "✅ 已更新", "noop": "ℹ️"}[plan["op"]]
        lines.append(f"{icon}　{describe_plan(plan)}")
        actions.append({"type": plan["op"], "candidate": public_candidate(cand), "changes": plan["changes"]})

    pending_id = None
    applied_text = "\n".join(lines)
    if to_confirm:
        reasons = sorted({r for p in to_confirm for r in p["reasons"]})
        pending_id = db.add_pending(operator, source, raw_text,
                                    {"plans": to_confirm, "engine": engine}, reasons)
        for p in to_confirm:
            lines.append(f"⚠️ 待确认　{describe_plan(p)}")
        lines.append("原因：" + "；".join(reasons))
        lines.append("回复「确认」写入，回复「取消」放弃。")
        actions.append({"type": "pending", "pending_id": pending_id, "plans": to_confirm, "reasons": reasons})
        bus.publish("pending", {"id": pending_id})
    # web_text：网页端已用卡片展示待确认内容，气泡里不再重复
    web_text = applied_text + ("\n" if applied_text else "") + "以下变更存在风险，请确认：" if to_confirm else None
    return {"kind": "update", "text": "\n".join(lines), "web_text": web_text, "actions": actions,
            "pending_id": pending_id, "extraction": meta}


def resolve_pending(pid: int, confirm: bool, operator: str, return_payload: bool = False) -> dict:
    p = db.get_pending(pid)
    if not p or p["state"] != "open":
        payload = {"kind": "chat", "text": "该记录已处理过了。"}
        return payload if return_payload else _post_bot(payload)
    if not confirm:
        db.close_pending(pid, "cancelled")
        payload = {"kind": "chat", "text": "已取消，本条不会写入。", "resolved": pid}
    else:
        lines, actions = [], []
        for plan in p["proposal"]["plans"]:
            if plan["op"] == "update":  # 确认时重新计算差异，防止期间数据已被修改
                fresh = plan_change({"name": plan["name"], **plan["fields"]})
                if fresh["op"] == "update" and fresh.get("candidate_id") == plan["candidate_id"]:
                    plan = fresh
            cand = apply_plan(plan, operator=operator, raw_text=p["raw_text"],
                              engine=p["proposal"].get("engine", ""), source=p["source"])
            lines.append(f"✅ 已确认写入　{describe_plan(plan)}")
            actions.append({"type": plan["op"], "candidate": public_candidate(cand), "changes": plan["changes"]})
        db.close_pending(pid, "confirmed")
        payload = {"kind": "update", "text": "\n".join(lines), "actions": actions, "resolved": pid}
    bus.publish("pending", {"id": pid})
    return payload if return_payload else _post_bot(payload)


def _post_bot(payload: dict) -> dict:
    msg = db.add_message("bot", payload.pop("text"), operator="招聘助手", source="web", payload=payload)
    bus.publish("message", {})
    return msg


# ---------------- 简历入口 ----------------
def read_resume_text(filename: str, content: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    for enc in ("utf-8", "gbk"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")


def handle_resume(filename: str, content: bytes, operator: str = "HR", source: str = "web",
                  channel: str | None = None) -> dict:
    db.add_message("user", f"📎 {filename}", operator=operator, source=source, payload={"file": filename})
    bus.publish("message", {})
    try:
        text = read_resume_text(filename, content)
        if len(text.strip()) < 20:
            payload = {"kind": "error", "text": f"没能从「{filename}」中读到文字，可能是扫描件/图片，请发送文字版简历。"}
        else:
            r = extract_resume(text)
            meta = {"intent": "resume", "engine": r.get("engine"), "confidence": r.get("confidence"),
                    "llm_meta": r.get("llm_meta"), "fallback_reason": r.get("fallback_reason"),
                    "candidates": [{k: r.get(k) for k in ("name", "phone", "email", "position", "education",
                                                          "years", "skills", "summary")}]}
            if not r.get("name"):
                payload = {"kind": "error", "text": "简历中未识别到姓名，请补充后重试。", "extraction": meta}
            else:
                summary = r.get("summary") or ""
                if r.get("education") and r["education"] not in summary:
                    summary = f"{r['education']}，{summary}"
                cand = {k: v for k, v in {
                    "name": r["name"], "phone": r.get("phone"), "email": r.get("email"),
                    "position": r.get("position"), "channel": channel, "stage": "简历筛选",
                    "status": STATUS_ACTIVE, "resume_summary": summary[:120] or None,
                }.items() if v}
                conf = float(r.get("confidence") or 0.8)
                payload = process_candidates([cand], conf, r.get("engine", ""), f"[简历] {filename}",
                                             operator, source, meta)
                payload["text"] = f"📄 简历解析完成：{summary}\n" + payload["text"]
                if payload.get("web_text"):
                    payload["web_text"] = f"📄 简历解析完成：{summary}\n" + payload["web_text"]
                payload["kind"] = "update"
    except Exception as e:
        log.exception("简历处理失败")
        payload = {"kind": "error", "text": f"简历处理失败：{e}"}
    msg = db.add_message("bot", payload.pop("text"), operator="招聘助手", source=source, payload=payload)
    bus.publish("message", {})
    return msg

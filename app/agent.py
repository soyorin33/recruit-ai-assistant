"""招聘助手智能体：大模型 Function Calling + 多轮记忆 + 写入安全护栏。

与「单次抽取」相比，智能体解决了真实群聊里的三类问题：
1. 指代与追问：「把他改到周五」「那她呢？」——依赖多轮上下文和「最近提及的候选人」。
2. 信息不全时会反问，而不是硬猜后写错数据。
3. 一句话里混合多个动作（先查再改、改完顺便问统计），由模型自主编排工具调用。

安全设计：
- 模型不能直接改数据库，只能调用 record_candidates，而它内部仍走「去重 → 状态机 → 置信度 → 人工确认」流水线；
- 统计数字只来自 query_data 的代码执行结果；
- 单轮最多 MAX_STEPS 次工具调用，防止循环；工具异常以错误信息返回给模型，由模型向 HR 解释。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from . import llm
from .config import now, settings
from .db import db
from .domain import CHANNELS, POSITIONS, STAGES, STATUSES, mask_phone
from .extractor import WEEKDAYS, _clean_candidate, field_rules

log = logging.getLogger("agent")
MAX_STEPS = 6
HISTORY_MESSAGES = 12

SYSTEM_PROMPT = """你是企业微信里的「招聘助手」智能体，服务于 HR 和面试官。当前时间：{now}（星期{weekday}）。

## 你的工作方式
- 招聘数据只能通过工具读写，严禁凭记忆编造候选人信息或统计数字。
- HR 用口语描述候选人进展时，调用 record_candidates 记录；一条消息可能涉及多人。
- 遇到「他 / 她 / 这个人 / 刚才那个」等指代，优先对应「最近提及的候选人」列表；仍无法确定是谁时，先调用 search_candidates，或直接向 HR 反问，不要猜。
- 更新已有候选人时务必填写 candidate_id（来自最近提及列表或 search_candidates 的结果），只填写本次发生变化的字段。
- 统计、名单、漏斗、分布类问题调用 query_data；问某个人的情况调用 get_candidate；要日报 / 汇总调用 daily_report。
- 信息明显缺失（如新候选人没有姓名）或存在歧义（同名多人）时，用一句话反问 HR，不要调用写入工具。
- 工具返回「待确认」时，告诉 HR 需要回复「确认」或点击卡片确认。
- 【重要】修改数据只能通过调用 record_candidates 完成。本轮没有成功调用它，就绝不能说「已改 / 已记录 / 已更新」。
- 【重要】不要自己用文字向 HR 索要「确认」：即使变更有风险（如阶段回退），也直接调用 record_candidates，系统会自动判断是否需要 HR 确认并生成确认卡片。
- 对话历史只用于理解上下文，其中描述的数据可能已过时，以工具查询结果为准。

{field_rules}

## 回复要求
- 中文，简洁（一般不超过 100 字），像同事在群里回复；说清楚做了什么、还需要 HR 做什么。
- 不要输出 JSON、不要复述工具原始返回、不要使用 Markdown 标题。

## 最近提及的候选人（按时间倒序，用于理解指代）
{focus}"""

_cand_props = {
    "candidate_id": {"type": "integer", "description": "已有候选人编号（来自最近提及列表或 search_candidates）；新候选人不填"},
    "name": {"type": "string", "description": "候选人姓名"},
    "phone": {"type": "string"}, "email": {"type": "string"},
    "position": {"type": "string", "description": "应聘岗位，尽量归一到：" + "、".join(POSITIONS)},
    "channel": {"type": "string", "description": "招聘渠道，尽量归一到：" + "、".join(CHANNELS)},
    "stage": {"type": "string", "enum": STAGES, "description": "当前所处 / 即将进行的阶段"},
    "status": {"type": "string", "enum": STATUSES},
    "interviewer": {"type": "string"},
    "interview_time": {"type": "string", "description": "下一次面试时间，格式 YYYY-MM-DD HH:MM"},
    "feedback": {"type": "string", "description": "面试评价 / 备注，≤40 字"},
}

TOOLS = [
    {"type": "function", "function": {
        "name": "record_candidates",
        "description": "新建或更新候选人信息。内部会自动去重、做阶段合法性校验；低置信度或高风险变更会转为待 HR 确认。",
        "parameters": {"type": "object", "properties": {
            "updates": {"type": "array", "items": {"type": "object", "properties": _cand_props, "required": ["name"]}},
            "confidence": {"type": "number", "description": "你对本次理解准确性的把握，0-1；有歧义时给低分"},
        }, "required": ["updates", "confidence"]}}},
    {"type": "function", "function": {
        "name": "search_candidates",
        "description": "按姓名关键字 / 岗位 / 阶段 / 状态查找候选人，用于确认是谁、拿到 candidate_id。",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "姓名或面试官关键字"},
            "position": {"type": "string"}, "stage": {"type": "string", "enum": STAGES},
            "status": {"type": "string", "enum": STATUSES}, "limit": {"type": "integer", "default": 8},
        }}}},
    {"type": "function", "function": {
        "name": "get_candidate",
        "description": "查看单个候选人的完整信息和最近的变更记录。",
        "parameters": {"type": "object", "properties": {
            "candidate_id": {"type": "integer"}, "name": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "query_data",
        "description": "执行招聘数据统计查询（数字由系统计算）。",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["count", "list", "group", "funnel"],
                     "description": "count 计数 / list 名单 / group 分组分布 / funnel 漏斗"},
            "filters": {"type": "object", "properties": {
                "position": {"type": "string"}, "channel": {"type": "string"},
                "stage": {"type": "string", "enum": STAGES}, "status": {"type": "string", "enum": STATUSES},
                "interviewer": {"type": "string"},
                "period": {"type": "string", "enum": ["today", "yesterday", "this_week", "last_week",
                                                      "this_month", "last_7_days", "all"],
                           "description": "时间范围；与 stage 同时出现时表示该时间段内「进入」该阶段"},
            }},
            "group_by": {"type": "string", "enum": ["position", "channel", "stage", "status", "interviewer", "hr_owner"]},
        }, "required": ["type"]}}},
    {"type": "function", "function": {
        "name": "daily_report",
        "description": "生成今日招聘日报（含漏斗、待面试、超时预警、AI 洞察），已配置群机器人时自动推送到企微群。",
        "parameters": {"type": "object", "properties": {}}}},
]


@dataclass
class Ctx:
    operator: str
    source: str
    raw_text: str
    steps: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    focus: list = field(default_factory=list)
    table: dict | None = None
    chart: dict | None = None
    report: dict | None = None
    pending_id: int | None = None
    lines: list = field(default_factory=list)


def _brief(c: dict) -> dict:
    return {"candidate_id": c["id"], "name": c["name"], "position": c.get("position"), "stage": c["stage"],
            "status": c["status"], "interviewer": c.get("interviewer"), "interview_time": c.get("interview_time"),
            "updated_at": c["updated_at"][:16]}


# ---------------- 工具实现 ----------------
def t_record(ctx: Ctx, args: dict) -> dict:
    from .pipeline import process_candidates
    cands = []
    for u in args.get("updates") or []:
        cid = u.get("candidate_id")
        c = _clean_candidate(u)
        if not c:
            continue
        if cid:
            c["candidate_id"] = int(cid)
        cands.append(c)
    if not cands:
        return {"error": "没有可记录的候选人（缺少姓名）"}
    try:
        conf = max(0.0, min(1.0, float(args.get("confidence", 0.8))))
    except (TypeError, ValueError):
        conf = 0.6
    payload = process_candidates(cands, conf, "agent", ctx.raw_text, ctx.operator, ctx.source,
                                 {"engine": "agent"})
    ctx.actions += payload.get("actions", [])
    ctx.lines.append(payload["text"])
    if payload.get("pending_id"):
        ctx.pending_id = payload["pending_id"]
    results = []
    for a in payload.get("actions", []):
        if a["type"] == "pending":
            for p in a["plans"]:
                results.append({"name": p["name"], "result": "待HR确认", "reasons": a["reasons"]})
                if p.get("candidate_id"):
                    ctx.focus.append(p["candidate_id"])
        else:
            ctx.focus.append(a["candidate"]["id"])
            results.append({"name": a["candidate"]["name"], "candidate_id": a["candidate"]["id"],
                            "result": {"create": "已新建", "update": "已更新", "noop": "无变化"}[a["type"]],
                            "changes": {k: v[1] for k, v in a["changes"].items() if k != "name"}})
    return {"results": results}


def t_search(ctx: Ctx, args: dict) -> dict:
    rows = db.all_candidates()
    kw = (args.get("keyword") or "").strip()
    if kw:
        rows = [r for r in rows if kw in r["name"] or kw in (r.get("interviewer") or "")]
    for k in ("position", "stage", "status"):
        if args.get(k):
            rows = [r for r in rows if (r.get(k) or "") and (args[k] in r[k] or r[k] in args[k])]
    limit = max(1, min(int(args.get("limit") or 8), 20))
    return {"total": len(rows), "items": [_brief(r) for r in rows[:limit]]}


def t_get(ctx: Ctx, args: dict) -> dict:
    c = None
    if args.get("candidate_id"):
        c = db.get_candidate(int(args["candidate_id"]))
    elif args.get("name"):
        hits = db.find_by_name(args["name"].strip())
        if len(hits) > 1:
            return {"ambiguous": True, "items": [_brief(h) for h in hits]}
        c = hits[0] if hits else None
    if not c:
        return {"error": "没有找到该候选人"}
    ctx.focus.append(c["id"])
    events = db.events_for(c["id"])[:5]
    return {"candidate": {**{k: v for k, v in c.items() if v not in (None, "")}, "phone": mask_phone(c.get("phone"))},
            "recent_events": [{"ts": e["ts"][:16], "operator": e["operator"], "raw": e["raw_text"],
                               "changes": {k: v[1] for k, v in e["changes"].items()}} for e in events]}


def t_query(ctx: Ctx, args: dict) -> dict:
    from .nlq import execute
    plan = {"type": args.get("type", "count"), "filters": args.get("filters") or {}, "group_by": args.get("group_by")}
    if plan["type"] == "group" and not plan["group_by"]:
        plan["group_by"] = "position"
    res = execute(plan)
    ctx.table, ctx.chart = res.get("table"), res.get("chart")
    out = {k: v for k, v in res.items() if k not in ("chart", "table")}
    out["rows"] = res["table"]["rows"][:20]
    out["columns"] = res["table"]["columns"]
    return out


def t_report(ctx: Ctx, args: dict) -> dict:
    from . import report
    rep = report.build_daily()
    pushed = report.push(rep["markdown"]) if settings.webhook_enabled else False
    ctx.report = rep
    ctx.lines.append(rep["text"] + ("\n\n（已同步推送到企微群）" if pushed else ""))
    return {"generated": True, "pushed_to_group": pushed, "insights": rep["insights"], "today": rep["today"]}


TOOL_IMPL = {"record_candidates": t_record, "search_candidates": t_search, "get_candidate": t_get,
             "query_data": t_query, "daily_report": t_report}


def _summ(name: str, result: dict) -> str:
    """推理过程里给人看的一行摘要。"""
    if "error" in result:
        return "❗" + result["error"]
    if name == "record_candidates":
        return "；".join(f"{r['name']} {r['result']}" for r in result["results"])
    if name == "search_candidates":
        return f"找到 {result['total']} 人：" + "、".join(i["name"] for i in result["items"][:5])
    if name == "get_candidate":
        if result.get("ambiguous"):
            return f"同名 {len(result['items'])} 人，需要确认"
        c = result["candidate"]
        return f"{c['name']}｜{c.get('position', '')}｜{c['stage']}·{c['status']}"
    if name == "query_data":
        return f"命中 {result['count']} 人" + (f"（{'，'.join(result['conditions'])}）" if result.get("conditions") else "")
    if name == "daily_report":
        return "日报已生成" + ("并推送到群" if result.get("pushed_to_group") else "")
    return "完成"


# ---------------- 主循环 ----------------
def focus_text(focus_ids: list[int]) -> str:
    rows = [db.get_candidate(i) for i in focus_ids[:6]]
    rows = [r for r in rows if r]
    if not rows:
        return "（暂无）"
    return "\n".join(f"- candidate_id={r['id']}：{r['name']}（{r.get('position') or '岗位未知'}，{r['stage']}，{r['status']}"
                     + (f"，面试 {r['interview_time']}" if r.get("interview_time") else "") + "）" for r in rows)


# 回复中出现这些「已完成写入」的说法，但本轮没有成功调用 record_candidates → 判定为模型谎报
# 只匹配「做了修改」的动作词；「已淘汰 / 已入职」是描述状态，不算
WRITE_CLAIM_RE = re.compile(r"已(经)?(帮你)?(改|修改|更新|记录|登记|建档|新建|调整|改为|改成|换成|换为|标记|设为|设置|录入|写入|同步)")
GUARD_NOTE = ("【系统校验】你在回复中表示已经修改了数据，但本轮并没有调用 record_candidates，数据库没有任何变化。"
              "请现在调用 record_candidates 完成修改；如果确实无法修改，请如实告诉 HR，不要声称已完成。")

PROGRESS_TEXT = {"record_candidates": "📝 正在记录候选人…", "search_candidates": "🔍 正在查找候选人…",
                 "get_candidate": "📇 正在查看候选人详情…", "query_data": "📊 正在统计数据…",
                 "daily_report": "📰 正在生成日报…"}


def run(text: str, operator: str, source: str, history: list[dict], focus_ids: list[int],
        progress=None) -> dict:
    """执行一轮智能体对话，返回机器人回复 payload（含 text）。大模型不可用时抛出 LLMError 由上层降级。

    progress(text)：每一步开始前回调，用于网页「正在…」提示和企微流式消息的实时刷新。
    """
    notify = progress or (lambda _t: None)
    notify("🤔 正在理解消息…")
    n = now()
    system = SYSTEM_PROMPT.format(now=n.strftime("%Y-%m-%d %H:%M"), weekday=WEEKDAYS[n.weekday()],
                                  field_rules=field_rules(), focus=focus_text(focus_ids))
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": text}]
    ctx = Ctx(operator=operator, source=source, raw_text=text)
    t0, tokens, final, guarded = time.time(), 0, "", False
    for _ in range(MAX_STEPS):
        if ctx.steps:
            notify("✍️ 正在组织回复…")
        msg, meta = llm.chat_tools(messages, TOOLS)
        tokens += (meta.get("prompt_tokens") or 0) + (meta.get("completion_tokens") or 0)
        calls = msg.get("tool_calls") or []
        if not calls:
            final = msg.get("content", "").strip()
            # 本轮一个工具都没调用、却声称做了修改 → 必然是谎报（查询类回答至少会调用一次查询工具）
            if not ctx.steps and not guarded and WRITE_CLAIM_RE.search(final):
                # 代码层校验：模型声称改了数据却没有调用写入工具 → 打回重来一次（不依赖模型自觉）
                guarded = True
                ctx.steps.append({"tool": "guard", "args": {}, "summary": "⚠️ 拦截：模型声称已修改数据但未调用写入工具，已要求重做"})
                messages += [{"role": "assistant", "content": final}, {"role": "user", "content": GUARD_NOTE}]
                final = ""
                continue
            break
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            name = call.get("function", {}).get("name", "")
            try:
                args = json.loads(call.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            impl = TOOL_IMPL.get(name)
            notify(PROGRESS_TEXT.get(name, f"正在执行 {name}…"))
            try:
                result = impl(ctx, args) if impl else {"error": f"未知工具 {name}"}
            except Exception as e:  # 工具异常反馈给模型，由模型向 HR 解释
                log.exception("工具 %s 执行失败", name)
                result = {"error": f"工具执行失败：{e}"}
            ctx.steps.append({"tool": name, "args": args, "summary": _summ(name, result)})
            messages.append({"role": "tool", "tool_call_id": call.get("id", name),
                             "content": json.dumps(result, ensure_ascii=False, default=str)[:6000]})
    else:
        final = "这件事步骤有点多，我没能一次处理完，麻烦拆成几句再发给我。"

    if ctx.report:  # 日报完整展示，模型的补充说明放在后面
        text_out = ctx.lines[-1] + (f"\n\n{final}" if final and len(final) < 80 else "")
    else:
        text_out = final or ("\n".join(ctx.lines) if ctx.lines else "收到。")
    if ctx.pending_id and "确认" not in text_out:
        text_out += "\n（有变更待确认：回复「确认」写入，回复「取消」放弃）"
    kind = "report" if ctx.report else ("update" if ctx.actions else ("query" if ctx.table else "chat"))
    agent_meta = {"model": settings.llm_model, "steps": ctx.steps, "latency_ms": int((time.time() - t0) * 1000),
                  "tokens": tokens or None, "focus_in": focus_ids[:6]}
    return {"kind": kind, "text": text_out, "actions": ctx.actions, "pending_id": ctx.pending_id,
            "table": ctx.table, "chart": ctx.chart, "report": ctx.report, "agent": agent_meta,
            "focus": list(dict.fromkeys(reversed(ctx.focus)))}

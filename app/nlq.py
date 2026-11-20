"""自然语言问数：问题 →（LLM）查询计划 JSON → 代码执行查询 →（LLM）组织回答。

关键设计：大模型只做「理解问题」和「表达结果」，数字全部由代码计算，杜绝模型编造统计数据。
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter

from . import llm
from .analytics import funnel, period_range
from .config import now, settings
from .db import db, ts
from .domain import (
    CHANNEL_PATTERNS, CHANNELS, POSITION_PATTERNS, POSITIONS, ROUND_TO_STAGE, STAGES, STATUSES, mask_phone,
    match_dict,
)

log = logging.getLogger("nlq")
PERIODS = ["today", "yesterday", "this_week", "last_week", "this_month", "last_7_days", "all"]
PERIOD_LABEL = {"today": "今天", "yesterday": "昨天", "this_week": "本周", "last_week": "上周",
                "this_month": "本月", "last_7_days": "近 7 天", "all": "累计"}
GROUP_LABEL = {"position": "岗位", "channel": "渠道", "stage": "阶段", "status": "状态",
               "interviewer": "面试官", "hr_owner": "负责HR"}

PLAN_PROMPT = """你把 HR 的招聘数据问题翻译成查询计划 JSON。当前时间 {now}。
可用取值：position∈{positions}；channel∈{channels}；stage∈{stages}；status∈{statuses}；period∈{periods}
输出格式：
{{"type": "count|list|group|funnel|detail",
  "filters": {{"position": null, "channel": null, "stage": null, "status": null, "interviewer": null, "name": null, "period": "all"}},
  "group_by": null 或 "position|channel|stage|status|interviewer|hr_owner"}}
规则：
- 一面=初试，二面/三面/终面=复试
- 带时间范围 + 阶段（如「本周进了几个二面」）表示该时间段内「进入」该阶段的人数
- 问「哪些人 / 名单 / 有谁」用 list；问「各岗位 / 各渠道 / 分布」用 group；问「漏斗 / 转化率」用 funnel；问某个具体候选人用 detail 并填 name
- 没提到的过滤条件填 null，period 默认 all
只输出 JSON。"""

ANSWER_PROMPT = """你是招聘数据分析助手。根据「查询结果」用中文简洁回答 HR 的问题（≤100字）。
必须只使用查询结果中的数字，不得编造；可补充一句有价值的观察或建议。不要使用 Markdown 标题。"""


# ---------------- 计划生成 ----------------
def rules_plan(q: str) -> dict:
    low = q.lower()
    f: dict = {"period": "all"}
    if pos := match_dict(low, POSITION_PATTERNS):
        f["position"] = pos
    if ch := match_dict(low, CHANNEL_PATTERNS):
        f["channel"] = ch
    for k, v in ROUND_TO_STAGE.items():
        if k in low:
            f["stage"] = v
    if "offer" in low:
        f["stage"] = "Offer"
    elif "入职" in low:
        f["stage"] = "已入职"
    if re.search(r"淘汰|挂", q):
        f["status"] = "已淘汰"
    elif "放弃" in q:
        f["status"] = "已放弃"
    elif re.search(r"在途|进行中|跟进中", q):
        f["status"] = "进行中"
    for word, p in (("今天", "today"), ("今日", "today"), ("昨天", "yesterday"), ("本周", "this_week"),
                    ("这周", "this_week"), ("上周", "last_week"), ("本月", "this_month"), ("这个月", "this_month"),
                    ("近7天", "last_7_days"), ("最近一周", "last_7_days")):
        if word in q:
            f["period"] = p
    if m := re.search(r"面试官\s*([一-龥]{2,3})", q):
        f["interviewer"] = m.group(1)
    names = [r["name"] for r in db.q("SELECT DISTINCT name FROM candidates") if r["name"] in q]
    group_by = None
    if re.search(r"各岗位|每个岗位|按岗位|哪个岗位|岗位分布", q):
        group_by, f["position"] = "position", None
    elif re.search(r"各渠道|每个渠道|按渠道|哪个渠道|渠道分布|渠道效果", q):
        group_by, f["channel"] = "channel", None
    elif re.search(r"各阶段|阶段分布", q):
        group_by = "stage"
    elif re.search(r"各面试官|每个面试官|哪个面试官", q):
        group_by = "interviewer"
    if names:
        f["name"] = max(names, key=len)
        typ = "detail"
    elif re.search(r"漏斗|转化", q):
        typ = "funnel"
    elif group_by:
        typ = "group"
    elif re.search(r"哪些|有谁|名单|列表|都是谁|是谁", q):
        typ = "list"
    else:
        typ = "count"
    return {"type": typ, "filters": f, "group_by": group_by}


def llm_plan(q: str) -> dict:
    system = PLAN_PROMPT.format(now=now().strftime("%Y-%m-%d %H:%M"), positions=POSITIONS, channels=CHANNELS,
                                stages=STAGES, statuses=STATUSES, periods=PERIODS)
    plan, _ = llm.chat_json(system, q, max_tokens=400)
    plan.setdefault("filters", {})
    if plan["filters"].get("period") not in PERIODS:
        plan["filters"]["period"] = "all"
    if plan.get("type") not in ("count", "list", "group", "funnel", "detail"):
        plan["type"] = "count"
    if plan.get("group_by") not in GROUP_LABEL:
        plan["group_by"] = None
    return plan


# ---------------- 执行 ----------------
def execute(plan: dict) -> dict:
    f = {k: v for k, v in (plan.get("filters") or {}).items() if v not in (None, "", "null")}
    rows = db.all_candidates()
    period = f.get("period", "all")
    rng = period_range(period)
    note = []
    if rng:
        a, b = ts(rng[0]), ts(rng[1])
        if f.get("stage") or f.get("status"):
            col, val = ("stage_to", f["stage"]) if f.get("stage") else ("status_to", f["status"])
            ids = {r["candidate_id"] for r in db.q(
                f"SELECT candidate_id FROM events WHERE {col}=? AND ts>=? AND ts<?", (val, a, b))}
            rows = [r for r in rows if r["id"] in ids]
            note.append(f"{PERIOD_LABEL[period]}进入「{val}」")
            f.pop("stage" if f.get("stage") else "status")
        else:
            rows = [r for r in rows if a <= r["created_at"] < b]
            note.append(f"{PERIOD_LABEL[period]}新增")
    for key in ("position", "channel", "stage", "status", "interviewer", "name"):
        if f.get(key):
            rows = [r for r in rows if (r.get(key) or "") and (f[key] in r[key] or r[key] in f[key])]
            note.append(f"{key}={f[key]}")

    typ = plan.get("type", "count")
    result: dict = {"type": typ, "conditions": note, "count": len(rows)}
    if typ == "funnel":
        fl = funnel(rows)
        result["funnel"] = fl
        result["table"] = {"columns": ["阶段", "到达人数", "环比转化率"],
                           "rows": [[x["stage"], x["count"], f"{x['rate']}%"] for x in fl]}
        result["chart"] = {"type": "funnel", "labels": [x["stage"] for x in fl], "values": [x["count"] for x in fl]}
    elif typ == "group" and plan.get("group_by"):
        g = plan["group_by"]
        cnt = Counter((r.get(g) or "未填写") for r in rows).most_common()
        result["groups"] = dict(cnt)
        result["table"] = {"columns": [GROUP_LABEL[g], "人数"], "rows": [[k, v] for k, v in cnt]}
        result["chart"] = {"type": "bar", "labels": [k for k, _ in cnt], "values": [v for _, v in cnt]}
    elif typ == "detail" and rows:
        r = rows[0]
        events = db.events_for(r["id"])[:5]
        result["candidate"] = {**r, "phone": mask_phone(r.get("phone"))}
        result["table"] = {"columns": ["时间", "操作人", "变更"],
                           "rows": [[e["ts"][5:16], e["operator"] or "",
                                     "；".join(f"{k}:{v[1]}" for k, v in e["changes"].items()
                                              if k in ("stage", "status", "interview_time", "feedback") and v[1])]
                                    for e in events]}
    else:
        result["table"] = {"columns": ["姓名", "岗位", "阶段", "状态", "面试时间", "最后更新"],
                           "rows": [[r["name"], r["position"] or "", r["stage"], r["status"],
                                     (r["interview_time"] or "")[5:], r["updated_at"][5:16]] for r in rows[:20]]}
    return result


def template_answer(q: str, plan: dict, res: dict) -> str:
    cond = "，".join(res["conditions"]) or "全部候选人"
    typ = res["type"]
    if typ == "funnel":
        fl = res["funnel"]
        worst = min(fl[1:4], key=lambda x: x["rate"])
        return (f"招聘漏斗（{cond}）：" + " → ".join(f"{x['stage']} {x['count']}" for x in fl)
                + f"。转化最低的环节是「{worst['stage']}」（{worst['rate']}%），建议重点复盘。")
    if typ == "group":
        g = res.get("groups", {})
        if not g:
            return f"{cond}：暂无数据。"
        top = next(iter(g.items()))
        return f"{cond}，共 {res['count']} 人，按{GROUP_LABEL[plan['group_by']]}分布如下，其中「{top[0]}」最多（{top[1]} 人）。"
    if typ == "detail":
        c = res.get("candidate")
        if not c:
            return "没有找到这位候选人。"
        s = f"{c['name']}｜{c['position'] or '岗位待补充'}｜{c['channel'] or '渠道未知'}，当前「{c['stage']}·{c['status']}」"
        if c.get("interview_time"):
            s += f"，面试时间 {c['interview_time']}" + (f"（面试官 {c['interviewer']}）" if c.get("interviewer") else "")
        if c.get("feedback"):
            s += f"。最新评价：{c['feedback'][-40:]}"
        return s + "。"
    names = "、".join(r[0] for r in res["table"]["rows"][:8])
    return f"{cond}：共 {res['count']} 人" + (f"，包括 {names}" + (" 等" if res["count"] > 8 else "") if names else "") + "。"


def answer(q: str) -> dict:
    engine = "rules"
    plan = None
    if settings.llm_enabled:
        try:
            plan, engine = llm_plan(q), "llm"
        except llm.LLMError as e:
            log.warning("问数计划生成失败，降级规则：%s", e)
    plan = plan or rules_plan(q)
    res = execute(plan)
    text = template_answer(q, plan, res)
    if settings.llm_enabled and engine == "llm":
        try:
            brief = {k: v for k, v in res.items() if k not in ("chart",)}
            brief["table"] = {**res["table"], "rows": res["table"]["rows"][:20]}
            content, _ = llm.chat([{"role": "system", "content": ANSWER_PROMPT},
                                   {"role": "user", "content": f"问题：{q}\n查询结果：{json.dumps(brief, ensure_ascii=False)}"}],
                                  max_tokens=300, temperature=0.3)
            text = content.strip() or text
        except llm.LLMError as e:
            log.warning("问数回答生成失败，使用模板：%s", e)
    return {"text": text, "table": res.get("table"), "chart": res.get("chart"),
            "query": {"plan": plan, "engine": engine, "count": res["count"]}}

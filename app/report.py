"""AI 招聘日报 + 超时预警：代码统计数据 → 大模型写洞察 → 企微群机器人推送。"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter

from . import llm
from .analytics import alerts, by_channel, by_position, funnel, kpis, period_range
from .config import now, settings
from .db import db, ts

log = logging.getLogger("report")

INSIGHT_PROMPT = """你是招聘负责人的数据助理。根据今日招聘数据，写 2-3 条简短洞察/建议（每条 ≤35 字），
只能使用给定数字，聚焦：转化瓶颈、渠道效果、需要跟进的风险。输出 JSON：{"insights": ["...", "..."]}"""


def _today_stats() -> dict:
    a, b = period_range("today")
    evs = db.q("SELECT e.*, c.name, c.position FROM events e JOIN candidates c ON c.id=e.candidate_id "
               "WHERE e.ts>=? AND e.ts<?", (ts(a), ts(b)))
    created = [e for e in evs if e["action"] == "create"]
    stage_moves = [e for e in evs if e["stage_to"] and e["action"] == "update"]
    return {
        "new": len(created),
        "new_by_position": dict(Counter(e["position"] or "待补充" for e in created)),
        "advanced": len({e["candidate_id"] for e in stage_moves}),
        "to_interview": len({e["candidate_id"] for e in evs if e["stage_to"] in ("初试", "复试")}),
        "rejected": len({e["candidate_id"] for e in evs if e["status_to"] == "已淘汰"}),
        "offers": [e["name"] for e in evs if e["stage_to"] == "Offer"],
        "hired": [e["name"] for e in evs if e["stage_to"] == "已入职"],
        "records": len(evs),
    }


def _template_insights(today: dict, fl: list[dict], channels: list[dict], al: dict) -> list[str]:
    out = []
    if len(fl) > 2 and fl[0]["count"]:
        worst = min(fl[1:4], key=lambda x: x["rate"])
        out.append(f"「{worst['stage']}」环节转化率仅 {worst['rate']}%，是当前主要瓶颈。")
    good = [c for c in channels if c["total"] >= 3]
    if good:
        best = max(good, key=lambda c: c["advance_rate"])
        out.append(f"{best['channel']} 进复试率 {best['advance_rate']}%，渠道质量最好，可加大投入。")
    if al["stale"] or al["feedback_missing"]:
        out.append(f"有 {len(al['stale'])} 人超 {settings.stale_days} 天未跟进、{len(al['feedback_missing'])} 场面试未填反馈，请尽快处理。")
    return out[:3]


def build_daily() -> dict:
    today, k, fl, al = _today_stats(), kpis(), funnel(), alerts()
    channels, positions = by_channel(), by_position()
    insights, engine = _template_insights(today, fl, channels, al), "rules"
    if settings.llm_enabled:
        try:
            data = {"today": today, "kpis": k, "funnel": fl, "channels": channels[:6],
                    "positions": [{p["position"]: p["active"]} for p in positions],
                    "stale": len(al["stale"]), "feedback_missing": len(al["feedback_missing"])}
            res, _ = llm.chat_json(INSIGHT_PROMPT, json.dumps(data, ensure_ascii=False), max_tokens=400)
            if isinstance(res.get("insights"), list) and res["insights"]:
                insights, engine = [str(x) for x in res["insights"][:3]], "llm"
        except llm.LLMError as e:
            log.warning("日报洞察生成失败，使用模板：%s", e)

    d = now()
    title = f"📊 招聘日报 · {d:%m月%d日}"
    upcoming = [f"{u['interview_time'][5:]} {u['name']}（{u['position'] or ''}·{u['stage']}，面试官 {u['interviewer'] or '待定'}）"
                for u in al["upcoming"][:6]]
    lines_today = [
        f"新增简历 {today['new']} 份" + (f"（{'、'.join(f'{p}{n}' for p, n in today['new_by_position'].items())}）"
                                     if today["new_by_position"] else ""),
        f"推进 {today['advanced']} 人，淘汰 {today['rejected']} 人",
        f"Offer：{'、'.join(today['offers']) or '无'}；入职：{'、'.join(today['hired']) or '无'}",
    ]
    funnel_line = " → ".join(f"{x['stage']}{x['count']}" for x in fl)

    md = [f"## {title}",
          f"> 在途候选人 <font color=\"info\">{k['active']}</font> 人｜本周面试 {k['interviews_week']} 场｜本月 Offer {k['offers_month']} 个",
          "", "**今日进展**"] + [f"- {x}" for x in lines_today] + [
          "", f"**招聘漏斗**：{funnel_line}"]
    if upcoming:
        md += ["", "**明日前待面试**"] + [f"- {x}" for x in upcoming]
    if al["stale"] or al["feedback_missing"]:
        md += ["", "**<font color=\"warning\">需跟进</font>**"]
        md += [f"- {s['name']}（{s['stage']}）已 {s['days']} 天无进展，负责人 {s['hr_owner'] or '-'}" for s in al["stale"][:5]]
        md += [f"- {m['name']} 的{m['stage']}已结束 {m['hours']} 小时，请 {m['interviewer'] or '面试官'} 填写反馈"
               for m in al["feedback_missing"][:5]]
    md += ["", "**AI 洞察**"] + [f"- {x}" for x in insights]
    md += ["", f"<font color=\"comment\">今日 AI 自动记录 {today['records']} 条，约节省 {today['records'] * 2} 分钟手工录入</font>"]
    markdown = "\n".join(md)
    text = re.sub(r"<font[^>]*>(.*?)</font>", r"\1", markdown)
    text = re.sub(r"^(## |> )", "", text.replace("**", ""), flags=re.M)
    return {"title": title, "markdown": markdown, "text": text, "insights": insights, "engine": engine,
            "today": today, "kpis": k, "alerts": al}


def build_alerts() -> str | None:
    al = alerts()
    if not (al["stale"] or al["feedback_missing"]):
        return None
    md = ["## ⏰ 招聘跟进提醒"]
    for m in al["feedback_missing"]:
        md.append(f"- {m['interviewer'] or '面试官'}：{m['name']} 的{m['stage']}已结束 {m['hours']} 小时，请在群里回复评价")
    for s in al["stale"]:
        md.append(f"- {s['hr_owner'] or 'HR'}：{s['name']}（{s['position'] or ''}·{s['stage']}）已 {s['days']} 天无进展")
    return "\n".join(md)


last_push: dict = {"at": None, "via": None, "sent": 0, "error": None}


def push(markdown: str) -> bool:
    """推送日报 / 提醒到企微群。

    优先由「招聘助手」（智能机器人长连接）主动推送到它被 @ 过的群，群里只需要一个机器人；
    招聘助手不可用（未连接 / 还没被拉进群 / 推送失败）且配置了 Webhook 群机器人时，用 Webhook 兜底。
    """
    from .wecom import aibot_ws
    sent, via, error = 0, None, None
    if aibot_ws.connected():
        r = aibot_ws.push_markdown(markdown)
        sent, via = r["sent"], "招聘助手"
        if r["failed"]:
            error = "；".join(r["errors"])[:200]
        elif not r["targets"]:
            error = "招聘助手还没被拉进任何群（在群里 @ 它一次即可）"
    if not sent and settings.webhook_enabled:
        from .wecom import api
        try:
            api.webhook_markdown(markdown)
            sent, via, error = 1, "Webhook 群机器人", None
        except Exception as e:  # noqa: BLE001
            error = f"群机器人推送失败：{e}"[:200]
    if not sent and not error and not settings.webhook_enabled:
        error = "未配置推送：招聘助手未连接，也没有 Webhook"
    last_push.update(at=ts(), via=via if sent else None, sent=sent, error=error)
    if error:
        log.warning("推送企微群：%s", error)
    return sent > 0
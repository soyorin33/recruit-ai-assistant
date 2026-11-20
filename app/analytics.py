"""统计分析：KPI、招聘漏斗、岗位/渠道分布、趋势、超时预警。全部由代码计算，大模型只负责解读。"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .config import now, settings
from .db import db, ts
from .domain import CHANNELS, POSITIONS, STAGE_INDEX, STAGES, STATUS_ACTIVE, mask_phone

MINUTES_SAVED_PER_RECORD = 2  # 估算：人工在群里找信息 + 录入表格一条约 2 分钟


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def period_range(period: str | None) -> tuple[datetime, datetime] | None:
    n = now()
    today = n.replace(hour=0, minute=0, second=0)
    monday = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    return {
        "today": (today, today + timedelta(days=1)),
        "yesterday": (today - timedelta(days=1), today),
        "this_week": (monday, monday + timedelta(days=7)),
        "last_week": (monday - timedelta(days=7), monday),
        "this_month": (month, (month + timedelta(days=32)).replace(day=1)),
        "last_7_days": (today - timedelta(days=6), today + timedelta(days=1)),
    }.get(period or "")


def kpis() -> dict:
    wk = period_range("this_week")
    mo = period_range("this_month")
    w0, w1 = ts(wk[0]), ts(wk[1])
    m0 = ts(mo[0])
    active = db.one("SELECT COUNT(*) n FROM candidates WHERE status=?", (STATUS_ACTIVE,))["n"]
    new_week = db.one("SELECT COUNT(*) n FROM candidates WHERE created_at>=? AND created_at<?", (w0, w1))["n"]
    interviews_week = db.one(
        "SELECT COUNT(*) n FROM candidates WHERE interview_time>=? AND interview_time<?",
        (wk[0].strftime("%Y-%m-%d 00:00"), wk[1].strftime("%Y-%m-%d 00:00")))["n"]
    offers_month = db.one("SELECT COUNT(DISTINCT candidate_id) n FROM events WHERE stage_to='Offer' AND ts>=?", (m0,))["n"]
    hired_month = db.one("SELECT COUNT(DISTINCT candidate_id) n FROM events WHERE stage_to='已入职' AND ts>=?", (m0,))["n"]
    auto_week = db.one("SELECT COUNT(*) n FROM events WHERE ts>=? AND ts<?", (w0, w1))["n"]
    auto_30d = db.one("SELECT COUNT(*) n FROM events WHERE ts>=?", (ts(now() - timedelta(days=30)),))["n"]
    al = alerts()
    return {
        "active": active, "new_week": new_week, "interviews_week": interviews_week,
        "offers_month": offers_month, "hired_month": hired_month,
        "auto_records_week": auto_week, "auto_records_30d": auto_30d,
        "hours_saved_30d": round(auto_30d * MINUTES_SAVED_PER_RECORD / 60, 1),
        "alerts": len(al["stale"]) + len(al["feedback_missing"]),
    }


def funnel(rows: list[dict] | None = None) -> list[dict]:
    rows = rows if rows is not None else db.all_candidates()
    reached = [0] * len(STAGES)
    for r in rows:
        for i in range(STAGE_INDEX.get(r["stage"], 0) + 1):
            reached[i] += 1
    out = []
    for i, s in enumerate(STAGES):
        prev = reached[i - 1] if i else reached[0]
        out.append({"stage": s, "count": reached[i],
                    "rate": round(reached[i] / prev * 100, 1) if prev else 0.0})
    return out


def by_position() -> list[dict]:
    rows = db.all_candidates()
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[r.get("position") or "待补充"].append(r)
    order = {p: i for i, p in enumerate(POSITIONS)}
    out = []
    for pos, items in sorted(groups.items(), key=lambda kv: order.get(kv[0], 99)):
        cnt = Counter(i["stage"] for i in items if i["status"] == STATUS_ACTIVE)
        out.append({"position": pos, "total": len(items),
                    "active": sum(1 for i in items if i["status"] == STATUS_ACTIVE),
                    "by_stage": {s: cnt.get(s, 0) for s in STAGES},
                    "rejected": sum(1 for i in items if i["status"] == "已淘汰"),
                    "hired": sum(1 for i in items if i["status"] == "已入职")})
    return out


def by_channel() -> list[dict]:
    rows = db.all_candidates()
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[r.get("channel") or "未知"].append(r)
    out = []
    for ch, items in groups.items():
        adv = sum(1 for i in items if STAGE_INDEX.get(i["stage"], 0) >= STAGE_INDEX["复试"])
        offer = sum(1 for i in items if STAGE_INDEX.get(i["stage"], 0) >= STAGE_INDEX["Offer"])
        out.append({"channel": ch, "total": len(items), "advanced": adv, "offer": offer,
                    "advance_rate": round(adv / len(items) * 100, 1)})
    order = {c: i for i, c in enumerate(CHANNELS)}
    return sorted(out, key=lambda x: (-x["total"], order.get(x["channel"], 99)))


def trend(days: int = 14) -> dict:
    today = now().replace(hour=0, minute=0, second=0)
    start = today - timedelta(days=days - 1)
    labels = [(start + timedelta(days=i)).strftime("%m-%d") for i in range(days)]
    new = Counter(r["created_at"][5:10] for r in db.q("SELECT created_at FROM candidates WHERE created_at>=?", (ts(start),)))
    rec = Counter(r["ts"][5:10] for r in db.q("SELECT ts FROM events WHERE ts>=?", (ts(start),)))
    return {"labels": labels, "new": [new.get(d, 0) for d in labels], "records": [rec.get(d, 0) for d in labels]}


def alerts() -> dict:
    n = now()
    stale_before = n - timedelta(days=settings.stale_days)
    tomorrow_end = (n + timedelta(days=2)).replace(hour=0, minute=0, second=0)
    stale, missing, upcoming = [], [], []
    for r in db.q("SELECT * FROM candidates WHERE status=?", (STATUS_ACTIVE,)):
        upd, it = _dt(r["updated_at"]), _dt(r["interview_time"])
        brief = {"id": r["id"], "name": r["name"], "position": r["position"], "stage": r["stage"],
                 "interviewer": r["interviewer"], "interview_time": r["interview_time"], "hr_owner": r["hr_owner"]}
        if it and n <= it < tomorrow_end:
            upcoming.append(brief)
        elif it and it < n - timedelta(hours=2) and upd and upd < it:
            missing.append({**brief, "hours": int((n - it).total_seconds() // 3600)})
        elif upd and upd < stale_before and not (it and it >= n):
            stale.append({**brief, "days": (n - upd).days})
    upcoming.sort(key=lambda x: x["interview_time"])
    stale.sort(key=lambda x: -x["days"])
    return {"stale": stale, "feedback_missing": missing, "upcoming": upcoming}


def candidates_table() -> list[dict]:
    return [{**r, "phone": mask_phone(r.get("phone"))} for r in db.all_candidates()]


def dashboard() -> dict:
    return {"kpis": kpis(), "funnel": funnel(), "by_position": by_position(), "by_channel": by_channel(),
            "trend": trend(), "alerts": alerts(), "stages": STAGES}

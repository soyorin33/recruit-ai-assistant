"""演示数据：模拟过去 3 周由「招聘助手」自动记录的真实招聘过程（时间相对当前生成，打开即是新鲜数据）。"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from .config import now
from .db import DB
from .domain import STAGES

NAMES = ("林子轩 陈雨桐 黄嘉豪 吴思远 周梓涵 徐浩然 马晓东 朱一鸣 胡雅婷 郭子豪 何欣怡 高俊杰 罗晨曦 梁文博 宋佳琪 "
         "唐志强 韩雪 冯天宇 邓佳怡 曹宇航 彭思琪 曾凡 肖逸飞 田甜 董浩 袁梦 潘宇 蒋文静 蔡明轩 杜若溪 "
         "叶开 程可欣 苏航 魏然 吕婷婷 丁一凡 沈清 姚远 卢佳 姜涛").split()
POSITION_W = [("后端开发", 9), ("前端开发", 7), ("产品经理", 5), ("算法工程师", 4), ("测试工程师", 3),
              ("数据分析师", 3), ("UI设计师", 2), ("运营专员", 3)]
CHANNEL_W = [("BOSS直聘", 10), ("内推", 5), ("猎聘", 4), ("拉勾", 3), ("智联招聘", 2), ("官网投递", 2), ("猎头", 1)]
INTERVIEWERS = {"后端开发": ["陈晨", "周强"], "前端开发": ["林峰", "吴磊"], "产品经理": ["何静"], "算法工程师": ["郑凯"],
                "测试工程师": ["吴磊"], "数据分析师": ["何静"], "UI设计师": ["林峰"], "运营专员": ["许敏"]}
HRS = ["HR小王", "HR小李"]
PASS_FB = ["基础扎实，沟通清晰", "项目经验匹配度高", "学习能力强", "逻辑清晰，有亮点", "业务理解不错"]
FAIL_FB = ["基础偏弱", "项目深度不够", "薪资期望过高", "稳定性存疑", "沟通表达一般", "与岗位方向不匹配"]
ROUND_WORD = {"初试": "一面", "复试": "二面"}


def _pick(rng: random.Random, weighted: list[tuple[str, int]]) -> str:
    return rng.choices([w[0] for w in weighted], weights=[w[1] for w in weighted])[0]


def _work_time(rng: random.Random, d: datetime) -> datetime:
    return d.replace(hour=rng.randint(9, 18), minute=rng.choice([0, 10, 20, 30, 40, 50]), second=0)


def _add(db: DB, rng: random.Random, n: datetime, name: str, position: str, channel: str, created: datetime,
         final_idx: int, outcome: str, schedule: str | None = None) -> None:
    hr = rng.choice(HRS)
    stage = "简历筛选"
    fields = {"name": name, "position": position, "channel": channel, "stage": stage, "status": "进行中",
              "hr_owner": hr, "phone": f"1{rng.choice('3578')}{rng.randint(100000000, 999999999)}"}
    cid = db.create_candidate(fields, at=created)
    db.add_event(cid, "create", {k: [None, v] for k, v in fields.items() if k != "phone"}, operator=hr,
                 raw_text=f"{name} {position} {channel}投的简历", engine="llm", source="wecom", at=created)
    t = created
    interviewer = rng.choice(INTERVIEWERS[position])
    for i in range(1, final_idx + 1):
        nt = _work_time(rng, t + timedelta(days=rng.uniform(0.6, 2.6)))
        if nt >= n - timedelta(hours=1):
            break
        t, prev, stage = nt, stage, STAGES[i]
        upd = {"stage": stage}
        ch = {"stage": [prev, stage]}
        if stage in ("初试", "复试"):
            upd["interviewer"] = interviewer
        if prev in ROUND_WORD:
            fb = rng.choice(PASS_FB)
            upd["feedback"] = fb
            ch["feedback"] = [None, fb]
            raw = f"{name}{ROUND_WORD[prev]}过了，{fb}"
        else:
            raw = {"初试": f"{name}简历通过，约一面", "Offer": f"{name}发offer了", "已入职": f"{name}今天入职"}.get(stage, name)
        if stage == "已入职":
            upd["status"] = ch["status"] = "已入职"
            ch["status"] = ["进行中", "已入职"]
        db.update_candidate(cid, upd, at=t)
        db.add_event(cid, "update", ch, operator=hr, raw_text=raw, engine="llm", source="wecom", at=t)

    if outcome in ("已淘汰", "已放弃") and stage != "已入职":
        nt = _work_time(rng, t + timedelta(days=rng.uniform(0.3, 1.5)))
        if nt < n - timedelta(hours=1):
            fb = rng.choice(FAIL_FB) if outcome == "已淘汰" else "候选人选择了其他机会"
            word = ROUND_WORD.get(stage, "简历")
            db.update_candidate(cid, {"status": outcome, "feedback": fb}, at=nt)
            db.add_event(cid, "update", {"status": ["进行中", outcome], "feedback": [None, fb]}, operator=hr,
                         raw_text=f"{name}{word}{'挂了' if outcome == '已淘汰' else '放弃了'}，{fb}",
                         engine="llm", source="wecom", at=nt)
            return

    if stage in ("初试", "复试"):
        if schedule == "upcoming":
            it = _work_time(rng, n + timedelta(days=rng.choice([0, 1, 1, 2]))).replace(minute=0)
            if it <= n + timedelta(minutes=30):
                it = _work_time(rng, n + timedelta(days=1)).replace(minute=0)
        elif schedule == "missing":
            it = (t + (n - t) * 0.6).replace(hour=rng.randint(10, 16), minute=0, second=0)
            if it <= t or it > n - timedelta(hours=3):
                return
        else:
            return
        db.update_candidate(cid, {"interview_time": it.strftime("%Y-%m-%d %H:%M")}, at=t)


def seed(db: DB, welcome: str) -> None:
    rng = random.Random(2026)
    n = now()
    # ---- 演示脚本会用到的固定候选人 ----
    _add(db, rng, n, "王芳", "算法工程师", "猎聘", n - timedelta(days=6), 2, "进行中", schedule="missing")
    _add(db, rng, n, "赵磊", "后端开发", "内推", n - timedelta(days=9), 2, "进行中", schedule="upcoming")
    _add(db, rng, n, "孙浩", "前端开发", "BOSS直聘", n - timedelta(days=14), 3, "进行中")
    # ---- 随机历史数据 ----
    for name in NAMES:
        age = rng.uniform(0.2, 21)
        created = _work_time(rng, n - timedelta(days=age))
        if created > n:
            created = n - timedelta(hours=1)
        max_idx = min(4, int(age // 2.2))
        final_idx = min(max_idx, rng.choices([0, 1, 2, 3, 4], weights=[3, 4, 3, 2, 1])[0])
        outcome = rng.choices(["进行中", "已淘汰", "已放弃"], weights=[55, 38, 7])[0]
        if final_idx == 0 and age > 4 and rng.random() < 0.8:  # 早期简历大多已被筛掉
            outcome = "已淘汰"
        schedule = rng.choices(["upcoming", "missing", None], weights=[5, 2, 3])[0]
        _add(db, rng, n, name, _pick(rng, POSITION_W), _pick(rng, CHANNEL_W), created, final_idx, outcome, schedule)
    db.add_message("bot", welcome, operator="招聘助手", source="web", payload={"kind": "chat"})

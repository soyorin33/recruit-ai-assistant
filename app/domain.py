"""招聘业务的领域定义：阶段、状态、字段、词典。规则引擎与大模型 Prompt 共用这一份定义。"""
from __future__ import annotations

import re

# 招聘阶段（有序）：候选人「当前所处 / 待进行」的阶段
STAGES = ["简历筛选", "初试", "复试", "Offer", "已入职"]
STAGE_INDEX = {s: i for i, s in enumerate(STAGES)}

# 候选人状态
STATUS_ACTIVE = "进行中"
STATUS_REJECTED = "已淘汰"
STATUS_DROPPED = "已放弃"
STATUS_HIRED = "已入职"
STATUSES = [STATUS_ACTIVE, STATUS_REJECTED, STATUS_DROPPED, STATUS_HIRED]

# 可由 AI 写入的字段（白名单，防止模型输出无关字段）
EDITABLE_FIELDS = [
    "name", "phone", "email", "position", "channel", "stage", "status",
    "interviewer", "interview_time", "feedback", "resume_summary",
]

FIELD_LABELS = {
    "id": "编号", "name": "姓名", "phone": "手机", "email": "邮箱", "position": "应聘岗位",
    "channel": "渠道", "stage": "当前阶段", "status": "状态", "interviewer": "面试官",
    "interview_time": "面试时间", "feedback": "面试评价", "resume_summary": "简历摘要",
    "hr_owner": "负责HR", "created_at": "建档时间", "updated_at": "最后更新",
    "match_score": "岗位匹配度", "match_detail": "匹配详情",
}

# 岗位词典：(正则, 标准岗位名)，按优先级排列
POSITION_PATTERNS: list[tuple[str, str]] = [
    (r"java|后端|服务端|golang|go开发|python开发", "后端开发"),
    (r"前端|web开发|vue|react", "前端开发"),
    (r"安卓|android|ios|移动端|客户端", "移动端开发"),
    (r"算法|机器学习|大模型|(?<![a-z])(nlp|cv)(?![a-z])", "算法工程师"),
    (r"测试|(?<![a-z])qa(?![a-z])", "测试工程师"),
    (r"数据分析|数分|(?<![a-z])bi(?![a-z])", "数据分析师"),
    (r"产品经理|产品|(?<![a-z])pm(?![a-z])", "产品经理"),
    (r"(?<![a-z])(ui|ux)(?![a-z])|视觉|交互|设计", "UI设计师"),
    (r"运营", "运营专员"),
    (r"销售|商务|(?<![a-z])bd(?![a-z])", "销售经理"),
]
POSITIONS = list(dict.fromkeys(p for _, p in POSITION_PATTERNS))

# 渠道词典
CHANNEL_PATTERNS: list[tuple[str, str]] = [
    (r"boss", "BOSS直聘"),
    (r"猎聘", "猎聘"),
    (r"拉勾", "拉勾"),
    (r"智联", "智联招聘"),
    (r"前程无忧|51job", "前程无忧"),
    (r"脉脉", "脉脉"),
    (r"内推|推荐", "内推"),
    (r"猎头", "猎头"),
    (r"官网", "官网投递"),
    (r"校招|校园", "校园招聘"),
]
CHANNELS = list(dict.fromkeys(c for _, c in CHANNEL_PATTERNS))

# 面试轮次口语 → 标准阶段
ROUND_TO_STAGE = {
    "一面": "初试", "初试": "初试", "初面": "初试",
    "二面": "复试", "复试": "复试", "三面": "复试", "终面": "复试", "hr面": "复试",
}


def match_dict(text: str, patterns: list[tuple[str, str]]) -> str | None:
    low = text.lower()
    for pat, value in patterns:
        if re.search(pat, low):
            return value
    return None


def normalize_phone(value: str | None) -> str | None:
    """「138-0013-8000」「+86 138 0013 8000」→ 13800138000；不是 11 位手机号时原样保留（去掉空格）。"""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return digits if len(digits) == 11 and digits.startswith("1") else str(value).strip() or None


def normalize_position(value: str | None) -> str | None:
    if not value:
        return None
    return match_dict(value, POSITION_PATTERNS) or value.strip()


def normalize_channel(value: str | None) -> str | None:
    if not value:
        return None
    return match_dict(value, CHANNEL_PATTERNS) or value.strip()


def mask_phone(phone: str | None) -> str:
    if not phone or len(phone) < 7:
        return phone or ""
    return phone[:3] + "****" + phone[-4:]

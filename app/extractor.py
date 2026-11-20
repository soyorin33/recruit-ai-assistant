"""AI 信息抽取：把 HR 的口语化消息 / 简历文本 → 结构化候选人数据。

主路径：大模型 + JSON Schema 约束 + Few-shot。
兜底路径：规则引擎（无 API Key、模型超时或返回异常时自动降级，保证业务不中断）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from . import llm
from .config import now, settings
from .domain import (
    CHANNELS, POSITIONS, ROUND_TO_STAGE, STAGES, STATUS_ACTIVE, STATUS_DROPPED, STATUS_HIRED,
    STATUS_REJECTED, STATUSES, EDITABLE_FIELDS, CHANNEL_PATTERNS, POSITION_PATTERNS, match_dict,
    normalize_channel, normalize_position,
)

log = logging.getLogger("extractor")
WEEKDAYS = "一二三四五六日"

# ============================================================
# 1. 大模型抽取
# ============================================================

FIELD_RULES_TEMPLATE = """## 字段规范（消息中没提到的字段一律输出 null，禁止猜测）
- name：候选人姓名（注意区分面试官、HR）
- phone / email：联系方式
- position：应聘岗位，尽量归一到 {positions}，都不匹配则保留原文
- channel：招聘渠道，尽量归一到 {channels}
- stage：候选人「当前所处 / 即将进行」的阶段，只能取 {stages}
  · 一面/初试/初面→初试；二面/三面/终面/HR面/复试→复试
  · 「X面通过/过了」表示进入下一阶段：一面过了→复试；复试/终面过了→Offer；简历筛选通过→初试
  · 「约了/安排 X面」→ 该阶段；「发了 offer」→Offer；「入职/报到」→已入职
- status：只能取 {statuses}
  · 挂了/不合适/没过/淘汰→已淘汰（stage 停留在被淘汰的那一轮）
  · 候选人拒绝 offer / 放弃 / 失联→已放弃；入职→已入职；其余推进→进行中
- interviewer：下一轮（或刚结束这一轮）的面试官姓名
- interview_time：下一次面试时间，换算为绝对时间 "YYYY-MM-DD HH:MM"。只说上午默认 10:00，下午 14:00，晚上 19:00；只有日期默认 10:00
- feedback：面试评价 / 备注的简短概括（≤40 字）"""


def field_rules() -> str:
    """字段规范（抽取 Prompt 与智能体 Prompt 共用）。"""
    return FIELD_RULES_TEMPLATE.format(positions="、".join(POSITIONS), channels="、".join(CHANNELS),
                                       stages="、".join(STAGES), statuses="、".join(STATUSES))


MESSAGE_SYSTEM_PROMPT = """你是企业招聘助手的「信息抽取引擎」。HR 会在企业微信群里用口语描述候选人进展，你要把它转换成结构化 JSON。
当前时间：{now}（星期{weekday}）。

{field_rules}
## 意图 intent
- update：登记 / 更新候选人信息（一条消息可能包含多个候选人）
- query：询问统计数据或候选人情况 → 原问题放入 question
- report：要日报 / 周报 / 汇总
- chat：其他闲聊

## 已有候选人（判断是否同一人；同一人请保持姓名一致）
{known}

## 示例
输入：张三 Java后端 BOSS来的，今天一面过了，周五下午二面，面试官李四
输出：{{"intent":"update","candidates":[{{"name":"张三","phone":null,"email":null,"position":"后端开发","channel":"BOSS直聘","stage":"复试","status":"进行中","interviewer":"李四","interview_time":"<本周五> 14:00","feedback":null}}],"question":null,"confidence":0.95}}
输入：王芳二面挂了，算法基础薄弱；赵磊接了offer
输出：{{"intent":"update","candidates":[{{"name":"王芳","stage":"复试","status":"已淘汰","feedback":"算法基础薄弱", ...其余为null}},{{"name":"赵磊","stage":"Offer","status":"进行中", ...其余为null}}],"question":null,"confidence":0.92}}

## 输出（只输出 JSON，不要任何解释）
{{"intent": "...", "candidates": [...], "question": null, "confidence": 0.0-1.0}}
confidence 表示你对抽取准确性的整体把握；信息含糊、姓名不确定、存在歧义时给低分（<0.7）。"""

RESUME_SYSTEM_PROMPT = """你是资深招聘专员，请从简历文本中提取关键信息，只输出 JSON：
{{"name": 姓名, "phone": 手机号, "email": 邮箱, "position": 求职意向岗位（尽量归一到 {positions}）,
"education": 最高学历及学校, "years": 工作年限(数字), "skills": [核心技能, 最多6个],
"summary": "一句话画像（≤60字，含年限/学历/核心能力/亮点）", "confidence": 0.0-1.0}}
没有的信息填 null，禁止编造。"""


def _known_text(known: list[dict]) -> str:
    if not known:
        return "（暂无）"
    lines = [f"- {c['name']}（{c.get('position') or '岗位未知'}，{c['stage']}，{c['status']}）" for c in known[:80]]
    return "\n".join(lines)


def _clean_candidate(c: dict) -> dict | None:
    """白名单 + 枚举校验 + 归一化，防止模型输出越界内容。"""
    if not isinstance(c, dict) or not c.get("name"):
        return None
    out = {k: c.get(k) for k in EDITABLE_FIELDS if c.get(k) not in (None, "", "null")}
    out["name"] = str(out["name"]).strip()
    if "position" in out:
        out["position"] = normalize_position(str(out["position"]))
    if "channel" in out:
        out["channel"] = normalize_channel(str(out["channel"]))
    if out.get("stage") not in STAGES:
        out.pop("stage", None)
    if out.get("status") not in STATUSES:
        out.pop("status", None)
    if "interview_time" in out:
        try:
            datetime.strptime(str(out["interview_time"]), "%Y-%m-%d %H:%M")
        except ValueError:
            out.pop("interview_time")
    for k, v in list(out.items()):
        out[k] = str(v).strip() if not isinstance(v, str) else v.strip()
    return out


def llm_extract_message(text: str, known: list[dict]) -> dict:
    n = now()
    system = MESSAGE_SYSTEM_PROMPT.format(
        now=n.strftime("%Y-%m-%d %H:%M"), weekday=WEEKDAYS[n.weekday()],
        field_rules=field_rules(), known=_known_text(known),
    )
    data, meta = llm.chat_json(system, text)
    intent = data.get("intent") if data.get("intent") in ("update", "query", "report", "chat") else "chat"
    cands = [c for c in (_clean_candidate(x) for x in data.get("candidates") or []) if c]
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0.8))))
    except (TypeError, ValueError):
        conf = 0.6
    return {"intent": intent, "candidates": cands, "question": data.get("question") or text,
            "confidence": conf, "engine": "llm", "llm_meta": meta}


def llm_extract_resume(text: str) -> dict:
    data, meta = llm.chat_json(RESUME_SYSTEM_PROMPT.format(positions="、".join(POSITIONS)), text[:6000])
    data["engine"] = "llm"
    data["llm_meta"] = meta
    return data


# ============================================================
# 2. 规则引擎（兜底）
# ============================================================

SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈"
    "姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
    "常温康施文牛樊葛邢安齐易乔伍庞颜倪庄聂章鲁岳翟殷詹申欧耿关兰焦俞左柳甘祝包宁尚符舒阮柯纪梅童凌毕单季裴霍涂成苗谷盛曲翁冉骆蓝路游辛靳管柴蒙鲍华喻祁蒲房滕屈饶解牟艾尤阳时穆农司卓古吉缪简车项连芦麦褚娄窦戚岑景党宫费卜冷晏席卫米柏宗瞿桂全佟应臧闵苟邬边卞姬师和仇栾隋商刁沙荣巫寇桑郎甄丛仲虞敖巩明佘池查麻苑迟邝"
)
# 姓名后紧跟这些字时，判定姓名只有两个字（例如「张三一面过了」）
NAME_STOP_NEXT = tuple(
    ["一面", "二面", "三面", "初试", "复试", "终面", "初面", "面试", "今天", "今晚", "明天", "明早", "后天", "上午",
     "下午", "晚上", "下周", "周一", "周二", "周三", "周四", "周五", "周六", "周日", "已经", "通过", "入职", "放弃",
     "昨天", "前端", "后端", "简历", "应聘", "岗位", "目前", "现在", "刚刚", "投了", "推荐", "内推"]
    + list("的是这那在也再和跟说要被给发拒刚来做投吧呢啊了过接已挂约没不想去还")
)
# 以姓氏开头但明显不是人名的常见词
NOT_NAMES = {"周一", "周二", "周三", "周四", "周五", "周六", "周日", "周天", "周末", "明天", "明早", "明晚",
             "安排", "高级", "方向", "和他", "时间", "全部", "全职", "应聘", "成功", "简历", "向他", "于是", "常规",
             "管理", "华为", "史上", "关于", "单位", "包括", "顾问", "毕业", "司机", "解决", "施工", "车辆", "金额", "金融"}
NAME_BOUNDARY = set("，,。；;、！!？? \n\t：:（(）)") | {""}

ROUND_RE = "一面|二面|三面|终面|初试|复试|初面|hr面"
FAIL_RE = r"挂了|挂掉|挂在|不合适|淘汰|没过|未通过|不通过|pass掉|刷了|不考虑"
DROP_RE = r"拒了?offer|拒绝了?offer|不来了|放弃|鸽了|失联|爽约|去了别家"
HIRE_RE = r"入职|报到|报道"


def _find_names(text: str, known_names: list[str]) -> list[tuple[int, str]]:
    """返回 [(位置, 姓名)]。先匹配已有候选人，再用「常见姓氏 + 边界」启发式发现新名字。"""
    found: dict[int, str] = {}
    blocked: list[tuple[int, int]] = []
    for m in re.finditer(r"(面试官|HR|hr|面试人|由)(?:是|为|换成|改成|改为|换为)?[:：]?\s*([一-龥]{2,3})", text):
        blocked.append((m.start(), m.end()))

    def is_blocked(i: int) -> bool:
        return any(a <= i < b for a, b in blocked)

    for name in sorted(set(known_names), key=len, reverse=True):
        for m in re.finditer(re.escape(name), text):
            if not is_blocked(m.start()) and not any(a <= m.start() < a + len(n) for a, n in found.items()):
                found[m.start()] = name

    for i, ch in enumerate(text):
        if ch not in SURNAMES or is_blocked(i) or any(a <= i < a + len(n) for a, n in found.items()):
            continue
        prev = text[i - 1] if i > 0 else ""
        prev2 = text[max(0, i - 3):i]
        if prev not in NAME_BOUNDARY and not prev2.endswith(("候选人", "还有", "另外", "然后")):
            continue
        if i + 1 >= len(text) or not ("一" <= text[i + 1] <= "龥"):
            continue
        name = text[i:i + 2]
        if (i + 2 < len(text) and "一" <= text[i + 2] <= "龥"
                and not text[i + 2:].startswith(NAME_STOP_NEXT)):
            name = text[i:i + 3]
        if name in NOT_NAMES or name[:2] in NOT_NAMES:
            continue
        found[i] = name
    return sorted(found.items())


def parse_time(text: str, base: datetime | None = None) -> str | None:
    base = base or now()
    day: datetime | None = None
    t = text
    if m := re.search(r"(\d{1,2})月(\d{1,2})[日号]", t):
        try:
            day = base.replace(month=int(m.group(1)), day=int(m.group(2)))
        except ValueError:
            day = None
    elif m := re.search(r"(下下|下个?)?(周|星期|礼拜)([一二三四五六日天1-7])", t):
        wd_char = m.group(3)
        target = "一二三四五六日".find(wd_char) if wd_char in "一二三四五六日" else (6 if wd_char == "天" else int(wd_char) - 1)
        monday = base - timedelta(days=base.weekday())
        weeks = 2 if m.group(1) == "下下" else (1 if m.group(1) else 0)
        day = monday + timedelta(days=7 * weeks + target)
        if weeks == 0 and day.date() < base.date():
            day += timedelta(days=7)
    elif "大后天" in t:
        day = base + timedelta(days=3)
    elif "后天" in t:
        day = base + timedelta(days=2)
    elif re.search(r"明天|明早|明晚|明日", t):
        day = base + timedelta(days=1)
    elif re.search(r"今天|今晚|今日|今早", t):
        day = base

    hour, minute = None, 0
    if m := re.search(r"(\d{1,2})\s*[点:：时]\s*(半|\d{1,2})?", t):
        hour = int(m.group(1))
        if m.group(2):
            minute = 30 if m.group(2) == "半" else int(m.group(2))
        if re.search(r"下午|晚上|今晚|明晚", t) and hour < 12:
            hour += 12
    elif re.search(r"上午|早上|明早|今早", t):
        hour = 10
    elif "中午" in t:
        hour = 12
    elif "下午" in t:
        hour = 14
    elif re.search(r"晚上|今晚|明晚", t):
        hour = 19

    if day is None and hour is None:
        return None
    day = day or base
    if hour is None:
        hour = 10
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return day.replace(hour=hour, minute=minute, second=0).strftime("%Y-%m-%d %H:%M")


def _next_stage(stage: str) -> str:
    i = STAGES.index(stage)
    return STAGES[min(i + 1, len(STAGES) - 1)]


def _rules_chunk(chunk: str, name: str) -> dict:
    low = chunk.lower()
    c: dict = {"name": name}
    # 岗位只在「状态描述」之前的部分识别，避免把评价里的「算法基础薄弱」误判为算法岗
    head = re.split(rf"{ROUND_RE}|{FAIL_RE}|{DROP_RE}|{HIRE_RE}|评价|反馈|觉得|通过|过了", low)[0]
    if pos := match_dict(head.replace(name, ""), POSITION_PATTERNS):
        c["position"] = pos
    if ch := match_dict(low, CHANNEL_PATTERNS):
        c["channel"] = ch
    if m := re.search(r"1[3-9]\d{9}", chunk):
        c["phone"] = m.group(0)
    if m := re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", chunk):
        c["email"] = m.group(0)
    if m := re.search(r"面试官(?:是|为|换成|改成|改为|换为)?[:：]?\s*([一-龥]{2,3}?)(?=[，,。；;、\s]|$|面|来|负责)", chunk):
        c["interviewer"] = m.group(1)

    rounds = [(m.start(), ROUND_TO_STAGE[m.group(0)]) for m in re.finditer(ROUND_RE, low)]
    if re.search(HIRE_RE, low) and not re.search(r"(下周|明天|后天|周.)入职", low):
        c["stage"], c["status"] = "已入职", STATUS_HIRED
    elif re.search(HIRE_RE, low):  # 约定了入职时间，仍处于 Offer 阶段
        c["stage"], c["status"] = "Offer", STATUS_ACTIVE
        if t := parse_time(chunk):
            c["feedback"] = f"约定 {t[:10]} 入职"
        return c
    elif re.search(DROP_RE, low):
        c["status"] = STATUS_DROPPED
        if "offer" in low:
            c["stage"] = "Offer"
        elif rounds:
            c["stage"] = rounds[-1][1]
    elif "offer" in low:
        c["stage"], c["status"] = "Offer", STATUS_ACTIVE
    else:
        fail = re.search(FAIL_RE, low)
        passed = re.search(rf"({ROUND_RE}|简历筛选|简历)\s*(已经|已|顺利)?\s*(通过|过了|过啦|pass|ok)", low)
        if fail:
            c["status"] = STATUS_REJECTED
            before = [s for p, s in rounds if p < fail.start()]
            if before or rounds:
                c["stage"] = (before or [s for _, s in rounds])[-1]
        elif passed:
            r = passed.group(1)
            c["stage"] = "初试" if r.startswith("简历") else _next_stage(ROUND_TO_STAGE[r])
            c["status"] = STATUS_ACTIVE
            later = [s for p, s in rounds if p > passed.end()]
            if later:
                c["stage"] = later[-1]
        elif rounds:
            c["stage"], c["status"] = rounds[-1][1], STATUS_ACTIVE
        elif re.search(r"简历|投递|新来|新增|推荐|内推", low):
            c["stage"], c["status"] = "简历筛选", STATUS_ACTIVE

    if c.get("status", STATUS_ACTIVE) == STATUS_ACTIVE and (t := parse_time(chunk)):
        c["interview_time"] = t
    if m := re.search(r"(评价|反馈|觉得|印象|表现|备注)[是为:：，,]?\s*([^。；;]{2,40})", chunk):
        c["feedback"] = m.group(2).strip("，, ")
    elif m := re.search(r"[，,\s]\s*([^，,。；;\s]*(基础|能力|沟通|经验|稳定|薪资|期望)[^。；;]{0,20})", chunk):
        c["feedback"] = m.group(1).strip()
    return c


QUERY_RE = r"[？?]|多少|几个|几位|几人|哪些|有谁|统计|查询|查一下|看看|怎么样|进展|情况|漏斗|转化|比例|占比|分布|排名"
REPORT_RE = r"日报|周报|汇总|报告|总结一下|今日小结"


def rules_extract_message(text: str, known: list[dict]) -> dict:
    t = re.sub(r"@\S+\s*", "", text).strip()
    names = _find_names(t, [k["name"] for k in known])
    has_update_words = re.search(
        rf"{ROUND_RE}|{FAIL_RE}|{DROP_RE}|{HIRE_RE}|offer|简历|投递|通过|过了|面试官|约了|安排", t.lower())
    base = {"engine": "rules", "question": t, "candidates": []}
    if re.search(REPORT_RE, t) and not (names and has_update_words):
        return {**base, "intent": "report", "confidence": 0.9}
    if re.search(QUERY_RE, t):
        return {**base, "intent": "query", "confidence": 0.85}
    if not names:
        return {**base, "intent": "chat", "confidence": 0.5}

    cands = []
    for idx, (pos, name) in enumerate(names):
        end = names[idx + 1][0] if idx + 1 < len(names) else len(t)
        start = 0 if idx == 0 else pos
        cands.append(_rules_chunk(t[start:end], name))
    known_set = {k["name"] for k in known}
    informative = all(len(c) > 1 for c in cands)
    conf = 0.9 if informative else 0.6
    if any(c["name"] not in known_set and len(c) <= 2 for c in cands):
        conf = min(conf, 0.6)
    return {**base, "intent": "update", "candidates": cands, "confidence": conf}


SKILL_WORDS = ["Java", "Spring", "MySQL", "Redis", "Kafka", "Go", "Python", "Vue", "React", "TypeScript", "Node",
               "Kubernetes", "Docker", "PyTorch", "TensorFlow", "LLM", "SQL", "Tableau", "Figma", "Axure",
               "Android", "iOS", "Flutter", "Selenium", "Linux", "微服务", "分布式", "数据分析", "用户增长"]


def rules_extract_resume(text: str) -> dict:
    d: dict = {"engine": "rules"}
    if m := re.search(r"姓\s*名[:：\s]*([一-龥]{2,4})", text):
        d["name"] = m.group(1)
    else:
        for line in text.splitlines():
            line = line.strip()
            if re.fullmatch(r"[一-龥]{2,4}", line):
                d["name"] = line
                break
    if m := re.search(r"1[3-9]\d{9}", text.replace(" ", "").replace("-", "")):
        d["phone"] = m.group(0)
    if m := re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
        d["email"] = m.group(0)
    if m := re.search(r"(求职意向|意向岗位|应聘岗位|期望职位|应聘职位)[:：\s]*([^\n]{2,20})", text):
        d["position"] = normalize_position(m.group(2))
    else:
        d["position"] = match_dict(text, POSITION_PATTERNS)
    for edu in ("博士", "硕士", "本科", "大专"):
        if edu in text:
            school = re.search(rf"([一-龥]{{2,12}}(大学|学院))[^\n]{{0,20}}{edu}|{edu}[^\n]{{0,20}}?([一-龥]{{2,12}}(大学|学院))", text)
            d["education"] = f"{edu}" + (f" · {school.group(1) or school.group(3)}" if school else "")
            break
    if m := re.search(r"(\d{1,2})\s*年(以上)?(工作|开发|相关)?经验", text):
        d["years"] = int(m.group(1))
    d["skills"] = [s for s in SKILL_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(s)}(?![A-Za-z])", text, re.I)][:6]
    parts = [f"{d['years']}年经验" if d.get("years") else None, d.get("education"),
             "、".join(d["skills"][:4]) if d["skills"] else None]
    d["summary"] = "，".join(p for p in parts if p) or None
    d["confidence"] = 0.85 if d.get("name") and (d.get("phone") or d.get("email")) else 0.5
    return d


# ============================================================
# 3. 统一入口：优先大模型，失败自动降级
# ============================================================

CONFIRM_WORDS = {"确认", "确定", "是", "对", "好", "ok", "1", "y", "yes"}
CANCEL_WORDS = {"取消", "不对", "否", "不是", "算了", "0", "n", "no"}


def extract_message(text: str, known: list[dict]) -> dict:
    bare = re.sub(r"@\S+\s*", "", text).strip().lower().rstrip("。!！")
    if bare in CONFIRM_WORDS:
        return {"intent": "confirm", "candidates": [], "confidence": 1.0, "engine": "rules"}
    if bare in CANCEL_WORDS:
        return {"intent": "cancel", "candidates": [], "confidence": 1.0, "engine": "rules"}
    if settings.llm_enabled:
        try:
            return llm_extract_message(text, known)
        except llm.LLMError as e:
            log.warning("LLM 抽取失败，降级规则引擎：%s", e)
            r = rules_extract_message(text, known)
            r["fallback_reason"] = str(e)[:120]
            return r
    return rules_extract_message(text, known)


def extract_resume(text: str) -> dict:
    if settings.llm_enabled:
        try:
            return llm_extract_resume(text)
        except llm.LLMError as e:
            log.warning("LLM 简历解析失败，降级规则引擎：%s", e)
            r = rules_extract_resume(text)
            r["fallback_reason"] = str(e)[:120]
            return r
    return rules_extract_resume(text)

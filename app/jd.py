"""简历 × 岗位要求（JD）匹配打分：大模型给出分数、命中 / 缺失项、亮点、风险和建议面试问题；规则兜底。

JD 默认内置在下方 DEFAULT_JDS，可通过环境变量 JD_FILE 指向一个 JSON 文件覆盖（结构相同）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from . import llm
from .config import settings

log = logging.getLogger("jd")

DEFAULT_JDS: dict[str, dict] = {
    "后端开发": {"min_years": 3, "must": ["Java", "Spring", "MySQL", "Redis"],
                 "nice": ["Kafka", "微服务", "分布式", "Docker", "Kubernetes", "Go"],
                 "summary": "负责交易与订单核心服务的设计开发，要求扎实的 Java 基础和高并发经验"},
    "前端开发": {"min_years": 2, "must": ["JavaScript", "Vue", "React", "TypeScript"],
                 "nice": ["小程序", "Node", "性能优化", "工程化", "Webpack", "Vite"],
                 "summary": "负责 B 端系统与小程序开发，要求熟悉主流框架和前端工程化"},
    "算法工程师": {"min_years": 2, "must": ["Python", "PyTorch", "机器学习"],
                   "nice": ["LLM", "大模型", "推荐", "NLP", "TensorFlow", "论文"],
                   "summary": "负责推荐与大模型应用算法，要求扎实的机器学习基础和工程能力"},
    "产品经理": {"min_years": 3, "must": ["需求分析", "原型", "数据分析"],
                 "nice": ["Axure", "B端", "SaaS", "用户增长", "项目管理"],
                 "summary": "负责 HR SaaS 产品规划，要求有 B 端产品经验和数据驱动意识"},
    "测试工程师": {"min_years": 2, "must": ["测试用例", "接口测试", "自动化测试"],
                   "nice": ["Selenium", "Python", "性能测试", "JMeter", "CI"],
                   "summary": "负责核心业务质量保障，要求熟悉自动化测试体系"},
    "数据分析师": {"min_years": 2, "must": ["SQL", "Python", "数据分析"],
                   "nice": ["Tableau", "AB测试", "统计", "用户增长", "BI"],
                   "summary": "负责业务指标体系与经营分析，要求熟练 SQL 和分析方法论"},
    "UI设计师": {"min_years": 2, "must": ["Figma", "交互设计", "视觉设计"],
                 "nice": ["设计规范", "B端", "动效", "Sketch"],
                 "summary": "负责产品视觉与交互设计，要求有完整的作品集"},
    "运营专员": {"min_years": 1, "must": ["用户运营", "活动策划", "数据分析"],
                 "nice": ["社群", "内容运营", "私域", "用户增长"],
                 "summary": "负责用户增长与社群运营"},
    "销售经理": {"min_years": 3, "must": ["客户开发", "谈判", "B端"],
                 "nice": ["SaaS", "大客户", "渠道"],
                 "summary": "负责企业客户拓展与签约"},
}

PROMPT = """你是资深招聘专家。请评估候选人简历与岗位要求的匹配度，只输出 JSON：
{{"score": 0-100 的整数, "level": "推荐面试|待定|不推荐",
  "matched": ["命中的要求，≤6项"], "missing": ["缺失或不明确的要求，≤4项"],
  "highlights": ["亮点，≤3条"], "risks": ["风险点，如跳槽频繁 / 年限不足，≤3条"],
  "questions": ["针对性的面试问题，3条，用于验证缺失项和亮点真实性"]}}
评分标准：必备要求权重 60%，加分项 25%，年限与稳定性 15%；≥75 推荐面试，55-74 待定，<55 不推荐。
只依据简历事实，禁止编造。

## 岗位：{position}
职责概述：{summary}
最低年限：{min_years} 年
必备要求：{must}
加分项：{nice}"""


def load_jds() -> dict[str, dict]:
    path = os.getenv("JD_FILE")
    if path and Path(path).exists():
        try:
            return {**DEFAULT_JDS, **json.loads(Path(path).read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError) as e:
            log.warning("JD_FILE 读取失败，使用默认 JD：%s", e)
    return DEFAULT_JDS


def _has(text: str, kw: str) -> bool:
    if re.fullmatch(r"[A-Za-z0-9.+# ]+", kw):
        return re.search(rf"(?<![A-Za-z]){re.escape(kw)}(?![A-Za-z])", text, re.I) is not None
    return kw in text


def rules_match(text: str, position: str, jd: dict, years: int | None) -> dict:
    must = [k for k in jd["must"] if _has(text, k)]
    nice = [k for k in jd["nice"] if _has(text, k)]
    years_ok = years is not None and years >= jd["min_years"]
    score = round(60 * len(must) / len(jd["must"]) + 25 * min(1.0, len(nice) / max(1, len(jd["nice"]) / 2))
                  + (15 if years_ok else 5 if years is None else 0))
    missing = [k for k in jd["must"] if k not in must]
    risks = [] if years is None or years_ok else [f"工作年限 {years} 年，低于要求的 {jd['min_years']} 年"]
    if years is None:
        risks.append("简历未写明工作年限")
    return {
        "score": score, "level": "推荐面试" if score >= 75 else "待定" if score >= 55 else "不推荐",
        "matched": (must + nice)[:6], "missing": missing[:4],
        "highlights": [f"具备加分项：{'、'.join(nice[:3])}"] if nice else [], "risks": risks,
        "questions": ([f"简历里没有体现 {missing[0]}，你有相关经验吗？请举一个例子" if missing else
                       "请挑简历中最有挑战的一个项目，讲讲你负责的部分和最终结果"]
                      + [f"在 {k} 上你具体负责哪一部分？遇到过什么问题、怎么解决的？" for k in must[:2]])[:3],
        "engine": "rules",
    }


def match(resume_text: str, position: str | None, years: int | None = None) -> dict | None:
    """返回匹配结果；岗位没有对应 JD 时返回 None。"""
    jds = load_jds()
    if not position or position not in jds:
        return None
    jd = jds[position]
    if settings.llm_enabled:
        try:
            system = PROMPT.format(position=position, summary=jd["summary"], min_years=jd["min_years"],
                                   must="、".join(jd["must"]), nice="、".join(jd["nice"]))
            data, meta = llm.chat_json(system, resume_text[:6000], max_tokens=700)
            score = max(0, min(100, int(data.get("score", 0))))
            level = data.get("level") if data.get("level") in ("推荐面试", "待定", "不推荐") else (
                "推荐面试" if score >= 75 else "待定" if score >= 55 else "不推荐")
            return {"score": score, "level": level,
                    **{k: [str(x) for x in (data.get(k) or [])][:6] for k in
                       ("matched", "missing", "highlights", "risks", "questions")},
                    "engine": "llm", "model": meta.get("model")}
        except (llm.LLMError, ValueError, TypeError) as e:
            log.warning("JD 匹配大模型失败，使用规则：%s", e)
    return rules_match(resume_text, position, jd, years)


def describe(m: dict) -> str:
    s = f"🎯 岗位匹配度 {m['score']} 分（{m['level']}）"
    if m.get("matched"):
        s += f"\n· 命中：{'、'.join(m['matched'])}"
    if m.get("missing"):
        s += f"\n· 缺失：{'、'.join(m['missing'])}"
    if m.get("risks"):
        s += f"\n· 风险：{'；'.join(m['risks'])}"
    if m.get("questions"):
        s += "\n· 建议面试问题：" + "".join(f"\n  {i}. {q}" for i, q in enumerate(m["questions"], 1))
    return s

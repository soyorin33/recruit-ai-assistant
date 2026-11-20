"""信息抽取准确率评测：同一批标注样本，对比「规则引擎」与「大模型」的效果。

用法（在项目根目录）：
    python -m eval.run_eval                 # 只评测规则引擎（无需 Key）
    python -m eval.run_eval --engine llm    # 评测 .env 中配置的大模型
    python -m eval.run_eval --engine both   # 两者对比，并写入 eval/REPORT.md

评分口径：
- 意图准确率：intent 是否正确
- 姓名召回：期望的候选人是否都被识别出来（同时统计多识别出的「幻觉候选人」）
- 字段准确率：只对标注了的字段打分（position/channel/stage/status/interviewer/interview_time/phone）
- 整条全对率：意图、人员、所有标注字段全部正确
样本使用固定的「当前时间」（2026-11-18 周三 10:00），保证相对时间可复现。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "eval.db"))
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from app import extractor  # noqa: E402
from app.config import settings  # noqa: E402

BASE_NOW = datetime(2026, 11, 18, 10, 0)
FIELDS = ["position", "channel", "stage", "status", "interviewer", "interview_time", "phone"]
PRICE_PER_M = {"input": 2.0, "output": 8.0}  # 元 / 百万 tokens（按 DeepSeek 公开价估算，可按实际模型修改）


def load_cases() -> list[dict]:
    return [json.loads(line) for line in (ROOT / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


XFIELDS = ["stage", "status", "interview_time"]


def cross_stats(case: dict, out: dict, known: list[dict]) -> dict:
    """交叉校验效果：规则引擎独立读同一句话，与被测结果冲突的字段会转人工确认。

    - caught：被测结果错了、且被规则冲突拦下（好）
    - false_alarm：被测结果是对的、却因规则冲突被打扰（成本）
    规则引擎自评时，把标注答案当作「完美模型」的输出，得到误报率上限。
    """
    rules = extractor.rules_extract_message(case["text"], known)
    rmap = {c["name"]: c for c in rules.get("candidates", [])}
    gmap = {c.get("name"): c for c in out.get("candidates", [])}
    st = {"checked": 0, "wrong": 0, "caught": 0, "false_alarm": 0}
    for ec in case["expect"].get("candidates", []):
        gc, rc = gmap.get(ec["name"]), rmap.get(ec["name"])
        for f in XFIELDS:
            if f not in ec or not gc or not gc.get(f):
                continue
            st["checked"] += 1
            wrong = gc[f] != ec[f]
            flagged = bool(rc and rc.get(f) and rc[f] != gc[f])
            st["wrong"] += wrong
            st["caught"] += wrong and flagged
            st["false_alarm"] += (not wrong) and flagged
    return st


def run_engine(engine: str, cases: list[dict]) -> dict:
    extractor.now = lambda: BASE_NOW  # 冻结时间
    results, tokens_in, tokens_out, latencies = [], 0, 0, []
    for case in cases:
        known = [{"name": n, "position": p, "stage": s, "status": "进行中"} for n, p, s in case.get("known", [])]
        t0 = time.time()
        try:
            out = (extractor.llm_extract_message(case["text"], known) if engine == "llm"
                   else extractor.rules_extract_message(case["text"], known))
        except Exception as e:  # 记录失败，不中断评测
            out = {"intent": "error", "candidates": [], "error": str(e)[:120]}
        latencies.append((time.time() - t0) * 1000)
        target = out if engine == "llm" else {"candidates": case["expect"].get("candidates", [])}
        xs = cross_stats(case, target, known)
        meta = out.get("llm_meta") or {}
        tokens_in += meta.get("prompt_tokens") or 0
        tokens_out += meta.get("completion_tokens") or 0
        results.append({**score(case, out), "x": xs})
    return summarize(engine, results, latencies, tokens_in, tokens_out)


def score(case: dict, out: dict) -> dict:
    exp = case["expect"]
    r = {"id": case["id"], "tag": case["tag"], "text": case["text"], "intent_ok": out.get("intent") == exp["intent"],
         "got_intent": out.get("intent"), "field_total": 0, "field_ok": 0, "names_exp": 0, "names_hit": 0,
         "extra_names": [], "errors": []}
    if out.get("error"):
        r["errors"].append(out["error"])
    got = {c.get("name"): c for c in out.get("candidates", [])} if exp["intent"] == "update" else {}
    for ec in exp.get("candidates", []):
        r["names_exp"] += 1
        gc = got.get(ec["name"])
        if gc:
            r["names_hit"] += 1
        for f in FIELDS:
            if f in ec:
                r["field_total"] += 1
                if gc and gc.get(f) == ec[f]:
                    r["field_ok"] += 1
                else:
                    r["errors"].append(f"{ec['name']}.{f}: 期望 {ec[f]}，实际 {gc.get(f) if gc else '未识别'}")
    exp_names = {c["name"] for c in exp.get("candidates", [])}
    r["extra_names"] = [n for n in got if n not in exp_names]
    if not r["intent_ok"]:
        r["errors"].insert(0, f"意图：期望 {exp['intent']}，实际 {out.get('intent')}")
    if r["extra_names"]:
        r["errors"].append(f"多识别出：{'、'.join(map(str, r['extra_names']))}")
    r["all_ok"] = r["intent_ok"] and r["field_ok"] == r["field_total"] and r["names_hit"] == r["names_exp"] and not r["extra_names"]
    return r


def summarize(engine: str, rs: list[dict], lat: list[float], tin: int, tout: int) -> dict:
    n = len(rs)
    ft, fo = sum(r["field_total"] for r in rs), sum(r["field_ok"] for r in rs)
    ne, nh = sum(r["names_exp"] for r in rs), sum(r["names_hit"] for r in rs)
    by_tag: dict[str, list] = {}
    for r in rs:
        by_tag.setdefault(r["tag"], []).append(r["all_ok"])
    cost = (tin * PRICE_PER_M["input"] + tout * PRICE_PER_M["output"]) / 1e6
    xs = {k: sum(r["x"][k] for r in rs) for k in ("checked", "wrong", "caught", "false_alarm")}
    return {
        "engine": engine if engine == "rules" else f"llm:{settings.llm_model}",
        "cases": n,
        "intent_acc": sum(r["intent_ok"] for r in rs) / n,
        "name_recall": nh / ne if ne else 1.0,
        "extra_names": sum(len(r["extra_names"]) for r in rs),
        "field_acc": fo / ft if ft else 1.0,
        "all_ok": sum(r["all_ok"] for r in rs) / n,
        "by_tag": {t: f"{sum(v)}/{len(v)}" for t, v in by_tag.items()},
        "avg_latency_ms": round(sum(lat) / n, 1),
        "tokens_per_msg": round((tin + tout) / n) if tin else None,
        "cost_per_1k_msgs_yuan": round(cost / n * 1000, 2) if tin else None,
        "failures": [r for r in rs if not r["all_ok"]],
        "cross_check": xs,
    }


def print_summary(s: dict) -> None:
    pct = lambda x: f"{x * 100:.1f}%"  # noqa: E731
    print(f"\n===== {s['engine']}（{s['cases']} 条样本）=====")
    print(f"意图准确率 {pct(s['intent_acc'])}｜姓名召回 {pct(s['name_recall'])}｜多识别 {s['extra_names']} 人"
          f"｜字段准确率 {pct(s['field_acc'])}｜整条全对 {pct(s['all_ok'])}")
    print("分类别全对：", "，".join(f"{k} {v}" for k, v in s["by_tag"].items()))
    print(f"平均耗时 {s['avg_latency_ms']} ms" + (f"｜平均 {s['tokens_per_msg']} tokens/条｜约 {s['cost_per_1k_msgs_yuan']} 元/千条"
                                              if s["tokens_per_msg"] else ""))
    x = s["cross_check"]
    if s["engine"] == "rules":
        print(f"交叉校验（假设大模型全对）：{x['checked']} 个关键字段中误报 {x['false_alarm']} 个"
              f"（{x['false_alarm'] / max(1, x['checked']) * 100:.1f}% 会多一次人工确认）")
    else:
        print(f"交叉校验：模型错 {x['wrong']} 个关键字段，被拦下 {x['caught']} 个；误报 {x['false_alarm']} 个")
    for f in s["failures"]:
        print(f"  ✗ #{f['id']} [{f['tag']}] {f['text']}")
        for e in f["errors"][:4]:
            print(f"      - {e}")


def write_report(summaries: list[dict]) -> Path:
    pct = lambda x: f"{x * 100:.1f}%"  # noqa: E731
    lines = ["# 信息抽取评测报告", "",
             f"- 样本：`eval/cases.jsonl`，共 {summaries[0]['cases']} 条（基础 / 口语 / 多人 / 时间 / 意图）",
             f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}，基准时间固定为 {BASE_NOW:%Y-%m-%d %H:%M}（周三）", "",
             "| 引擎 | 意图准确率 | 姓名召回 | 多识别 | 字段准确率 | 整条全对 | 平均耗时 | 千条成本 |",
             "|---|---|---|---|---|---|---|---|"]
    for s in summaries:
        lines.append(f"| {s['engine']} | {pct(s['intent_acc'])} | {pct(s['name_recall'])} | {s['extra_names']} | "
                     f"{pct(s['field_acc'])} | {pct(s['all_ok'])} | {s['avg_latency_ms']} ms | "
                     f"{('约 ' + str(s['cost_per_1k_msgs_yuan']) + ' 元') if s['cost_per_1k_msgs_yuan'] is not None else '0'} |")
    lines += ["", "## 规则 × 大模型交叉校验", "",
              "规则引擎独立读同一句话，与大模型在阶段 / 状态 / 面试时间上冲突时转人工确认。",
              "", "| 引擎 | 关键字段数 | 模型出错 | 被拦下 | 误报（多一次确认） |", "|---|---|---|---|---|"]
    for s in summaries:
        x = s["cross_check"]
        label = "完美模型（以标注为输出，测误报上限）" if s["engine"] == "rules" else s["engine"]
        lines.append(f"| {label} | {x['checked']} | {x['wrong']} | {x['caught']} | {x['false_alarm']} |")
    lines += ["", "## 分类别全对数", "", "| 引擎 | " + " | ".join(summaries[0]["by_tag"]) + " |",
              "|---|" + "---|" * len(summaries[0]["by_tag"])]
    for s in summaries:
        lines.append(f"| {s['engine']} | " + " | ".join(s["by_tag"].get(t, "-") for t in summaries[0]["by_tag"]) + " |")
    for s in summaries:
        lines += ["", f"## {s['engine']} 未全对的样本", ""]
        if not s["failures"]:
            lines.append("无")
        for f in s["failures"]:
            lines.append(f"- **#{f['id']} [{f['tag']}]** {f['text']}")
            lines += [f"  - {e}" for e in f["errors"][:4]]
    path = ROOT / "REPORT.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["rules", "llm", "both"], default="rules")
    args = ap.parse_args()
    if args.engine in ("llm", "both") and not settings.llm_enabled:
        sys.exit("未配置 LLM_API_KEY，请先在 .env 中填写（见 .env.example）")
    cases = load_cases()
    engines = ["rules", "llm"] if args.engine == "both" else [args.engine]
    summaries = [run_engine(e, cases) for e in engines]
    for s in summaries:
        print_summary(s)
    print(f"\n报告已写入 {write_report(summaries)}")


if __name__ == "__main__":
    main()

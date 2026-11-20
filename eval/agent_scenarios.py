"""智能体多轮对话场景测试（需要真实大模型 Key，不进 CI）。

用法：python -m eval.agent_scenarios [--rounds 3]
每轮都在全新的临时数据库（含演示数据）里，按顺序模拟一位 HR 的连续对话，逐步检查数据库状态和回复。
大模型输出有随机性，建议跑多轮看通过率。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def run_once() -> list[tuple[str, bool, str]]:
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "scenario.db")
    import importlib

    from app import db as dbmod
    dbmod.db = dbmod.DB(os.environ["DB_PATH"])  # 每轮全新数据库
    for mod in ("app.pipeline", "app.agent", "app.analytics", "app.nlq", "app.report", "app.integrity"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from app import pipeline
    from app.config import now
    from app.db import db
    from app.seed import seed
    seed(db, "hi")
    for pos in ("后端开发", "产品经理"):  # 两个同名候选人，测试歧义反问
        cid = db.create_candidate({"name": "张伟", "position": pos, "stage": "初试", "status": "进行中"})
        db.add_event(cid, "create", {"name": [None, "张伟"]})

    tomorrow = (now() + timedelta(days=1)).strftime("%Y-%m-%d")
    after = (now() + timedelta(days=2)).strftime("%Y-%m-%d")
    z = lambda: (db.find_by_name("张三") or [{}])[0]  # noqa: E731
    results = []

    def step(name, text, check):
        m = pipeline.handle_message(text, operator="HR小王")
        try:
            ok, why = check(m)
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"检查异常：{e}"
        results.append((name, ok, f"{why}｜回复：{m['text'][:60]}"))

    step("建档", "张三 Java后端 BOSS来的，一面过了，明天下午3点二面，面试官陈晨",
         lambda m: (z().get("stage") == "复试" and z().get("interview_time") == f"{tomorrow} 15:00"
                    and z().get("interviewer") == "陈晨", f"{z().get('stage')} {z().get('interview_time')} {z().get('interviewer')}"))
    step("指代·改时间", "把他的二面改到后天上午10点",
         lambda m: (z().get("interview_time") == f"{after} 10:00", z().get("interview_time")))
    step("指代·换面试官", "他的面试官换成林峰",
         lambda m: (z().get("interviewer") == "林峰", z().get("interviewer")))
    step("追问一致", "他现在什么情况？", lambda m: ("林峰" in m["text"], "回复是否提到林峰"))
    step("同名反问不写入", "张伟一面过了",
         lambda m: (all(c["stage"] == "初试" for c in db.find_by_name("张伟")) and not m["payload"].get("pending_id"),
                    "两位张伟均未被修改"))
    step("一句多任务", "赵磊什么情况？顺便看下本周各岗位进复试的人数",
         lambda m: (any(s["tool"] == "query_data" for s in m["payload"].get("agent", {}).get("steps", []))
                    and "赵磊" in m["text"], "调用了统计 + 提到赵磊"))
    step("风险变更出卡片", "张三一面挂了",
         lambda m: (bool(m["payload"].get("pending_id")) and z().get("status") == "进行中", "生成待确认卡片且未直接写入"))
    step("确认后写入", "确认", lambda m: (z().get("status") == "已淘汰", z().get("status")))
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=1)
    args = ap.parse_args()
    from app.config import settings
    if not settings.llm_enabled:
        sys.exit("需要在 .env 中配置 LLM_API_KEY")
    tally: dict[str, int] = {}
    for r in range(args.rounds):
        print(f"\n===== 第 {r + 1} 轮（{settings.llm_model}）=====")
        for name, ok, why in run_once():
            tally[name] = tally.get(name, 0) + ok
            print(f"{'✅' if ok else '❌'} {name}：{why}")
    print("\n通过率：" + "，".join(f"{k} {v}/{args.rounds}" for k, v in tally.items()))
    total = sum(tally.values())
    print(f"合计 {total}/{len(tally) * args.rounds}")


if __name__ == "__main__":
    main()

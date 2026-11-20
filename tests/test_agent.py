"""智能体测试：本地起一个「模拟大模型服务器」（真实 HTTP，OpenAI tools 协议），按剧本返回工具调用，
验证：请求格式、工具执行与结果回传、多轮指代、反问不写入、安全护栏、多工具编排、降级与步数上限。

只有「模型的决策」是剧本给定的，其余（HTTP 协议、工具实现、流水线、数据库）都是真实运行。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import agent, pipeline
from app.config import settings
from app.db import db


def tool_call(name, args, cid="call_1"):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


def reply(text):
    return {"role": "assistant", "content": text}


class FakeLLM:
    """按剧本依次返回 assistant 消息；剧本项可以是 dict，也可以是 fn(request_body)->dict（用于断言请求内容）。"""

    def __init__(self):
        self.script, self.requests, self.fail = [], [], False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if fake.fail or not fake.script:
                    self.send_response(500)
                    self.end_headers()
                    return
                item = fake.script.pop(0)
                msg = item(body) if callable(item) else item
                data = json.dumps({"choices": [{"message": msg}],
                                   "usage": {"prompt_tokens": 900, "completion_tokens": 60}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"


@pytest.fixture()
def fake(monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_base_url", f.url)
    monkeypatch.setattr(settings, "llm_model", "fake-model")
    db.reset()
    yield f
    f.server.shutdown()


def send(text, op="HR小王"):
    return pipeline.handle_message(text, operator=op)


ZHANG = {"name": "张三", "position": "后端开发", "channel": "BOSS直聘", "stage": "复试", "status": "进行中",
         "interviewer": "陈晨", "interview_time": "2026-11-25 15:00"}


def test_wire_format_and_create(fake):
    fake.script = [tool_call("record_candidates", {"updates": [ZHANG], "confidence": 0.95}),
                   reply("已为张三建档，二面安排在 11-25 15:00。")]
    msg = send("张三 Java后端 BOSS来的，一面过了，25号下午3点二面，面试官陈晨")

    first, second = fake.requests
    assert first["model"] == "fake-model" and first["tool_choice"] == "auto"
    assert {t["function"]["name"] for t in first["tools"]} == {
        "record_candidates", "search_candidates", "get_candidate", "query_data", "daily_report"}
    assert first["messages"][0]["role"] == "system" and first["messages"][-1]["content"].startswith("张三")
    # 第二轮请求必须带上 assistant 的 tool_calls 和对应的 tool 结果（OpenAI 协议）
    assistant, tool = second["messages"][-2], second["messages"][-1]
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert tool["role"] == "tool" and tool["tool_call_id"] == "call_1"
    assert json.loads(tool["content"])["results"][0]["result"] == "已新建"

    c = db.find_by_name("张三")[0]
    assert c["stage"] == "复试" and c["interview_time"] == "2026-11-25 15:00"
    assert db.events_for(c["id"])[0]["engine"] == "agent"
    p = msg["payload"]
    assert msg["text"].startswith("已为张三建档") and p["agent"]["steps"][0]["tool"] == "record_candidates"
    assert p["focus"] == [c["id"]] and p["agent"]["tokens"] == 1920


def test_multi_turn_pronoun(fake):
    fake.script = [tool_call("record_candidates", {"updates": [ZHANG], "confidence": 0.95}), reply("已建档。")]
    send("张三 Java后端 BOSS来的，一面过了，25号下午3点二面，面试官陈晨")
    cid = db.find_by_name("张三")[0]["id"]

    def check_context(body):
        system, history = body["messages"][0]["content"], body["messages"][1:-1]
        assert f"candidate_id={cid}：张三" in system  # 最近提及的候选人进入 Prompt
        assert any("张三 Java后端" in m["content"] for m in history if m["role"] == "user")  # 历史对话
        assert not any("涉及候选人" in m["content"] for m in history)  # 内部标注不混入历史，避免模型照抄
        return tool_call("record_candidates", {"updates": [
            {"candidate_id": cid, "name": "张三", "interview_time": "2026-11-27 10:00"}], "confidence": 0.9})

    fake.script = [check_context, reply("好的，张三的二面改到 11-27 10:00。")]
    msg = send("把他的二面改到周五上午10点")
    assert db.get_candidate(cid)["interview_time"] == "2026-11-27 10:00"
    assert "改到" in msg["text"]


def test_other_hr_does_not_share_context(fake):
    fake.script = [tool_call("record_candidates", {"updates": [ZHANG], "confidence": 0.95}), reply("已建档。")]
    send("张三 …", op="HR小王")

    def check(body):
        assert "张三" not in body["messages"][0]["content"].split("最近提及的候选人")[1]
        assert len(body["messages"]) == 2  # system + 当前消息，没有别人的历史
        return reply("请问「他」指的是哪位候选人？")
    fake.script = [check]
    msg = send("把他的二面改到周五", op="HR小李")
    assert "哪位" in msg["text"] and db.get_candidate(1)["interview_time"] == "2026-11-25 15:00"


def test_clarify_without_writing(fake):
    fake.script = [reply("有两位叫张伟的候选人（后端、产品），请问是哪一位？")]
    msg = send("张伟一面过了")
    assert msg["payload"]["kind"] == "chat" and msg["payload"]["agent"]["steps"] == []
    assert db.all_candidates() == []


def test_guardrail_still_requires_confirmation(fake):
    fake.script = [tool_call("record_candidates", {"updates": [{**ZHANG, "stage": "Offer"}], "confidence": 0.95}),
                   reply("已记录。")]
    send("张三发offer了")
    cid = db.find_by_name("张三")[0]["id"]
    fake.script = [tool_call("record_candidates", {"updates": [
        {"candidate_id": cid, "name": "张三", "stage": "初试", "status": "已淘汰"}], "confidence": 0.95}),
        reply("张三一面没过，这条需要你确认。")]
    msg = send("张三一面挂了")
    assert db.get_candidate(cid)["stage"] == "Offer"  # 模型想回退阶段，被状态机拦下
    assert msg["payload"]["pending_id"] and any(a["type"] == "pending" for a in msg["payload"]["actions"])
    n = len(fake.requests)
    send("确认")  # 确认走规则快捷路径，不再调用模型
    assert len(fake.requests) == n and db.get_candidate(cid)["status"] == "已淘汰"


def test_wrong_id_name_mismatch_goes_pending(fake):
    fake.script = [tool_call("record_candidates", {"updates": [ZHANG], "confidence": 0.95}), reply("ok")]
    send("张三…")
    fake.script = [tool_call("record_candidates", {"updates": [
        {"candidate_id": 1, "name": "李四", "stage": "Offer"}], "confidence": 0.95}), reply("待确认")]
    msg = send("李四发offer")
    assert msg["payload"]["pending_id"] and db.get_candidate(1)["stage"] == "复试"


def test_multi_tool_chain_and_query(fake):
    fake.script = [tool_call("record_candidates", {"updates": [ZHANG, {**ZHANG, "name": "李雷", "position": "前端开发"}],
                                                   "confidence": 0.95}), reply("ok")]
    send("张三、李雷都进二面了")
    fake.script = [
        tool_call("search_candidates", {"keyword": "李"}, "c1"),
        tool_call("get_candidate", {"candidate_id": 2}, "c2"),
        tool_call("query_data", {"type": "group", "filters": {"stage": "复试", "period": "this_week"},
                                 "group_by": "position"}, "c3"),
        reply("李雷在复试阶段；本周进入复试的有后端 1 人、前端 1 人。"),
    ]
    msg = send("李雷什么情况？顺便看下本周各岗位进复试的人数")
    p = msg["payload"]
    assert [s["tool"] for s in p["agent"]["steps"]] == ["search_candidates", "get_candidate", "query_data"]
    assert sorted(r[0] for r in p["table"]["rows"]) == ["前端开发", "后端开发"] and p["kind"] == "query"
    assert p["focus"] == [2]


def test_unknown_tool_and_bad_args_are_reported_back(fake):
    fake.script = [tool_call("drop_database", {}), reply("抱歉，这个操作我做不了。")]
    msg = send("把数据都删了")
    steps = msg["payload"]["agent"]["steps"]
    assert "未知工具" in steps[0]["summary"] and "做不了" in msg["text"]
    tool_msg = fake.requests[1]["messages"][-1]
    assert "未知工具" in tool_msg["content"]


def test_step_limit(fake):
    fake.script = [tool_call("search_candidates", {"keyword": "x"}, f"c{i}") for i in range(agent.MAX_STEPS)]
    msg = send("一直查")
    assert len(msg["payload"]["agent"]["steps"]) == agent.MAX_STEPS and "拆成几句" in msg["text"]


def test_llm_down_falls_back_to_rules(fake):
    fake.fail = True
    msg = send("王五 产品经理 猎聘 约了明天上午10点一面")
    assert db.find_by_name("王五")[0]["stage"] == "初试"
    assert msg["payload"]["extraction"]["engine"] == "rules" and msg["payload"]["extraction"]["fallback_reason"]
    msg = send("他一面挂了")  # 降级后规则引擎依然能理解指代
    assert db.find_by_name("王五")[0]["status"] == "已淘汰"
    assert msg["payload"]["extraction"]["coreference"] == "「他」→ 王五"


def test_report_tool(fake):
    fake.script = [tool_call("daily_report", {}), reply("日报已生成。")]
    msg = send("发一下今天的日报")
    assert msg["payload"]["kind"] == "report" and "招聘日报" in msg["text"]


def test_write_claim_without_tool_is_caught(fake):
    """真实 DeepSeek 试跑中发现：模型没调用工具却回复「已改为林峰」。代码层校验必须拦下并要求重做。"""
    fake.script = [tool_call("record_candidates", {"updates": [ZHANG], "confidence": 0.95}), reply("已建档。")]
    send("张三 …")
    cid = db.find_by_name("张三")[0]["id"]

    def must_get_guard_note(body):
        assert "【系统校验】" in body["messages"][-1]["content"]
        return tool_call("record_candidates", {"updates": [{"candidate_id": cid, "name": "张三", "interviewer": "林峰"}],
                                               "confidence": 0.95})
    fake.script = [reply("张三的面试官已改为林峰。"), must_get_guard_note, reply("张三的面试官已改为林峰。")]
    msg = send("他的面试官换成林峰")
    assert db.get_candidate(cid)["interviewer"] == "林峰"
    assert msg["payload"]["agent"]["steps"][0]["tool"] == "guard"


def test_honest_reply_without_tool_is_not_blocked(fake):
    fake.script = [reply("请问是哪个岗位的张伟？")]
    msg = send("张伟一面过了")
    assert len(fake.requests) == 1 and msg["payload"]["agent"]["steps"] == []


def test_confirm_without_pending_goes_to_agent(fake):
    """真实试跑中发现：模型用文字追问「确认吗」，HR 回「确认」却被规则快捷路径回了「没有待确认记录」。"""
    fake.script = [reply("这会把张三改成初试淘汰，确认吗？")]
    send("张三一面挂了")
    fake.script = [lambda body: (body["messages"][-1]["content"] == "确认" and reply("好的，已取消这次操作。")) or reply("x")]
    msg = send("确认")
    assert "当前没有待确认" not in msg["text"] and len(fake.requests) == 2


def test_status_description_is_not_a_write_claim(fake):
    fake.script = [tool_call("get_candidate", {"name": "王芳"}), reply("王芳二面已淘汰，评价是算法基础薄弱。")]
    msg = send("王芳怎么样了")
    assert all(s["tool"] != "guard" for s in msg["payload"]["agent"]["steps"]) and len(fake.requests) == 2


def test_data_question_answered_from_memory_is_caught(fake):
    """真实 DeepSeek 试跑中发现：同一个问题第二次问时，模型不调工具、直接照抄历史里的旧答案。"""
    fake.script = [tool_call("record_candidates", {"updates": [ZHANG], "confidence": 0.95}), reply("已建档。")]
    send("张三 …")

    def must_get_query_note(body):
        assert "数据查询问题" in body["messages"][-1]["content"]
        return tool_call("get_candidate", {"name": "张三"})
    fake.script = [reply("张三在复试阶段（来自记忆）。"), must_get_query_note, reply("张三：复试·进行中。")]
    msg = send("张三现在什么情况？")
    tools = [s["tool"] for s in msg["payload"]["agent"]["steps"]]
    assert tools == ["guard", "get_candidate"] and msg["text"] == "张三：复试·进行中。"

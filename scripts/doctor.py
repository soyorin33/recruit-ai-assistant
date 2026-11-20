"""接入自检：逐项检查 .env 里配置的大模型、企业微信应用、回调加解密、智能表格、群机器人是否真的可用。

用法（在项目根目录）：
    python -m scripts.doctor                  # 只读检查，不会发消息、不会写表格
    python -m scripts.doctor --to zhangsan    # 额外给企微成员 zhangsan 发一条测试应用消息
    python -m scripts.doctor --push           # 额外往群机器人推送一条测试消息
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

OK, FAIL, SKIP = "✅", "❌", "⏭️ "
ERR_HINTS = {
    40001: "Secret 不正确，请核对应用的 Secret（不是企业 ID）",
    40013: "CorpID 不正确",
    40014: "access_token 无效",
    41001: "缺少 access_token",
    48002: "API 接口无权限：在应用设置中开启对应权限（如文档权限）",
    60020: "IP 不在企业可信 IP 列表：应用详情 → 企业可信 IP 中加入本机公网 IP（需先配置可信域名或接收消息 URL）",
    640014: "无权限访问该文档：请把应用添加为智能表格的协作者或检查 docid",
}
results: list[tuple[str, str, str]] = []


def report(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    print(f"{status} {name}" + (f"：{detail}" if detail else ""))


def hint(err: Exception) -> str:
    text = str(err)
    for code, h in ERR_HINTS.items():
        if f"'errcode': {code}" in text or f'"errcode": {code}' in text:
            return f"{text[:160]}\n     👉 {h}"
    return text[:200]


def check_llm() -> None:
    if not settings.llm_enabled:
        return report("大模型", SKIP, "未配置 LLM_API_KEY（将使用规则引擎）")
    from app import extractor
    try:
        r = extractor.llm_extract_message("张三 Java后端 BOSS来的，一面过了，明天下午3点二面，面试官陈晨", [])
        m = r.get("llm_meta", {})
        c = (r.get("candidates") or [{}])[0]
        ok = r["intent"] == "update" and c.get("name") == "张三" and c.get("stage") == "复试"
        report(f"大模型 {settings.llm_model}", OK if ok else FAIL,
               f"{m.get('latency_ms')}ms，{(m.get('prompt_tokens') or 0) + (m.get('completion_tokens') or 0)} tokens，"
               f"抽取结果 {json.dumps(c, ensure_ascii=False)}")
    except Exception as e:
        report("大模型", FAIL, str(e)[:200])


def check_wecom(to_user: str | None) -> None:
    if not (settings.wecom_corp_id and settings.wecom_secret):
        return report("企微应用 access_token", SKIP, "未配置 WECOM_CORP_ID / WECOM_SECRET")
    from app.wecom import api
    try:
        token = api.token_cache.get()
        report("企微应用 access_token", OK, f"获取成功（{token[:6]}…）")
    except Exception as e:
        return report("企微应用 access_token", FAIL, hint(e))
    if to_user:
        try:
            api.send_text(to_user, "【招聘助手】接入自检：如果你看到这条消息，说明应用消息发送正常 ✅")
            report(f"应用消息发送 → {to_user}", OK)
        except Exception as e:
            report(f"应用消息发送 → {to_user}", FAIL, hint(e))


def check_callback() -> None:
    if not (settings.wecom_token and settings.wecom_aes_key):
        return report("回调加解密配置", SKIP, "未配置 WECOM_TOKEN / WECOM_AES_KEY")
    from app.wecom.crypto import WXBizMsgCrypt
    try:
        c = WXBizMsgCrypt(settings.wecom_token, settings.wecom_aes_key, settings.wecom_corp_id)
        assert c.decrypt(c.encrypt("ping")) == "ping"
        report("回调加解密配置", OK, "EncodingAESKey 有效；企微后台「接收消息」URL 填 https://<你的域名>/wecom/callback")
    except Exception as e:
        report("回调加解密配置", FAIL, str(e))


def check_smartsheet() -> None:
    if not settings.smartsheet_enabled:
        return report("智能表格", SKIP, "未配置 SMARTSHEET_DOCID / SMARTSHEET_SHEET_ID")
    from app.wecom import api
    try:
        fields = api.smartsheet_get_fields()
        titles = {f.get("field_title") for f in fields}
        missing = [t for t in api.SMARTSHEET_COLUMNS.values() if t not in titles]
        if missing:
            report("智能表格字段", FAIL, f"缺少列：{'、'.join(missing)}（请按此列名新建，类型选「文本」）")
        else:
            report("智能表格字段", OK, f"{len(fields)} 列，所需列齐全")
    except Exception as e:
        report("智能表格", FAIL, hint(e))


def check_webhook(push: bool) -> None:
    if not settings.webhook_enabled:
        return report("群机器人 Webhook", SKIP, "未配置 WECOM_WEBHOOK_URL")
    if not push:
        return report("群机器人 Webhook", SKIP, "已配置；加 --push 发送一条测试消息")
    from app.wecom import api
    try:
        api.webhook_markdown("**【招聘助手】接入自检**\n> 群机器人推送正常 ✅ 之后的日报与跟进提醒会发到这里")
        report("群机器人 Webhook", OK, "测试消息已推送，请到群里查看")
    except Exception as e:
        report("群机器人 Webhook", FAIL, str(e)[:200])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", help="企微成员 UserID，发送一条测试应用消息")
    ap.add_argument("--push", action="store_true", help="往群机器人推送一条测试消息")
    args = ap.parse_args()
    print("== 招聘助手 · 接入自检 ==\n")
    check_llm()
    check_wecom(args.to)
    check_callback()
    check_smartsheet()
    check_webhook(args.push)
    fails = [r for r in results if r[1] == FAIL]
    print(f"\n共 {len(results)} 项：{sum(r[1] == OK for r in results)} 通过，{len(fails)} 失败，"
          f"{sum(r[1] == SKIP for r in results)} 跳过")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

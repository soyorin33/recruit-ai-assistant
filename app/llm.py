"""极简 OpenAI 兼容客户端：DeepSeek / 腾讯混元 / 通义千问 / Kimi 等只需改 base_url + model。"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

from .config import settings

log = logging.getLogger("llm")


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    """兼容模型偶尔包裹 ```json 代码块或前后带说明文字的情况。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"模型未返回 JSON：{text[:200]}")
    return json.loads(text[start:end + 1])


def chat(messages: list[dict], *, json_mode: bool = False, temperature: float = 0.1,
         max_tokens: int = 1200) -> tuple[str, dict]:
    """返回 (内容, 用量信息)。"""
    if not settings.llm_enabled:
        raise LLMError("未配置 LLM_API_KEY")
    body: dict = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    t0 = time.time()
    try:
        resp = httpx.post(
            f"{settings.llm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json=body,
            timeout=settings.llm_timeout,
        )
        if resp.status_code == 400 and json_mode:
            # 个别模型不支持 response_format，去掉后重试一次
            body.pop("response_format", None)
            resp = httpx.post(
                f"{settings.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json=body,
                timeout=settings.llm_timeout,
            )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise LLMError(f"调用大模型失败：{e}") from e
    content = data["choices"][0]["message"]["content"] or ""
    usage = data.get("usage") or {}
    meta = {
        "model": settings.llm_model,
        "latency_ms": int((time.time() - t0) * 1000),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }
    return content, meta


def chat_tools(messages: list[dict], tools: list[dict], *, temperature: float = 0.1,
               max_tokens: int = 1200) -> tuple[dict, dict]:
    """Function Calling：返回 (assistant 消息 dict，可能包含 tool_calls, 用量信息)。

    使用 OpenAI 标准 tools 协议，DeepSeek / 通义 / 混元 / Kimi 的兼容接口均支持。
    """
    if not settings.llm_enabled:
        raise LLMError("未配置 LLM_API_KEY")
    body = {"model": settings.llm_model, "messages": messages, "tools": tools, "tool_choice": "auto",
            "temperature": temperature, "max_tokens": max_tokens}
    t0 = time.time()
    try:
        resp = httpx.post(f"{settings.llm_base_url}/chat/completions",
                          headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                          json=body, timeout=settings.llm_timeout)
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        raise LLMError(f"调用大模型失败：{e}") from e
    usage = data.get("usage") or {}
    meta = {"model": settings.llm_model, "latency_ms": int((time.time() - t0) * 1000),
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")}
    return {"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []}, meta


def chat_json(system: str, user: str, **kw) -> tuple[dict, dict]:
    content, meta = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        json_mode=True, **kw,
    )
    try:
        return _extract_json(content), meta
    except (json.JSONDecodeError, LLMError) as e:
        raise LLMError(f"解析模型 JSON 失败：{e}") from e

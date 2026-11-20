"""截图识别：简历截图 / 招聘软件聊天截图 → 候选人信息（多模态大模型，OpenAI 兼容 image_url 协议）。

HR 日常最常转发的不是文字，而是截图。识别结果与文字消息走同一条流水线（去重、状态机、人工确认）。
推荐模型：通义 qwen-vl-plus / qwen-vl-max、混元 hunyuan-vision 等；DeepSeek 对话模型不支持图片。
"""
from __future__ import annotations

import base64

from . import llm
from .config import settings
from .domain import CHANNELS, POSITIONS

PROMPT = f"""你是招聘助手的「截图识别引擎」。判断图片类型并提取候选人信息，只输出 JSON：
{{"type": "resume|chat|other",
  "candidates": [{{"name": 姓名, "phone": 手机号, "email": 邮箱, "position": 应聘岗位（尽量归一到 {"、".join(POSITIONS)}）,
                  "channel": 招聘渠道（可根据界面判断，尽量归一到 {"、".join(CHANNELS)}）,
                  "education": 最高学历及学校, "years": 工作年限数字, "skills": [核心技能≤6],
                  "summary": "一句话画像≤60字", "feedback": "聊天截图中的关键沟通结论≤40字"}}],
  "text": "图片中与候选人相关文字的转写（≤1500字，用于后续岗位匹配）",
  "confidence": 0-1}}
规则：
- resume：简历或简历截图；chat：招聘软件 / 微信中与候选人的聊天截图；other：与候选人无关。
- 只提取图片中真实可见的信息，看不清或没有的字段填 null，禁止编造；姓名不确定时 confidence 给低分。"""


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def recognize(data: bytes, mime: str | None = None) -> dict:
    """调用视觉模型识别图片。未配置视觉模型时抛出 LLMError。"""
    if not settings.vision_enabled:
        raise llm.LLMError("未配置视觉模型（VISION_MODEL）")
    mime = mime or sniff_mime(data) or "image/png"
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    content, meta = llm.chat(
        [{"role": "system", "content": PROMPT},
         {"role": "user", "content": [{"type": "text", "text": "请识别这张图片中的候选人信息。"},
                                      {"type": "image_url", "image_url": {"url": url}}]}],
        model=settings.vision_model, base_url=settings.vision_base_url or None,
        api_key=settings.vision_api_key or None, max_tokens=1500,
    )
    data_out = llm._extract_json(content)
    data_out["llm_meta"] = meta
    if data_out.get("type") not in ("resume", "chat", "other"):
        data_out["type"] = "other"
    return data_out

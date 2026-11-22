"""全局配置：全部从环境变量 / .env 读取，未配置的能力自动降级。"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Shanghai"))


def now() -> datetime:
    """统一使用北京时间（naive datetime，存为文本，两种数据库下比较行为一致）。"""
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def parse_user_names(raw: str) -> dict[str, str]:
    """「ZhouJiaXing=周家兴,lisi=李四」→ {账号: 中文名}，兼容中文逗号和多余空格。"""
    pairs = (item.partition("=") for item in raw.replace("，", ",").split(","))
    return {k.strip(): v.strip() for k, _, v in pairs if k.strip() and v.strip()}


class Settings:
    # ---- 大模型（任意 OpenAI 兼容接口：DeepSeek / 混元 / 通义 / Kimi ...）----
    llm_api_key = _env("LLM_API_KEY")
    llm_base_url = _env("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    llm_model = _env("LLM_MODEL", "deepseek-chat")
    llm_timeout = float(_env("LLM_TIMEOUT", "30"))
    # 视觉模型（截图识别）：默认复用上面的 Key 和地址，只需填模型名，如 qwen-vl-plus
    vision_model = _env("VISION_MODEL")
    vision_base_url = _env("VISION_BASE_URL").rstrip("/")
    vision_api_key = _env("VISION_API_KEY")

    # ---- 企业微信自建应用（接收消息 + 回复 + 智能表格 API）----
    wecom_corp_id = _env("WECOM_CORP_ID")
    wecom_agent_id = _env("WECOM_AGENT_ID")
    wecom_secret = _env("WECOM_SECRET")
    wecom_token = _env("WECOM_TOKEN")
    wecom_aes_key = _env("WECOM_AES_KEY")
    # 企微 API 地址（测试时可指向本地模拟服务器）
    wecom_api_base = _env("WECOM_API_BASE", "https://qyapi.weixin.qq.com/cgi-bin").rstrip("/")

    # ---- 企微「智能机器人」API 模式（群里 @ 机器人）----
    wecom_aibot_token = _env("WECOM_AIBOT_TOKEN")
    wecom_aibot_aes_key = _env("WECOM_AIBOT_AES_KEY")
    # 长连接模式（推荐）：只需 Bot ID + Secret，不需要公网域名 / HTTPS / 备案
    wecom_aibot_id = _env("WECOM_AIBOT_ID")
    wecom_aibot_secret = _env("WECOM_AIBOT_SECRET")
    wecom_aibot_ws_url = _env("WECOM_AIBOT_WS_URL", "wss://openws.work.weixin.qq.com")

    # ---- 企微账号 → 中文名（「负责HR」、消息发送人显示用）----
    # 格式：WECOM_USER_NAMES=ZhouJiaXing=周家兴,lisi=李四（企微后台「通讯录」里成员的「账号」一栏）
    wecom_user_names = parse_user_names(_env("WECOM_USER_NAMES"))

    # ---- 企微群机器人 Webhook（推送日报 / 预警）----
    wecom_webhook_url = _env("WECOM_WEBHOOK_URL")

    # ---- 企微 / 腾讯文档 智能表格 ----
    smartsheet_docid = _env("SMARTSHEET_DOCID")
    smartsheet_sheet_id = _env("SMARTSHEET_SHEET_ID")

    # ---- 腾讯文档（docs.qq.com）官方 MCP：候选人总表实时写入腾讯文档智能表格 ----
    tdocs_token = _env("TDOCS_TOKEN")  # 在 https://docs.qq.com/open/auth/mcp.html 生成的个人 Token
    tdocs_mcp_url = _env("TDOCS_MCP_URL", "https://docs.qq.com/openapi/mcp")
    tdocs_file_id = _env("TDOCS_FILE_ID")  # 由 python -m scripts.tdocs_setup 自动创建并写入 .env
    tdocs_sheet_id = _env("TDOCS_SHEET_ID")
    tdocs_url = _env("TDOCS_URL")

    # ---- 业务参数 ----
    # 数据库：设置 DATABASE_URL（mysql://用户:密码@主机:3306/库名）用 MySQL；不设置则用 SQLite 文件 DB_PATH
    database_url = _env("DATABASE_URL")
    db_path = _env("DB_PATH", str(BASE_DIR / "data" / "recruit.db"))
    confidence_threshold = float(_env("CONFIDENCE_THRESHOLD", "0.8"))
    stale_days = int(_env("STALE_DAYS", "3"))
    report_time = _env("REPORT_TIME", "18:00")  # 每日日报推送时间
    alert_time = _env("ALERT_TIME", "10:00")  # 每日超时预警推送时间
    seed_demo_data = _env("SEED_DEMO_DATA", "1") == "1"

    # 规则与大模型交叉校验：两者对阶段 / 状态 / 面试时间的判断冲突时转人工确认
    cross_check = _env("CROSS_CHECK", "1") == "1"

    # ---- 访问控制 ----
    # 设置后，看板与所有 /api 接口都需要先输入口令（部署到公网时必须设置）
    access_token = _env("ACCESS_TOKEN")
    session_hours = int(_env("SESSION_HOURS", "72"))

    @property
    def demo_mode(self) -> bool:
        """演示模式：允许「重置演示数据」。正式使用（SEED_DEMO_DATA=0）时关闭。"""
        return self.seed_demo_data

    @property
    def auth_enabled(self) -> bool:
        return bool(self.access_token)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def vision_enabled(self) -> bool:
        return bool(self.vision_model and (self.vision_api_key or self.llm_api_key))

    @property
    def wecom_app_enabled(self) -> bool:
        return all([self.wecom_corp_id, self.wecom_secret, self.wecom_token, self.wecom_aes_key])

    @property
    def aibot_enabled(self) -> bool:
        return bool(self.wecom_aibot_token and self.wecom_aibot_aes_key)

    @property
    def aibot_ws_enabled(self) -> bool:
        return bool(self.wecom_aibot_id and self.wecom_aibot_secret)

    @property
    def tdocs_enabled(self) -> bool:
        return bool(self.tdocs_token and self.tdocs_file_id and self.tdocs_sheet_id)

    @property
    def smartsheet_enabled(self) -> bool:
        return bool(self.wecom_corp_id and self.wecom_secret and self.smartsheet_docid and self.smartsheet_sheet_id)

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.wecom_webhook_url)


settings = Settings()


def display_name(userid: str | None) -> str:
    """企微账号 ID → 中文名（未配置时原样返回）。"""
    if not userid:
        return ""
    return settings.wecom_user_names.get(userid, userid)

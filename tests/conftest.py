import os
import tempfile

# 必须在导入 app 之前设置：测试使用独立的临时数据库，且不调用真实大模型
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["SEED_DEMO_DATA"] = "0"

# 本机 .env 里可能有真实的企微 / 腾讯文档 / 群机器人配置：测试绝不能连到真实服务
# （否则会挤掉正在运行的服务的机器人长连接、往真实表格里写测试数据、往真实群里推消息）。
# load_dotenv 不会覆盖已存在的环境变量，所以这里把 .env.example 里列出的所有外部接入项都置空。
_EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.example")
with open(_EXAMPLE, encoding="utf-8") as _f:
    for _line in _f:
        _key = _line.split("=", 1)[0].strip()
        if ("=" in _line and not _line.lstrip().startswith("#") and _key not in os.environ
                and _key.startswith(("WECOM_", "TDOCS_", "SMARTSHEET_", "VISION_", "ACCESS_TOKEN"))):
            os.environ[_key] = ""
for _key in ("WECOM_API_BASE", "WECOM_AIBOT_WS_URL", "TDOCS_MCP_URL"):  # 有默认地址的项交给代码默认值
    if os.environ.get(_key) == "":
        del os.environ[_key]

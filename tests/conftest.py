import os
import tempfile

# 必须在导入 app 之前设置：测试使用独立的临时数据库，且不调用真实大模型
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

# 设置 TEST_DATABASE_URL=mysql://…/xxx_test 时，整套测试改用 MySQL 跑（CI 里 SQLite、MySQL 各跑一遍）。
# 为防止误删真实数据，库名必须以 _test 结尾；开始前清空该库的所有表。
_TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
if _TEST_DB:
    from urllib.parse import unquote, urlparse

    import pymysql

    # 注意：这里不能 import app.db —— 它在导入时就会按当前配置建立连接
    _u = urlparse(_TEST_DB)
    _cfg = {"host": _u.hostname or "127.0.0.1", "port": _u.port or 3306, "user": unquote(_u.username or "root"),
            "password": unquote(_u.password or ""), "database": _u.path.lstrip("/")}
    if not _cfg["database"].endswith("_test"):
        raise SystemExit(f"TEST_DATABASE_URL 的库名必须以 _test 结尾（当前：{_cfg['database']}），以免清空真实数据")
    _c = pymysql.connect(**_cfg, autocommit=True)
    with _c.cursor() as _cur:
        _cur.execute("SHOW TABLES")
        for (_t,) in _cur.fetchall():
            _cur.execute(f"DROP TABLE `{_t}`")
    _c.close()
    os.environ["DATABASE_URL"] = _TEST_DB
else:
    os.environ["DATABASE_URL"] = ""  # 本机 .env 里的 MySQL 配置不能被测试读到
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

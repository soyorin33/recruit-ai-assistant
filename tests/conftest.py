import os
import tempfile

# 必须在导入 app 之前设置：测试使用独立的临时数据库，且不调用真实大模型
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["SEED_DEMO_DATA"] = "0"

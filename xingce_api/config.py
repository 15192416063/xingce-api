# -*- coding: utf-8 -*-
"""集中配置。优先读环境变量,其次读同目录 .env(免装 python-dotenv)。"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """极简 .env 加载:KEY=VALUE 每行一条,不覆盖已存在的真实环境变量。"""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# ---- 存储 ----
DB_URL = os.getenv("XC_DB_URL", f"sqlite:///{os.path.join(BASE_DIR, 'xingce.db')}")
# sqlite 相对路径统一锚定到本目录,避免从不同 cwd 启动时连错库/建空库
if DB_URL.startswith("sqlite:///") and not os.path.isabs(DB_URL[10:]):
    DB_URL = "sqlite:///" + os.path.join(BASE_DIR, DB_URL[10:])
PDF_DIR = os.path.join(BASE_DIR, "data", "pdf")        # 原始PDF
IMAGE_DIR = os.path.join(BASE_DIR, "data", "images")   # 抠出的图
VECTOR_DIR = os.path.join(BASE_DIR, "vector_db")       # Chroma 向量库

# ---- AI ----
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
LLM_MODEL = "deepseek-chat"

# ---- 向量 embedding(改用 API,不再本地跑模型,省内存便于上云)----
# 默认硅基流动(siliconflow.cn):OpenAI兼容、有免费额度、托管 bge 模型。
# 也可换通义/智谱等任意 OpenAI 兼容的 embedding 服务。
EMBED_BASE_URL = os.getenv("XC_EMBED_BASE_URL", "https://api.siliconflow.cn/v1")
EMBED_KEY = os.getenv("XC_EMBED_KEY", "")
EMBED_MODEL = os.getenv("XC_EMBED_MODEL", "BAAI/bge-large-zh-v1.5")

# ---- 业务 ----
ADMIN_TOKEN = os.getenv("XC_ADMIN_TOKEN", "admin123")  # 兼容旧接口(已弃用)
CATEGORIES_L1 = ["言语理解", "数量关系", "判断推理", "资料分析", "常识判断", "政治理论"]
# 考试倒计时(可改成你的目标考试)
EXAM_NAME = os.getenv("XC_EXAM_NAME", "2027国家公务员考试")
EXAM_DATE = os.getenv("XC_EXAM_DATE", "2026-11-29")  # YYYY-MM-DD

# ---- 安全 ----
# 令牌签名密钥:上线务必用环境变量设成随机长串,否则令牌可被伪造!
SECRET_KEY = os.getenv("XC_SECRET_KEY", "")
SECRET_IS_DEFAULT = not bool(SECRET_KEY)
if not SECRET_KEY:
    import secrets as _secrets
    SECRET_KEY = _secrets.token_hex(32)  # 临时随机(重启即失效,仅开发用)
TOKEN_TTL_DAYS = 30
# 首位注册者自动成为管理员;也可用此口令注册管理员
ADMIN_SIGNUP_CODE = os.getenv("XC_ADMIN_CODE", "make-me-admin")

# ---- 免费系统 · 成本控制 ----
# 每人每日 AI 调用硬上限(防滥用/焊死成本,管理员不限)
AI_DAILY_CAP = int(os.getenv("XC_AI_DAILY_CAP", "30"))
# 私有题库容量(全员统一;一份真题卷约130题,够十几套)
MINE_CAP = int(os.getenv("XC_MINE_CAP", "2000"))
# 用户上传 PDF 大小上限(MB)与每日上传份数上限
MINE_PDF_MAX_MB = int(os.getenv("XC_MINE_PDF_MAX_MB", "30"))
MINE_PDF_DAILY = int(os.getenv("XC_MINE_PDF_DAILY", "3"))
# 每日刷题目标(留存:首页进度条)
DAILY_GOAL = int(os.getenv("XC_DAILY_GOAL", "20"))
# DeepSeek 价格(¥/百万token),管理员成本面板估算用,价格变了改这里
PRICE_IN_PER_M = float(os.getenv("XC_PRICE_IN", "2"))
PRICE_OUT_PER_M = float(os.getenv("XC_PRICE_OUT", "8"))

for d in (PDF_DIR, IMAGE_DIR, VECTOR_DIR):
    os.makedirs(d, exist_ok=True)

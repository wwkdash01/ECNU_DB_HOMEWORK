import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------- 项目级环境变量（.env）----------
# 在 config 里加载：任何 `import config` 的脚本都自动生效，不必改每个入口。
# override=False ⇒ 系统环境变量优先；未导出时才用 .env，两者都不存在则报错。
def _load_env():
    try:
        from dotenv import load_dotenv
        loaded = load_dotenv(ROOT / ".env", override=False)
        return "dotenv" if loaded else "none"
    except ImportError:
        pass
    # 退化路径：不依赖 python-dotenv，手工解析足够简单
    f = ROOT / ".env"
    if not f.exists():
        return "none"
    for raw in f.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            os.environ.setdefault(k, v)
    return "manual"

ENV_SOURCE = _load_env()

# ---------- 路径 ----------
DATA_DIR       = ROOT / "data"
DB_DIR         = DATA_DIR / "dev_databases"
QUESTIONS_FILE = DATA_DIR / "mini_dev_sqlite.json"
GOLD_FILE      = DATA_DIR / "mini_dev_sqlite_gold.sql"  
RESULTS_DIR    = ROOT / "results"

# ---------- 模型 ----------
API_KEY_ENV = "DEEPSEEK_API_KEY"
BASE_URL    = "https://api.deepseek.com"
MODEL       = "deepseek-flash"   # 当前唯一 Flash API ID（V4.1-Flash）。旧名 deepseek-v4-flash 已退役
MODEL_VERSION = "DeepSeek-V4.1-Flash"   # 写进 env.json 供报告版本溯源
TEMPERATURE = 0.0
MAX_TOKENS  = 393216   

# ---------- 关闭思考模式 ----------
THINKING = {"type": "disabled"}

def require_api_key():
    """取密钥；缺失时给出可操作的报错，而不是裸 KeyError"""
    k = os.environ.get(API_KEY_ENV, "").strip()
    if not k:
        raise RuntimeError(
            f"{API_KEY_ENV} 未设置。请在 {ROOT / '.env'} 写入 "
            f"{API_KEY_ENV}=sk-xxx，或 `export {API_KEY_ENV}=sk-xxx`"
        )
    return k

# ---------- 实验参数（冻结，别中途改）----------
MAX_STEPS     = 3    # agent 轮数【上限】，成功即停，不是固定跑 3 轮
K_CANDIDATES  = 3    # O3 采样数，与 MAX_STEPS 对齐
SMOKE_N       = 20   # 冒烟测试题数（取前 N 条，固定，不要随机）

# ---------- 执行沙箱 ----------
RESULT_ROW_LIMIT      = 100    # run_sql 最多返回行数
QUERY_TIMEOUT_SEC     = 5      # 单条 SQL 执行超时
TOOL_OUTPUT_MAX_CHARS = 2000   # 工具返回值截断长度
VALUE_SAMPLE_LIMIT    = 20     # get_column_values 取样条数

# ---------- 重试与并发 ----------
API_MAX_RETRIES  = 3
API_BACKOFF_BASE = 2           # 秒
CONCURRENCY      = 5           # 阶段 3 实测吞吐后再定
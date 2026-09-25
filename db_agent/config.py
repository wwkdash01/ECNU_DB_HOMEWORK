import os
from pathlib import Path

ROOT = Path(os.environ.get("DB_AGENT_ROOT")
            or Path(__file__).resolve().parent.parent).resolve()

# ---------- 项目级环境变量（.env）----------
def _load_env():
    try:
        from dotenv import load_dotenv
        loaded = load_dotenv(ROOT / ".env", override=False)
        return "dotenv" if loaded else "none"
    except ImportError:
        pass
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
EVAL_DIR       = ROOT / "reference" / "evaluation"

# ---------- 模型 ----------
API_KEY_ENV = "DEEPSEEK_API_KEY"
BASE_URL    = "https://api.deepseek.com"
MODEL       = "deepseek-flash"
MODEL_VERSION = "DeepSeek-V4.1-Flash"
TEMPERATURE = 0.0

TEMPERATURE_SAMPLING = 0.7
MAX_TOKENS  = 2048

REQUEST_TIMEOUT = 120

# ---------- 关闭思考模式 ----------
THINKING = {"type": "disabled"}

def require_api_key():
    k = os.environ.get(API_KEY_ENV, "").strip()
    if not k:
        raise RuntimeError(
            f"{API_KEY_ENV} 未设置。请在 {ROOT / '.env'} 写入 "
            f"{API_KEY_ENV}=sk-xxx，或 `export {API_KEY_ENV}=sk-xxx`"
        )
    return k

# ---------- 实验参数 ----------
MAX_STEPS     = 10
K_CANDIDATES  = 3
SMOKE_N       = 20

# ---------- 执行沙箱 ----------
RESULT_ROW_LIMIT      = 100
QUERY_TIMEOUT_SEC     = 5
TOOL_OUTPUT_MAX_CHARS = 2000

# ---------- 重试与并发 ----------
API_MAX_RETRIES  = 3
API_BACKOFF_BASE = 2
CONCURRENCY      = 5

# ---------- agent 末轮兜底 ----------
FALLBACK_TO_LAST_EXECUTED = True
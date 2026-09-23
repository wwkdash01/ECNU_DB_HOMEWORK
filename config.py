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
EVAL_DIR       = ROOT / "reference" / "evaluation"      # 官方评测脚本（score.py 用）

# ---------- 模型 ----------
API_KEY_ENV = "DEEPSEEK_API_KEY"
BASE_URL    = "https://api.deepseek.com"
MODEL       = "deepseek-flash"   # 当前唯一 Flash API ID（V4.1-Flash）。旧名 deepseek-v4-flash 已退役
MODEL_VERSION = "DeepSeek-V4.1-Flash"   # 写进 env.json 供报告版本溯源
TEMPERATURE = 0.0

# O3 专用。T=0 时同一 prompt 采样三次会得到三条相同 SQL，投票无意义、
# O3 退化成 O1 —— 这是 O3 机制的内在要求，报告里必须写明。
TEMPERATURE_SAMPLING = 0.7
# 输出上限。模型实际每条 SQL 仅几十~几百 token；设成模型上限无意义，
# 且一旦模型跑飞会按输出价烧钱。2048 足够容纳 SQL + 少量说明文字。
MAX_TOKENS  = 2048

# ---------- A9 专用：原生思考模式 ----------
# A9 = A1 的【逐字同一 prompt、逐字同一工具】，唯一自变量是 thinking 开关。
# 老组（O1/O3/A1~A8）一律不受影响：MAX_TOKENS 仍是 2048、temperature 仍生效，
# 因此历史结果与今后的对照重跑口径完全一致。
THINKING_A9        = {"type": "enabled"}
REASONING_EFFORT   = "max"      # 官方映射：max -> max（最高档）。low/high/max 三档

# 思考模式下【推理 token 与正式输出共享 max_tokens】，而推理长度不可预知。
# 若沿用 2048，模型常在写完 SQL 前被截断（finish_reason='length'），
# 表现为"tool_call 参数为空"——会被误读成"模型不会用工具"。故 A9 放开到模型上限。
# 393216 是实测上限：发 400000 时 API 报
#   "the valid range of max_tokens is [1, 393216]"
# （注意：官方价格页把 MAX OUTPUT 写成 "384K"，与 API 实际接受的 393216 不一致；
#  以 API 报错信息为准，别照抄价格页。）
MAX_TOKENS_A9      = 393216

# 单次 API 请求的网络超时（秒），防止请求悬挂拖死整批
REQUEST_TIMEOUT = 120

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
# ⚠️ 本仓库实测：手册的 3 轮对本模型不够 —— submit_answer 启用后 11/11 次提交
#    全部发生在第 3 轮，且 9/20 从未提交（累计 run_sql 达 3~8 次）。
#    故 3 -> 10：它现在role是"安全护栏"而非"实验变量"，因为模型提交答案即停。
#    cost trade-off 改由 EX@k 曲线离线分析（见 README / 报告）。
MAX_STEPS     = 10
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

# ---------- agent 末轮兜底 ----------
# 实测：模型即使末轮被禁用工具，仍会把「工具调用语法」当纯文本输出，例如
#   SELECT ... GROUP BY c.Segment</*DSML*/ parameter></*DSML*/ invoke>
#   <*DSML*/ invoke name="run_sql"><*DSML*/ parameter name="sql" string="true">SELECT ...
# 即它把两次 run_sql 调用连文本一起吐出来，多段 SQL 连成一条 -> 语法错。
# 这些 SQL 当场执行过且成功，因此末轮答案执行失败时回退到「最后一条真的执行成功过的 SQL」。
FALLBACK_TO_LAST_EXECUTED = True
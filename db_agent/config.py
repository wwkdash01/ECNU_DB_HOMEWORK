import os
from pathlib import Path

ROOT = Path(os.environ.get("DB_AGENT_ROOT")
            or Path(__file__).resolve().parent.parent).resolve()

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

DATA_DIR       = ROOT / "data"
DB_DIR         = DATA_DIR / "dev_databases"
QUESTIONS_FILE = DATA_DIR / "mini_dev_sqlite.json"
GOLD_FILE      = DATA_DIR / "mini_dev_sqlite_gold.sql"  
RESULTS_DIR    = ROOT / "results"
EVAL_DIR       = ROOT / "reference" / "evaluation"

# ---------------------------------------------------------------------------
# 多 LLM 提供方注册表
# ---------------------------------------------------------------------------
# 每个 provider 描述「怎么调一次 chat completions」。新增模型只需在这里加一项，
# 或用环境变量 LLM_MODEL / LLM_BASE_URL / LLM_API_KEY_ENV 定义 custom。
#
# 字段：
#   model            —— 请求里的 model 名
#   model_version    —— 写入结果/报告的展示名
#   base_url         —— OpenAI 兼容 endpoint
#   api_key_env      —— 从哪个环境变量读密钥
#   extra_body       —— 额外请求体（DeepSeek 的 thinking 开关等）；None 表示不发
#   send_temperature —— 是否在请求里带 temperature（部分推理模型不接受）
#   max_tokens_param —— 用 max_tokens 还是 max_completion_tokens
#
# 说明：extra_body 是厂商私有字段，发给不支持的厂商会报错，所以按 provider 隔离。
PROVIDERS = {
    "deepseek-flash": {
        "model": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "extra_body": {"thinking": {"type": "disabled"}},
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    "deepseek-chat": {
        "model": "deepseek-chat",
        "model_version": "DeepSeek-Chat",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "extra_body": {"thinking": {"type": "disabled"}},
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    "qwen-plus": {
        "model": "qwen-plus",
        "model_version": "Qwen-Plus",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "extra_body": None,
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    "qwen-max": {
        "model": "qwen-max",
        "model_version": "Qwen-Max",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "extra_body": None,
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    "gpt-4o": {
        "model": "gpt-4o",
        "model_version": "GPT-4o",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "extra_body": None,
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    "gpt-4o-mini": {
        "model": "gpt-4o-mini",
        "model_version": "GPT-4o-mini",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "extra_body": None,
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    "glm-4.6": {
        "model": "glm-4.6",
        "model_version": "GLM-4.6",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPU_API_KEY",
        "extra_body": None,
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    "kimi": {
        # 用 MOONSHOT_MODEL，避免与 custom provider 的 LLM_MODEL 抢同一个变量
        "model": os.environ.get("MOONSHOT_MODEL", "moonshot-v1-8k"),
        "model_version": "Moonshot-Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "extra_body": None,
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    # --- 自建 OpenAI 兼容网关（vllm 部署）---
    # 思考开关：该服务**认 vllm 的字段，不认 DeepSeek 官方的 thinking:{type}**。
    #   有效   —— chat_template_kwargs.enable_thinking=false
    #             或 reasoning_effort="none"（OpenAI 新字段，vllm 亦支持）
    #   无效   —— thinking:{"type":"disabled"}（官方 DeepSeek 字段，服务不透传）
    # 实测（2026-11-02）：关思考后 reasoning 字段为空、completion_tokens 1024→16、
    # 延迟 4.3s→0.9s，且 tool calling 正常。故：
    #   *-nothink  → 关思考，可作为与官方 deepseek-flash（同样关思考）对齐的口径
    #   不带后缀   → 保持思考开启，对应 10-30 那批已跑数据的历史口径
    #
    # 端点与密钥**不写死在仓库里**，只从环境变量读：
    #   PROXY_BASE_URL（必填，缺失时 provider_spec 会明确报错）
    #   PROXY_API_KEY
    "proxy-ds-flash": {
        "model": "deepseek/deepseek-v4.1-flash",
        "model_version": "DeepSeek-V4.1-Flash(gateway,vllm)",
        "base_url": os.environ.get("PROXY_BASE_URL", ""),
        "api_key_env": "PROXY_API_KEY",
        "extra_body": None,
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    "proxy-ds-nothink": {
        "model": "deepseek/deepseek-v4.1-flash",
        "model_version": "DeepSeek-V4.1-Flash(gateway,vllm,think-off)",
        "base_url": os.environ.get("PROXY_BASE_URL", ""),
        "api_key_env": "PROXY_API_KEY",
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    "proxy-glm-flash": {
        "model": "glm5.3-flash",
        "model_version": "GLM-5.3-Flash(gateway,vllm)",
        "base_url": os.environ.get("PROXY_BASE_URL", ""),
        "api_key_env": "PROXY_API_KEY",
        "extra_body": None,
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    "proxy-glm-nothink": {
        "model": "glm5.3-flash",
        "model_version": "GLM-5.3-Flash(gateway,vllm,think-off)",
        "base_url": os.environ.get("PROXY_BASE_URL", ""),
        "api_key_env": "PROXY_API_KEY",
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    "proxy-qwen-27b": {
        "model": "qwen3.8-27b",
        "model_version": "Qwen3.8-27B(gateway,vllm)",
        "base_url": os.environ.get("PROXY_BASE_URL", ""),
        "api_key_env": "PROXY_API_KEY",
        "extra_body": None,
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
    # 任意 OpenAI 兼容服务：用 LLM_MODEL / LLM_BASE_URL / LLM_API_KEY_ENV 指定
    "custom": {
        "model": os.environ.get("LLM_MODEL", ""),
        "model_version": os.environ.get("LLM_MODEL", "custom"),
        "base_url": os.environ.get("LLM_BASE_URL", ""),
        "api_key_env": os.environ.get("LLM_API_KEY_ENV", "LLM_API_KEY"),
        "extra_body": None,
        "send_temperature": True,
        "max_tokens_param": "max_tokens",
    },
}

DEFAULT_PROVIDER = os.environ.get("DB_AGENT_MODEL", "deepseek-flash")


def list_providers():
    """可用于 --model / DB_AGENT_MODEL 的 provider 名。"""
    return list(PROVIDERS)


def provider_spec(name):
    if name not in PROVIDERS:
        raise SystemExit(
            f"未知模型 {name!r}。可选：{', '.join(PROVIDERS)}\n"
            f"  或在环境变量里用 LLM_MODEL / LLM_BASE_URL / LLM_API_KEY_ENV 定义 custom"
        )
    spec = dict(PROVIDERS[name])
    if name == "custom" and (not spec["base_url"] or not spec["model"]):
        raise SystemExit("custom provider 需要同时设置 LLM_MODEL 与 LLM_BASE_URL")
    if name.startswith("proxy-") and not spec["base_url"]:
        raise SystemExit(
            f"{name} 需要设置 PROXY_BASE_URL（OpenAI 兼容端点，形如 https://host/v1）。\n"
            f"  网关地址不写死在仓库里，请写进 .env 或 export PROXY_BASE_URL=..."
        )
    return spec


def _thinking_flags(extra_body):
    """从 extra_body 反推「思考状态」，兼容三种写法。

    返回 (thinking, disabled)：thinking 只反映 DeepSeek 官方的 thinking 字段
    （供 llm 层判断是否要抑制 temperature），disabled 表示**已证明关闭思考**。
    """
    eb = extra_body or {}
    thinking = eb.get("thinking")
    ctk = eb.get("chat_template_kwargs") or {}
    effort = str(eb.get("reasoning_effort", "")).lower()
    disabled = (
        (thinking or {}).get("type") == "disabled"
        or ctk.get("enable_thinking") is False
        or effort in ("none", "minimal")
    )
    return thinking, disabled


# 当前激活的 provider（模块级可变状态；run/score/analyze 通过 use_provider 切换）
ACTIVE_PROVIDER = DEFAULT_PROVIDER
PROVIDER        = provider_spec(DEFAULT_PROVIDER)
MODEL           = PROVIDER["model"]
MODEL_VERSION   = PROVIDER["model_version"]
BASE_URL        = PROVIDER["base_url"]
API_KEY_ENV     = PROVIDER["api_key_env"]
EXTRA_BODY      = PROVIDER["extra_body"]
SEND_TEMPERATURE = PROVIDER["send_temperature"]
MAX_TOKENS_PARAM = PROVIDER["max_tokens_param"]
THINKING, THINKING_DISABLED = _thinking_flags(EXTRA_BODY)


def use_provider(name):
    """切换全局激活的模型；llm 层每次调用都会读取这些模块级变量。"""
    global ACTIVE_PROVIDER, PROVIDER, MODEL, MODEL_VERSION, BASE_URL
    global API_KEY_ENV, EXTRA_BODY, SEND_TEMPERATURE, MAX_TOKENS_PARAM
    global THINKING, THINKING_DISABLED
    PROVIDER = provider_spec(name)
    ACTIVE_PROVIDER  = name
    MODEL            = PROVIDER["model"]
    MODEL_VERSION    = PROVIDER["model_version"]
    BASE_URL         = PROVIDER["base_url"]
    API_KEY_ENV      = PROVIDER["api_key_env"]
    EXTRA_BODY       = PROVIDER["extra_body"]
    SEND_TEMPERATURE = PROVIDER["send_temperature"]
    MAX_TOKENS_PARAM = PROVIDER["max_tokens_param"]
    THINKING, THINKING_DISABLED = _thinking_flags(EXTRA_BODY)
    return PROVIDER


use_provider(DEFAULT_PROVIDER)


TEMPERATURE = 0.0

TEMPERATURE_SAMPLING = 0.7
# 8192：为思考开启的配置留足 reasoning 空间（思考 token 计入 completion_tokens，
# 2048 会把正文截断导致 final_sql 采集失败）。关思考的配置实际只用几十 token，
# 上限不影响计费（按实际用量计）。
MAX_TOKENS  = 8192

REQUEST_TIMEOUT = 120

def require_api_key():
    k = os.environ.get(API_KEY_ENV, "").strip()
    if not k:
        raise RuntimeError(
            f"{API_KEY_ENV} 未设置。请在 {ROOT / '.env'} 写入 "
            f"{API_KEY_ENV}=sk-xxx，或 `export {API_KEY_ENV}=sk-xxx`"
        )
    return k

MAX_STEPS     = 10
K_CANDIDATES  = 3
SMOKE_N       = 20

# A0 / A0+ 控制组：无执行反馈的自我反思轮数。
# 取 3 与主实验 A1 的平均轮数（3.57）同量级；收敛（与上轮逐字相同）即提前停。
SELFREFINE_ROUNDS = 3

# A2（反馈消融组）：run_sql 只返回固定「无数据库访问」桩，模型拿不到任何行列信息。
# 步数上限与 A1 完全一致（MAX_STEPS），保证两组唯一差别就是「反馈内容」。
# 实测模型拿到桩后基本都在第 2 轮就 submit，上限放宽不会增加成本。
A2_MAX_STEPS = 10

RESULT_ROW_LIMIT      = 100
QUERY_TIMEOUT_SEC     = 5
TOOL_OUTPUT_MAX_CHARS = 2000

API_MAX_RETRIES  = 3
API_BACKOFF_BASE = 2
CONCURRENCY      = 5

FALLBACK_TO_LAST_EXECUTED = True

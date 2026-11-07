import time
import re

from openai import OpenAI

from db_agent import config

# 客户端按 (base_url, api_key) 缓存：切换 provider 时无需显式清理，
# 同一 provider 复用连接，不同 provider 各建一份。
_clients = {}


def _get_client():
    key = config.require_api_key()
    ck = (config.BASE_URL, key)
    cli = _clients.get(ck)
    if cli is None:
        cli = OpenAI(api_key=key, base_url=config.BASE_URL,
                     timeout=config.REQUEST_TIMEOUT)
        _clients[ck] = cli
    return cli


try:
    from openai import (RateLimitError, APIConnectionError, APITimeoutError,
                        InternalServerError)
    _RETRYABLE = (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
except ImportError:
    _RETRYABLE = (Exception,)


def chat(messages, tools=None, temperature=None, thinking=None, max_tokens=None):
    """单次 chat completions。provider 差异（extra_body/温度/参数名）由 config 决定。

    thinking 参数保留为显式覆盖：传 None 用 provider 默认 extra_body；
    传 dict 则临时覆盖为 {"thinking": thinking}（仅对支持该字段的厂商有意义）。
    """
    temperature = config.TEMPERATURE if temperature is None else temperature
    extra_body = config.EXTRA_BODY if thinking is None else {"thinking": thinking}
    enabled = bool((extra_body or {}).get("thinking", {}).get("type") == "enabled")
    last_err = None

    for attempt in range(config.API_MAX_RETRIES):
        kwargs = dict(model=config.MODEL, messages=messages)
        cap = config.MAX_TOKENS if max_tokens is None else max_tokens
        kwargs[config.MAX_TOKENS_PARAM] = cap
        if config.SEND_TEMPERATURE and not enabled:
            kwargs["temperature"] = temperature
        if extra_body:
            kwargs["extra_body"] = extra_body
        if tools:
            kwargs["tools"] = tools

        t0 = time.perf_counter()
        try:
            resp = _get_client().chat.completions.create(**kwargs)
        except _RETRYABLE as e:
            last_err = e
            time.sleep(config.API_BACKOFF_BASE ** attempt)
            continue
        call_latency = time.perf_counter() - t0

        u = resp.usage
        # DeepSeek 官方：prompt_cache_hit_tokens；vllm 部署：prompt_tokens_details.cached_tokens
        hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
        if not hit:
            ptd = getattr(u, "prompt_tokens_details", None)
            hit = getattr(ptd, "cached_tokens", 0) or 0
        meta = dict(
            call_latency=call_latency,
            prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(u, "completion_tokens", 0) or 0,
            cache_hit_tokens=hit,
            cache_miss_tokens=max((getattr(u, "prompt_tokens", 0) or 0) - hit, 0),
            finish_reason=getattr(resp.choices[0], "finish_reason", None),
            provider=config.ACTIVE_PROVIDER,
            model=config.MODEL,
        )
        return resp.choices[0].message, meta

    raise RuntimeError(f"API 重试 {config.API_MAX_RETRIES} 次仍失败: {last_err}")


_QUERY_RE = re.compile(r"(?is)\b(SELECT|WITH)\b")

_CLOSURE_OR_INVOKE = re.compile(r"</?[\uff5c|]*DSML[\uff5c|]*[^>]*>")


def strip_tool_syntax(text):
    if not text:
        return text
    hits = re.findall(
        r'<[\uff5c|]*DSML[\uff5c|]*[^>]*parameter[^>]*?name="sql"[^>]*>(.*?)'
        r'<[\uff5c|]*DSML[\uff5c|]*[^>]*parameter',
        text, re.S | re.I)
    if hits:
        return hits[-1].strip()
    m = _CLOSURE_OR_INVOKE.search(text)
    return text[:m.start()].strip() if m else text


def extract_sql(text):
    if not text:
        return None
    text = strip_tool_syntax(text)

    cand = None
    for pat in (r"```sql\s*(.+?)```", r"```\s*(.+?)```"):
        m = re.search(pat, text, re.S | re.I)
        if m:
            cand = m.group(1).strip().rstrip(";")
            break
    if cand is None:
        m = _QUERY_RE.search(text)
        cand = text[m.start():].strip().rstrip(";") if m else None

    if not cand or not _QUERY_RE.search(cand):
        return None
    parts = [s.strip() for s in cand.split(";")]
    tail = next((s for s in reversed(parts) if _QUERY_RE.match(s.strip())), None)
    return tail or cand

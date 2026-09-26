import time
import re

from openai import OpenAI

from db_agent import config

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.require_api_key(),
            base_url=config.BASE_URL,
            timeout=config.REQUEST_TIMEOUT,
        )
    return _client


try:
    from openai import (RateLimitError, APIConnectionError, APITimeoutError,
                        InternalServerError)
    _RETRYABLE = (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
except ImportError:
    _RETRYABLE = (Exception,)


def chat(messages, tools=None, temperature=None, thinking=None, max_tokens=None):
    thinking = config.THINKING if thinking is None else thinking
    enabled = thinking.get("type") == "enabled"
    temperature = config.TEMPERATURE if temperature is None else temperature
    last_err = None

    for attempt in range(config.API_MAX_RETRIES):
        kwargs = dict(
            model=config.MODEL,
            messages=messages,
            max_tokens=config.MAX_TOKENS if max_tokens is None else max_tokens,
            extra_body={"thinking": thinking},
        )
        if not enabled:
            kwargs["temperature"] = temperature
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
        meta = dict(
            call_latency=call_latency,
            prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(u, "completion_tokens", 0) or 0,
            cache_hit_tokens=getattr(u, "prompt_cache_hit_tokens", 0) or 0,
            cache_miss_tokens=getattr(u, "prompt_cache_miss_tokens", 0) or 0,
            finish_reason=getattr(resp.choices[0], "finish_reason", None),
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

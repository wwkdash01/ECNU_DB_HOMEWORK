"""llm.py —— 唯一的 API 出口：chat() + extract_sql()

相对手册（§2.2）的四处调整（均有原因，见各处注释）：
  1. 密钥走 config.require_api_key()，而不是裸 os.environ[...]
  2. 显式传 extra_body={"thinking": ...} —— 手册写于模型默认非思考的年代；
     不关思考则 temperature 静默失效，O3 采样机制不成立
  3. 只重试【瞬时】错误；参数错/鉴权错立刻抛，避免每题白等 7 秒
  4. meta 增记 finish_reason —— 'length' 表示被 max_tokens 截断（静默失败信号）
"""
import time
import re

from openai import OpenAI

import config

_client = None


def _get_client():
    """惰性创建，避免 import 阶段就要求密钥存在"""
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.require_api_key(),
            base_url=config.BASE_URL,
            timeout=config.REQUEST_TIMEOUT,
        )
    return _client


# 只对这些【瞬时】错误重试；其余（鉴权、参数非法）立刻抛出
try:
    from openai import (RateLimitError, APIConnectionError, APITimeoutError,
                        InternalServerError)
    _RETRYABLE = (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
except ImportError:                                   # SDK 版本差异兜底
    _RETRYABLE = (Exception,)


def chat(messages, tools=None, temperature=None):
    """调用一次模型，返回 (message, meta)。

    meta 含【每次调用单独计时】与 token 分项 —— 事后补不回来。
    call_latency 是长尾分解的前提：
        总延迟 = Σ call_latency + Σ qet + 调度开销
    没有它就说不清长尾来自「agent 多轮」还是「API 自身抖动」。
    """
    temperature = config.TEMPERATURE if temperature is None else temperature
    last_err = None

    for attempt in range(config.API_MAX_RETRIES):
        kwargs = dict(
            model=config.MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=config.MAX_TOKENS,
            extra_body={"thinking": config.THINKING},   # 见模块 docstring 第 2 条
        )
        if tools:
            kwargs["tools"] = tools

        t0 = time.perf_counter()
        try:
            resp = _get_client().chat.completions.create(**kwargs)
        except _RETRYABLE as e:                          # 限流 / 网络 / 服务端
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


def extract_sql(text):
    """从模型回复里抽 SQL；抽不到返回 None（计 EX=0，归 parse_error）。

    四条路径：```sql 围栏 -> ``` 围栏 -> 裸 SELECT/WITH -> None。
    加固（手册之外的防御）：抽出物必须含 SELECT|WITH。实测模型偶尔只吐
    CREATE TABLE，此时返回 DDL 比返回 None 更难排查（判分会失败但看不出原因）。
    """
    if not text:
        return None

    cand = None
    for pat in (r"```sql\s*(.+?)```", r"```\s*(.+?)```"):
        m = re.search(pat, text, re.S | re.I)
        if m:
            cand = m.group(1).strip().rstrip(";")
            break
    if cand is None:
        m = _QUERY_RE.search(text)
        cand = text[m.start():].strip().rstrip(";") if m else None

    # 必须是一条查询；纯 DDL / 纯说明文字一律视为"没抽出 SQL"
    if not cand or not _QUERY_RE.search(cand):
        return None
    return cand

"""llm.py —— 唯一的 API 出口：chat() + extract_sql()

相对手册（§2.2）的四处调整（均有原因，见各处注释）：
  1. 密钥走 config.require_api_key()，而不是裸 os.environ[...]
  2. 显式传 extra_body={"thinking": ...} —— 手册写于模型默认非思考的年代。
     注：deepseek-flash 官方【默认开启】思考模式，所以这里传的是 disabled；
     A9 反向传 enabled，于是 O/A 全体与历史结果口径不变。
     不关思考则 temperature 静默失效，O3 采样机制不成立。
  3. 只重试【瞬时】错误；参数错/鉴权错立刻抛，避免每题白等 7 秒
  4. meta 增记 finish_reason —— 'length' 表示被 max_tokens 截断（静默失败信号）
  5. A9 增记 reasoning_tokens —— 推理 token 按 output 计费，不记会低估成本

★ A9 的一个隐式依赖（方法层面，但成因在这里）：
  带 tools 的请求，官方要求【所有历史轮次的 reasoning_content 都必须回传】，
  否则 API 直接 400。本仓库无需特判 —— methods.run_agent 用
  `messages.append(msg)` 追加 SDK 原始消息对象，而 openai==3.16.2 的
  BaseModel 是 extra="allow"，reasoning_content 会原样保留并序列化回传。
  若将来把 append(msg) 改成手搓 dict，必须显式带上 reasoning_content。
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


def chat(messages, tools=None, temperature=None, thinking=None,
         reasoning_effort=None, max_tokens=None):
    """调用一次模型，返回 (message, meta)。

    meta 含【每次调用单独计时】与 token 分项 —— 事后补不回来。
    call_latency 是长尾分解的前提：
        总延迟 = Σ call_latency + Σ qet + 调度开销
    没有它就说不清长尾来自「agent 多轮」还是「API 自身抖动」。

    thinking 参数（A9 用）三处语义变化，官方文档明写：
      * 思考模式【不支持 temperature】，传了不报错但也不生效 —— 故这里
        思考模式下干脆不传，避免报告里出现"设了 T=0"的假陈述。
      * 推理 token 与正式输出【共享 max_tokens】，且推理长度不可预知，
        所以 A9 必须单独放开上限（config.MAX_TOKENS_A9 = 393216，API 实测上限），
        否则会被静默截断。
      * reasoning token 按 output 计费，必须记账（见下方 reasoning_tokens），
        否则成本表会低估一大截。
    """
    thinking = config.THINKING if thinking is None else thinking
    enabled = thinking.get("type") == "enabled"
    temperature = config.TEMPERATURE if temperature is None else temperature
    last_err = None

    for attempt in range(config.API_MAX_RETRIES):
        kwargs = dict(
            model=config.MODEL,
            messages=messages,
            max_tokens=config.MAX_TOKENS if max_tokens is None else max_tokens,
            extra_body={"thinking": thinking},          # 见模块 docstring 第 2 条
        )
        if not enabled:
            kwargs["temperature"] = temperature          # 思考模式下该参数无效，故不传
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
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
        _det = getattr(u, "completion_tokens_details", None)
        meta = dict(
            call_latency=call_latency,
            prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(u, "completion_tokens", 0) or 0,
            # 思考模式专用：推理 token 按 output 计费，不记这一项就会低估成本
            reasoning_tokens=getattr(_det, "reasoning_tokens", 0) or 0,
            cache_hit_tokens=getattr(u, "prompt_cache_hit_tokens", 0) or 0,
            cache_miss_tokens=getattr(u, "prompt_cache_miss_tokens", 0) or 0,
            finish_reason=getattr(resp.choices[0], "finish_reason", None),
        )
        return resp.choices[0].message, meta

    raise RuntimeError(f"API 重试 {config.API_MAX_RETRIES} 次仍失败: {last_err}")


_QUERY_RE = re.compile(r"(?is)\b(SELECT|WITH)\b")

# 模型会把「工具调用语法」当纯文本输出（实测），需要在抽取前剥离闭合标记。
# 原文形如： SELECT ... </｜｜DSML｜｜ parameter> </｜｜DSML｜｜ invoke> ...
# 注意标记里可能有空格（如 "</｜｜DSML｜｜ parameter>"），故用 [^>]* 吞掉。
_CLOSURE_OR_INVOKE = re.compile(r"</?[\uff5c|]*DSML[\uff5c|]*[^>]*>")


def strip_tool_syntax(text):
    """剥离模型误当文本输出的工具调用标记，尽量保留其中的 SQL。

    实测形态：一次 content 里塞了多次 run_sql 的 XML 文本，多段 SQL 连在一起。
    策略：优先取最后一个 <parameter name="sql"> 的内容；否则砍掉第一个
    闭合标记之后的所有内容。
    """
    if not text:
        return text
    # 优先：最后一个内嵌 sql 参数里的内容
    hits = re.findall(
        r'<[\uff5c|]*DSML[\uff5c|]*[^>]*parameter[^>]*?name="sql"[^>]*>(.*?)'
        r'<[\uff5c|]*DSML[\uff5c|]*[^>]*parameter',
        text, re.S | re.I)
    if hits:
        return hits[-1].strip()
    # 否则：砍掉第一个闭合标记之后的所有内容
    m = _CLOSURE_OR_INVOKE.search(text)
    return text[:m.start()].strip() if m else text


def extract_sql(text):
    """从模型回复里抽 SQL；抽不到返回 None（计 EX=0，归 parse_error）。

    四条路径：```sql 围栏 -> ``` 围栏 -> 裸 SELECT/WITH -> None。
    加固（手册之外的防御）：抽出物必须含 SELECT|WITH。实测模型偶尔只吐
    CREATE TABLE，此时返回 DDL 比返回 None 更难排查（判分会失败但看不出原因）。
    """
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

    # 必须是查询；纯 DDL / 纯说明文字一律视为"没抽出 SQL"
    if not cand or not _QUERY_RE.search(cand):
        return None
    # 多语句时取【最后一条】以 SELECT|WITH 开头的语句：
    # 模型在末轮可能把多段 SQL 连在一起（实测），而它最后的意图是最后那条。
    parts = [s.strip() for s in cand.split(";")]
    tail = next((s for s in reversed(parts) if _QUERY_RE.match(s.strip())), None)
    return tail or cand

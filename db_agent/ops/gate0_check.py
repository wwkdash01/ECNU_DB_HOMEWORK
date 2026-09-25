import sqlite3, sys, json
from openai import OpenAI
from db_agent import config


def mask(k):
    return f"{k[:6]}...{k[-4:]} (len={len(k)})" if len(k) > 12 else "***"


def main():
    # ---------- ① 环境落盘 ----------
    dev = {
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "model": config.MODEL,
        "model_version": config.MODEL_VERSION,
        "thinking": config.THINKING,
        "env_source": config.ENV_SOURCE,
    }
    config.RESULTS_DIR.mkdir(exist_ok=True)
    json.dump(dev, open(config.RESULTS_DIR / "env.json", "w"), ensure_ascii=False, indent=2)
    print(json.dumps(dev, ensure_ascii=False, indent=2))

    assert sqlite3.sqlite_version_info >= (3, 41), "SQLite version should >= 3.41"

    key = config.require_api_key()
    print(f"\n[{config.API_KEY_ENV}] 来源={config.ENV_SOURCE}  值={mask(key)}")
    assert config.ENV_SOURCE != "none", "API KEY not found, plz check .env"

    c = OpenAI(api_key=key, base_url=config.BASE_URL)

    extra = {"thinking": config.THINKING}

    r = c.chat.completions.create(
        model=config.MODEL, temperature=0,
        max_tokens=64,
        messages=[{"role": "user", "content": "say ok"}],
        extra_body=extra,
    )
    fields = list(r.usage.model_dump().keys())
    print("\nusage 字段:", fields)

    r2 = c.chat.completions.create(
        model=config.MODEL, temperature=0,
        max_tokens=256,
        messages=[{"role": "user", "content": "orders 表有多少行？"}],
        tools=[{"type": "function", "function": {
            "name": "run_sql", "description": "执行 SQL 查询",
            "parameters": {"type": "object",
                "properties": {"sql": {"type": "string"}}, "required": ["sql"]}}}],
        extra_body=extra,
    )
    m = r2.choices[0].message
    tc = m.tool_calls
    print("content:", repr(m.content)[:120])
    print("tool_calls:", tc)

    # ---------- Gate 0 判定 ----------
    print("\n===== Gate 0 =====")
    ok_cache = all(k in fields for k in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"))
    ok_tool = bool(tc) and tc[0].function.name == "run_sql"
    print(f"[{'PASS' if ok_cache else 'FAIL'}] cache 分项 token 可采集 {fields}")
    print(f"[{'PASS' if ok_tool else 'FAIL'}] tool calling 可用 -> {tc[0].function.name if tc else None}")

    if not ok_tool:
        print("\n提示：若 tool_calls 为 None，先看 content 里模型是不是把答案直接写成了文本，")
        print("      再确认 extra_body 的 thinking 开关是否与模型能力匹配。")

    assert ok_cache, "缺 cache 分项 token：成本分项补不回来，别往下走"
    assert ok_tool, "tool calling 不可用：A 组做不了"
    print("\nGate 0 通过 ")


if __name__ == "__main__":
    main()

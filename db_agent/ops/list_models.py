import os
import unicodedata

from db_agent import config


def _w(s):
    """终端显示宽度：CJK 全角算 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, width):
    s = str(s)
    return s + " " * max(0, width - _w(s))


def _display_url(name, url):
    """proxy-* 的端点是私有的，只显示「已配置」，不打印真实地址。"""
    if name.startswith("proxy-"):
        return "（已配置，见 PROXY_BASE_URL）" if url else "（未配置 PROXY_BASE_URL）"
    return url


def main():
    print(f"默认 provider: {config.DEFAULT_PROVIDER}\n")

    rows = []
    for name in config.list_providers():
        try:
            spec = config.provider_spec(name)
        except SystemExit as e:
            rows.append((name, "（未配置）", "", "—", str(e).splitlines()[0]))
            continue
        env = spec["api_key_env"]
        ok = "已设置" if os.environ.get(env, "").strip() else "未设置"
        rows.append((name, spec["model"], env, ok, _display_url(name, spec["base_url"])))

    head = ("provider", "model", "密钥环境变量", "密钥状态")
    widths = [max([_w(head[i]), *(_w(r[i]) for r in rows)]) + 2 for i in range(4)]

    print("".join(_pad(head[i], widths[i]) for i in range(4)) + "base_url")
    print("-" * (sum(widths) + 52))
    for name, model, env, ok, url in rows:
        mark = " ←默认" if name == config.DEFAULT_PROVIDER else ""
        print("".join(_pad(v, widths[i]) for i, v in enumerate((name, model, env, ok))) + url + mark)

    print("\n思考开关（vllm 部署的服务）：chat_template_kwargs.enable_thinking=false")
    print("  带 -nothink 后缀的 provider 已关闭思考，可与官方 deepseek-flash（同样关思考）对齐口径。")
    print("\n用法示例：")
    print("  python -m db_agent run --group A1 --model qwen-plus")
    print("  python -m db_agent run --group A1 --model proxy-ds-nothink --out ds-nt/A1.jsonl")
    print("  或在 .env / 环境变量里设 DB_AGENT_MODEL=qwen-plus 作为默认")


if __name__ == "__main__":
    main()

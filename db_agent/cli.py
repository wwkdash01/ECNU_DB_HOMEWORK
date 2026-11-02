import sys

SUBCOMMANDS = {
    "fetch":   ("db_agent.ops.fetch_data",     lambda a: a),
    "run":     ("db_agent.experiment.run",     lambda a: a),
    "score":   ("db_agent.evaluation.score",   lambda a: a),
    "pair":    ("db_agent.evaluation.paired",  lambda a: a),
    "analyze": ("db_agent.evaluation.analyze", lambda a: a),
    "models":  ("db_agent.ops.list_models",    lambda a: []),
    "tui":     ("db_agent.tui.repro_tui",      lambda a: []),
    "check":   ("db_agent.tui.repro_tui",      lambda a: ["--check"]),
    "set-key": ("db_agent.tui.repro_tui",      lambda a: ["--set-key", *a]),
}

HELP = """\
用法：python -m db_agent <子命令> [参数…]        （须在仓库根目录执行）

子命令：
  fetch                       下载并摆正 BIRD Mini-Dev 数据集
                              （--force / --keep-zip / --redownload）
  gate 0                      阶段 0 验收：密钥 + tool calling（2 次真实调用，几分钱）
  gate 1                      阶段 1 验收：数据与起点（离线，~15 s）
  gate 2 [--offline]          阶段 2 验收：代码与埋点（--offline 不发 API）
  models                      列出可用 LLM provider 及其密钥状态
  run --group {O1,O1P,O3,A0,A0P,A2,A1}
                              跑一组实验（--model / --limit / --concurrency / --out）
                              O1/O1P=单轮 无/有形态；A0/A0P=多轮 无/有形态；
                              A2=agent+反馈桩（反馈消融）；A1=agent 闭环
  score --group {O1,O1P,O3,A0,A0P,A2,A1}
                              官方判分 → results/<group>_scored.jsonl
                              （--model 用于 O3 机制体检）
  pair A_scored.jsonl B_scored.jsonl
                              两组配对比较（McNemar 精确检验 + 分层 + 翻盘题）
  analyze                     阶段 8 离线分析（--results-dir / --out / --no-exk）
  tui                         启动复刻流程傻瓜式 TUI
  check                       非交互打印 9 步状态表（不花钱）
  set-key                     从 stdin 读 API Key 写入 .env

多模型：run/score/analyze 均支持 --model <provider>，或用环境变量 DB_AGENT_MODEL 设默认。
各子命令自己的参数说明由原脚本提供，例如：
  python -m db_agent run --help
"""


def resolve(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        return None, None

    sub, rest = argv[0], list(argv[1:])

    if sub == "gate":
        if not rest or rest[0] not in ("0", "1", "2"):
            raise SystemExit("用法：python -m db_agent gate {0|1|2} [--offline]\n"
                             "  例：python -m db_agent gate 2 --offline")
        return f"db_agent.ops.gate{rest[0]}_check", rest[1:]

    if sub in SUBCOMMANDS:
        mod, transform = SUBCOMMANDS[sub]
        return mod, transform(rest)

    raise SystemExit(f"未知子命令：{sub}\n\n{HELP}")


if __name__ == "__main__":
    mod_, tail_ = resolve(sys.argv[1:])
    if mod_ is None:
        print(HELP, end="")

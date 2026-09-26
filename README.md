# ECNU-DB-HOMEWORK

在 **BIRD Mini-Dev（SQLite）** 上对比 **one-shot** 与 **agent 闭环** 的 text-to-SQL 表现。
模型侧全部走 API（`deepseek-flash` / DeepSeek-V4.1-Flash），**不训练、不微调**；
判分复用官方 `execute_sql` + `calculate_ex`，保证口径与榜单一致。

实验设计与全部数字见 [`实验结论.md`](实验结论.md)。

---

## 1. 仓库结构

```
ECNU-DB-HOMEWORK/
├── db_agent/                    # 唯一的 Python 包（所有可执行代码）
│   ├── __main__.py              # 入口：python -m db_agent <子命令>
│   ├── cli.py                   # 子命令 → 模块的分发表（不含业务逻辑）
│   ├── config.py                # 单一事实源：模型名/路径/温度/MAX_STEPS/沙箱参数
│   ├── core/                    # 与实验组无关的底层能力
│   │   ├── data.py              #   数据加载 + build_context（O 与 A 逐字共用的起点）
│   │   ├── db.py                #   唯一执行入口：只读连接、查询超时、QET
│   │   ├── llm.py               #   唯一 API 出口：chat() + extract_sql()
│   │   ├── metrics.py           #   列级召回 + 幻觉率（sqlglot 解析）
│   │   └── plot_utils.py        #   空壳：阶段 7 可视化预留位，未实现
│   ├── experiment/              # 三组实验本体
│   │   ├── run.py               #   跑组入口（--group/--limit/--concurrency/--out）
│   │   ├── methods.py           #   run_oneshot / run_selfconsistency / run_agent
│   │   ├── prompts.py           #   ONESHOT 与 AGENT_SYSTEM（A1 基线冻结）
│   │   └── tools.py             #   run_sql + submit_answer，按组裁剪工具集
│   ├── evaluation/              # 判分与统计分析
│   │   ├── score.py             #   官方判分 → results/<group>_scored.jsonl + EX@k
│   │   ├── paired.py            #   McNemar 精确检验 + 分层 + 翻盘题清单
│   │   └── analyze.py           #   阶段 8 离线分析 → results/analyze.md
│   ├── ops/                     # 运维与验收
│   │   ├── fetch_data.py        #   下载/解压/摆正/清理 BIRD Mini-Dev
│   │   └── gate0_check.py / gate1_check.py / gate2_check.py
│   └── tui/
│       └── repro_tui.py         # 复刻流程TUI（Textual 8.x）
├── data/                        # 4 个小文件已版本化；dev_databases/ 不提交（约 1.4 GB）
├── docs/                        # doc.md（项目手册）、阶段8产出.md、requirement.pdf
├── plan/                        # 各次改动的方案文档（重构、数据抓取、TUI 等）
├── reference/                   # 官方评测脚本 + 参考实现/历史产物（只读对照）
│   ├── evaluation/              #   官方 execute_sql / calculate_ex
│   └── llm/                     #   参考 prompt、脚本与 exp_result 历史预测
├── results/                     # 运行产物（不提交）；.tui/state.json 为 TUI 台账
├── 实验结论.md                   # ★ 全部结论与数字（唯一结论来源）
└── README.md                    # 本文件
```
---

## 2. Python 与库版本

| 项 | 版本 |
| --- | --- |
| Python | **3.12.14** |
| SQLite | **3.53.4**（`gate 0` 断言 ≥ 3.41） |
| 模型 | `deepseek-flash`（DeepSeek-V4.1-Flash），思考模式显式关闭 |

**直接依赖**

| 包 | 版本 | 用途 |
| --- | --- | --- |
| `openai` | 3.16.2 | 唯一 API 客户端（`chat()` 出口） |
| `sqlglot` | 30.18.0 | 列级召回/幻觉率的 SQL 解析（`sqlite` 方言） |
| `pandas` | 3.0.6 | 结果集比较与配对表 |
| `tqdm` | 4.70.1 | 跑组进度条 |
| `python-dotenv` | 1.2.3 | 加载 `.env`（缺失时有手工解析兜底） |
| `matplotlib` | 3.11.2 | 官方评测脚本依赖 |
| `func_timeout` | 4.3.5 | 官方评测脚本的查询超时 |
| `psycopg2-binary` | 2.9.13 | 官方评测脚本依赖（本项目用 SQLite） |
| `PyMySQL` | 1.2.3 | 官方评测脚本依赖（本项目用 SQLite） |
| `textual` | 8.2.8 | **复刻流程 TUI**（`repro_tui.py`）；跑实验本身不需要 |

**随附的关键间接依赖**（全部由 `textual` 带入，本机实测）

| 包 | 版本 |
| --- | --- |
| `rich` | 15.0.0 |
| `pygments` | 2.21.0 |
| `platformdirs` | 4.11.12 |
| `markdown-it-py` | 4.2.0 |
| `mdit-py-plugins` | 0.6.1 |
| `linkify-it-py` | 2.2.0 |
| `mdurl` | 0.1.2 |

---

## 3. 快速开始（TUI 使用方法）

### 3.1 启动 TUI

```bash
python -m db_agent tui
```

启动的是全屏仪表盘：**左侧是 9 个步骤的状态与选项，右侧是子进程实时输出与进度条**。
子进程的 stdout/stderr 会原样流进右侧日志，tqdm 的 `\r` 进度被提出来喂给进度条。

### 3.2 界面与按键

| 按键 | 作用 |
| --- | --- |
| `↑` `↓` | 在左侧步骤列表中选择（选中即打印该步的状态与说明） |
| `空格` | 勾选/取消分组（O1 / O3 / A1） |
| `d` | 执行当前选中的步骤（**唯一的执行入口**，点选不会自动跑） |
| `r` | 刷新状态表 |
| `s` | 停止当前子进程（先 `TERM`，5 秒未退则 `KILL`） |
| `q` | 退出（重开自动接上，不需要重跑已完成步骤） |

执行前会弹确认框并给出**将执行的确切命令**；花钱的步骤（`Gate 0`、`跑组`）还会给出
预计成本（`README` 口径：O1 ¥0.4 / O3 ¥1.2 / A1 ¥5.3，冒烟按题数折算）。

### 3.3 手动复现

```bash
python -m db_agent check                       
printf 'sk-...' | python -m db_agent set-key   
python -m db_agent fetch                       # 数据准备
python -m db_agent gate 0                      # 阶段验收
python -m db_agent run --group A1 --limit 20   # 冒烟测试20 题
python -m db_agent run --group O1              # 全量 500 题（--out / --concurrency 可选）
python -m db_agent score --group A1            # 判分
python -m db_agent pair O1_scored.jsonl A1_scored.jsonl   # 配对检验（秒级、不发 API）
python -m db_agent analyze                     # 离线分析
```

---

## 4. 结论

> 完整表格、机制验证与方法论教训见 [`实验结论.md`](实验结论.md)。以下只摘主干。

同一冻结模型、同批次跑完，对比分析：

| 组 | 方法 | EX | 相对 O1 | 调用/题 |
| --- | --- | --- | --- | --- |
| `O1` | one-shot ×1（T=0） | 57.80% | 基准 | 1.00 |
| `O3` | 采样 ×3 + 执行结果聚类投票（T=0.7） | 59.40% | +1.60pt，**不显著**（p=0.0963） | 3.00 |
| **`A1`** | agent 循环 + 输出形态自检（T=0） | **64.40%** | **+6.60pt，显著**（p=0.0007） | 3.57 |

1. **agent 闭环显著优于 one-shot**：A1 − O1 = **+6.60pt**（McNemar p = 0.0007），
   另一次独立运行为 +5.00pt / p = 0.0059，方向与量级一致。
2. **增益来自具体机制，而不是「多轮」本身**：A1 相对 O1 只多一个自变量——**输出形态自检**
   （prompt 第一步先决定输出列，工具回报「N 行 / M 列」）。
   主导错误类是「多带佐证列」（改动前占 48.6%），而官方 EX 是集合比较、**多一列即判错**；
   改动后该类占比降到 22.7%、错误总数减少 39 条，因果链闭合。
3. **采样投票在本任务上无效，且机制上限极低**：76% 的题上三个候选的执行结果集完全相同，
   投票是空操作，天花板只有 23% 的题；实测净翻转仅 18 题，且 O3 显著劣于 A1（−5.00pt）。
4. **执行反馈无法区分对错**：O1 的错误 96% 是「SQL 合法但语义错」，agent 只能看见
   「执行成功/报错/几行几列」，因此重写等于重新掷骰子——**收益来自被迭代验证的形态约束**。
5. **代价**：A1 的 +6.60pt 约为 O1 的 8 倍 token（P50 延迟 3.7 倍、P95 7 倍）；
   O3 花 3 倍 token 换来不显著的 +1.60pt。**token 花在「带执行反馈的迭代」上回报远高于
   花在「独立采样重选」上。**

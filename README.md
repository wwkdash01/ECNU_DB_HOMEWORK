# ECNU-DB-HOMEWORK

在 **BIRD Mini-Dev（SQLite）** 上对比 **one-shot** 与 **agent 闭环** 的 text-to-SQL 表现。
模型侧全部走 API（默认 `deepseek-flash` / DeepSeek-V4.1-Flash，**可切换多模型**，见 §4），
**不训练、不微调**；
判分复用官方 `execute_sql` + `calculate_ex`，保证口径与榜单一致。

实验设计与全部数字见 [`docs/实验结论.md`](docs/实验结论.md)。

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
│   ├── experiment/              # 七组实验本体（2×2 因子 + 反馈消融）
│   │   ├── run.py               #   跑组入口（--group/--model/--limit/--concurrency/--out）
│   │   ├── methods.py           #   run_oneshot(O1/O1P) / run_selfconsistency(O3) / run_selfrefine(A0/A0P) / run_agent(A1/A2)
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
├── docs/                        # 实验结论.md（结论唯一来源）、期末报告.md、requirement.pdf 等
├── plan/                        # 各次改动的方案文档（重构、数据抓取、TUI 等）
├── reference/                   # 官方评测脚本 + 参考实现/历史产物（只读对照）
│   ├── evaluation/              #   官方 execute_sql / calculate_ex
│   └── llm/                     #   参考 prompt、脚本与 exp_result 历史预测
├── results/                     # 运行产物（不提交）；.tui/state.json 为 TUI 台账
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
| `空格` | 勾选/取消分组（O1 / O1P / O3 / A0 / A0P / A2 / A1） |
| `d` | 执行当前选中的步骤（**唯一的执行入口**，点选不会自动跑） |
| `r` | 刷新状态表 |
| `s` | 停止当前子进程（先 `TERM`，5 秒未退则 `KILL`） |
| `q` | 退出（重开自动接上，不需要重跑已完成步骤） |

执行前会弹确认框并给出**将执行的确切命令**；花钱的步骤（`Gate 0`、`跑组`）还会给出
预计成本（`README` 口径：O1 ¥0.4 / O1P ¥0.4 / O3 ¥1.2 / A0 ¥0.8 / A0P ¥0.8 / A2 ¥3.0 / A1 ¥5.3，冒烟按题数折算）。

### 3.3 手动复现

```bash
python -m db_agent check                       
printf 'sk-...' | python -m db_agent set-key   
python -m db_agent fetch                       # 数据准备
python -m db_agent gate 0                      # 阶段验收
python -m db_agent run --group A1 --limit 20   # 冒烟测试20 题
python -m db_agent run --group O1              # 全量 500 题（--out / --concurrency 可选）
python -m db_agent run --group A0              # A0 控制组：自反思多轮、无执行反馈
python -m db_agent score --group A1            # 判分
python -m db_agent pair O1_scored.jsonl A1_scored.jsonl   # 配对检验（秒级、不发 API）
python -m db_agent analyze                     # 离线分析
```

---

## 4. 补充实验：2×2 因子拆解（迭代 × 形态自检）+ 反馈消融

### 4.1 实验组一览

主实验只能回答「A1 比 O1 强」，但 A1 相对 O1 同时动了三件事：**多轮迭代**、
**输出形态自检指令**、**执行反馈**。为此补 7 组做因子拆解：

| 组 | 轮数 | 形态自检 | 执行反馈 | 作用 |
| --- | --- | --- | --- | --- |
| `O1` | 1 | ✗ | ✗ | 基准（主实验同款 prompt） |
| `O1P` | 1 | ✓ | ✗ | **形态指令的纯效应**（只多一段 prompt） |
| `A0` | ≤3 | ✗ | ✗ | **迭代的纯效应**（多轮、无反馈、无形态提示） |
| `A0P` | ≤3 | ✓ | ✗ | 迭代 + 形态（= 旧版 A0，prompt 逐字冻结） |
| `A2` | ≤10 | ✓ | 桩 | **反馈的纯效应**（同 A1 的 prompt/工具/上限，只把回灌换成桩） |
| `A1` | ≤10 | ✓ | ✓ | 真实执行反馈闭环 |
| `O3` | 3 | ✗ | ✗ | 采样投票（另一种"多花 token"方式，与迭代无关） |

**为什么 A2 是关键组。** 旧版 A0 与 A1 差的不止「执行反馈」：prompt 不同、无工具、
轮数预算不同，而且旧 A0 的 prompt 里**自带形态自检条款**——恰好是主实验认定 A1
增益来源的那条指令。所以旧 A0 − O1 混了「多轮」与「白送一次形态指令」两个变量。
A2 用与 A1 **完全相同**的 system prompt、工具集与步数上限，只把 `run_sql` /
`submit_answer` 的回灌内容替换成固定桩（不含行数/列名/报错），因此：

```
形态指令效应 = O1P − O1
迭代效应     = A0  − O1
反馈效应     = A1  − A2      ← 唯一差别只有回灌内容
交互项       = (A0P − O1P) − (A0 − O1)
```

A0/A0P 默认 3 轮（`SELFREFINE_ROUNDS`），模型重复上一轮 SQL 即提前收敛。
A2 实测全部在第 2 轮就提交（上限同为 10，不是人为截断）。

### 4.2 切换 / 新增 LLM

```bash
python -m db_agent models                     # 列出可用 provider 与密钥状态
python -m db_agent run --group A1 --model qwen-plus
python -m db_agent run --group A0 --model glm-4.6
```

- 已内置 `deepseek-flash / deepseek-chat / qwen-plus / qwen-max / gpt-4o / gpt-4o-mini /
  glm-4.6 / kimi`，以及用环境变量定义的 `custom`（任意 OpenAI 兼容服务）。
- **自建 OpenAI 兼容网关**：`proxy-ds-flash` / `proxy-glm-flash`（思考开启，对应 10-30 那批历史数据）
  与 `proxy-ds-nothink` / `proxy-glm-nothink`（**思考关闭**，本批补充实验用）。
  base_url/密钥在 `.env`（`PROXY_BASE_URL` / `PROXY_API_KEY`）。
- 密钥放 `.env`（对应 `DASHSCOPE_API_KEY` / `OPENAI_API_KEY` / `ZHIPU_API_KEY` /
  `MOONSHOT_API_KEY` 等）；默认 provider 可用 `DB_AGENT_MODEL` 环境变量固定。
- 非 `deepseek-flash` 的 provider，输出文件自动带后缀（如 `A1__qwen-plus.jsonl`），
  不会覆盖已有结果；判分时 `score --group A1 --out A1__qwen-plus.jsonl`。
- 建议**一个模型一个子目录**，方便 `analyze --results-dir` 一次读完：

```bash
python -m db_agent run --group A1 --model qwen-plus --out qwen/A1.jsonl
python -m db_agent score --group A1 --model qwen-plus --out qwen/A1.jsonl
python -m db_agent analyze --results-dir results/qwen --model qwen-plus
```

> ✅ **思考开关（实测结论）**：vllm 部署的服务**认 vllm 的字段，不认 DeepSeek 官方的
> `thinking:{type}`**。有效写法是 `chat_template_kwargs.enable_thinking=false`
> （或 `reasoning_effort="none"`）。实测关思考后 `reasoning` 为空、
> `completion_tokens` 1024→16、延迟 4.3s→0.9s，且 tool calling 正常。
>
> 因此 `-nothink` 后缀的 provider 与官方 `deepseek-flash`（同样关思考）**口径对齐**，
> 可直接互验：关思考后 O3 的发散度与主实验一致（SQL 全同 27.8% vs 23.0%，
> 结果集单簇 77.6% vs 76.0%），说明该 serving 是官方 API 的合格替身。
>
> usage 仍是 vllm 风格（`prompt_tokens_details.cached_tokens`），`llm.py` 已做两口径兜底。

### 4.3 一个模型跑完整七组（批量脚本）

```bash
MODEL=proxy-ds-nothink        # 思考关闭；换成 proxy-glm-nothink 即换模型
for g in O1 O1P O3 A0 A0P A2 A1; do
  python -m db_agent run   --group $g --model $MODEL --out $MODEL/$g.jsonl
  python -m db_agent score --group $g --model $MODEL --out $MODEL/$g.jsonl
done
python -m db_agent pair $MODEL/O1_scored.jsonl  $MODEL/O1P_scored.jsonl   # 形态指令
python -m db_agent pair $MODEL/O1_scored.jsonl  $MODEL/A0_scored.jsonl    # 迭代
python -m db_agent pair $MODEL/A2_scored.jsonl  $MODEL/A1_scored.jsonl    # 执行反馈
python -m db_agent analyze --results-dir results/$MODEL --no-exk --out results/$MODEL/analyze.md
```

### 4.4 结果（n=500，api_error 计错）

| 模型 | O1 | O1P | O3 | A0 | A0P | A2 | **A1** |
| --- | --- | --- | --- | --- | --- | --- | --- |
| DeepSeek-V4.1-Flash | 59.80% | 61.80% | 58.80% | 59.20% | 57.60% | 58.80% | **62.80%** |
| GLM-5.3-Flash | 55.80% | 57.40% | **59.20%** | 57.60% | 56.60% | 57.60% | 57.60% |

因子分解（McNemar）：

| 对比 | 含义 | DeepSeek Δ (p) | GLM Δ (p) |
| --- | --- | --- | --- |
| `O1P − O1` | 形态指令 | +2.00pt (0.121) | +1.60pt (0.403) |
| `A0 − O1` | 迭代 | −0.60pt (0.711) | +1.80pt (0.253) |
| `A0P − O1P` | 迭代（有形态） | **−4.20pt (0.0011)** | −0.80pt (0.704) |
| **`A1 − A2`** | **执行反馈** | **+4.00pt (0.0055)** | **0.00pt (1.000)** |
| `O3 − O1` | 采样投票 | −1.00pt (0.405) | **+3.40pt (0.0186)** |

**四条结论**

1. **只有执行反馈带来显著增益，而且是模型相关的**：DeepSeek +4.00pt（p=0.0055）；
   GLM 精确为 0（赢 35 / 输 35，A1 与 A2 同为 57.60%）。
2. **机制是「可执行性 vs 语义」**：GLM 单轮就有 36 题 `exec_error`，A1 修到 0 但 EX 不涨——
   修好的是"能不能跑"；DeepSeek 只有 5 题，反馈被用于语义试错，才转化为 +4pt。
3. **无反馈的多轮自我修改有害**：`A0P − O1P = −4.20pt（p=0.0011）`，多给两轮"自己再看看"
   反而显著变差；纯迭代 A0 与 O1 无差异。
4. **旧版 A0 的"+2.2pt 迭代增益"不成立**：它的 prompt 自带形态自检条款、且混在思考开启配置里。
   旧数据改名 `A0P` 冻结留存，仅作复算，不再作结论依据（详见报告 §4.8.6）。

> ⚠️ 本节跑在自建 OpenAI 兼容网关上（关思考）。同模型 O1 在官方 API 是 57.80%、此处 59.80%，
> A1 总效应也从 +6.60pt（官方）缩到 +3.00pt（此处）。**绝对 EX 与效应量不能跨 serving 比**。

---

## 5. 结论

> 完整表格、机制验证与方法论教训见 [`docs/实验结论.md`](docs/实验结论.md)；
> 2×2 因子拆解与跨模型的补充结果见 §4.4 与 [`docs/期末报告.md`](docs/期末报告.md) §4.8。
> 以下只摘主实验主干（单模型、官方 API、思考关闭）。

同一冻结模型、同批次跑完，对比分析：

| 组 | 方法 | EX | 相对 O1 | 调用/题 |
| --- | --- | --- | --- | --- |
| `O1` | one-shot ×1（T=0） | 57.80% | 基准 | 1.00 |
| `O3` | 采样 ×3 + 执行结果聚类投票（T=0.7） | 59.40% | +1.60pt，**不显著**（p=0.0963） | 3.00 |
| **`A1`** | agent 循环 + 输出形态自检（T=0） | **64.40%** | **+6.60pt，显著**（p=0.0007） | 3.57 |

1. **agent 闭环显著优于 one-shot**：A1 − O1 = **+6.60pt**（McNemar p = 0.0007），
   另一次独立运行为 +5.00pt / p = 0.0059，方向与量级一致。
2. **增益来自具体机制，而不是「多轮」本身**：§4.4 的因子实验把 A1 相对 O1 的三个变量
   （迭代 / 形态指令 / 执行反馈）拆开——**只有执行反馈显著**（DeepSeek +4.00pt，p=0.0055），
   迭代本身无效应、无反馈的多轮自我修改甚至显著有害（−4.20pt，p=0.0011）。
   主导错误类是「多带佐证列」（改动前占 48.6%），而官方 EX 是集合比较、**多一列即判错**。
3. **采样投票在本任务上无效，且机制上限极低**：76% 的题上三个候选的执行结果集完全相同，
   投票是空操作，天花板只有 23% 的题；实测净翻转仅 18 题，且 O3 显著劣于 A1（−5.00pt）。
   ⚠️ 这条**不跨模型**：GLM-5.3-Flash 上 O3 为 +3.40pt（p=0.0186，显著为正）。
4. **执行反馈只能修「可执行性」，修不了「语义对错」**：O1 的错误 96% 是「SQL 合法但语义错」，
   agent 只能看见「执行成功/报错/几行几列」。§4.4 的反馈桩组把这个上限量化了：
   GLM 上反馈把 36 题 `exec_error` 修到 0，EX 却一分没涨（净效应精确为 0）。
5. **代价**：A1 的 +6.60pt 约为 O1 的 8 倍 token（P50 延迟 3.7 倍、P95 7 倍）；
   O3 花 3 倍 token 换来不显著的 +1.60pt。**token 花在「带执行反馈的迭代」上回报远高于
   花在「独立采样重选」上。**

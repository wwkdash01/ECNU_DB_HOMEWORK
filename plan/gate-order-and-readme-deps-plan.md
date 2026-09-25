---
name: gate-order-and-readme-deps
overview: 方案A 调整 TUI 步骤顺序为 0→1→2；并在 README 环境配置中补上本次新增的 textual 及其间接依赖
todos:
  - id: reorder
    content: repro_tui.py 的 STEPS 改为 env→secret→Gate 0→data→Gate 1→Gate 2→run→score→pair，并清掉 detect() 里会过期的编号注释
    status: pending
  - id: readme-pip
    content: README「快速开始·安装依赖」补 textual==8.2.8（标注仅 TUI 需要）+ fetch_data.py 零额外依赖说明
    status: pending
  - id: readme-tables
    content: README「依赖清单」直接依赖加 textual 行；随附间接依赖加 rich 等 7 行
    status: pending
  - id: verify
    content: 无头断言新顺序（第 3 项必须是 gate0）+ README 代码块 bash 语法 + 表格行存在性
    status: pending
isProject: false
---

# 方案A：TUI 步骤顺序对齐 0→1→2 + README 补新增依赖

## Context

### 为什么顺序是 1-0-2（已查实，附证据）

- **编号 0/1/2 按「项目阶段」编**（`docs/doc.md`）：阶段 0 环境与依赖（含步骤 0.2 配 API Key）
  → Gate 0；阶段 1 数据准备 → Gate 1；阶段 2 写代码 → Gate 2。所以编号顺序本是 0→1→2。
- **README / 我 TUI 的 1→0→2 是按「准备工作的叙述顺序」**（README 第 336–339 行：
  ① 环境 ② 数据 ③ 密钥），两句验收命令顺着写下来，未回头按编号校正。
- **两者技术上互不依赖**（import 证据）：`gate0_check.py` 只 import `openai`+`config`（只要密钥）；
  `gate1_check.py` 只 import `config`+`data`（只要数据，离线）。`results/env.json` 由 gate0 写，
  但 grep 全仓**没有任何脚本读它**。
- 唯一站得住的实质理由：Gate 0 会花钱（2 次真实调用），Gate 1 离线免费。

### 方案A（用户已选）

顺序改为 **env → secret → Gate 0 → data → Gate 1 → Gate 2 → run → score → pair**：
既符合编号 0→1→2，又让"只依赖密钥"的 Gate 0 尽早跑（便宜、快速暴露密钥与 tool-calling 问题），
不必先等 1.4 G 下载。

## 整体架构 / 流程

```mermaid
flowchart LR
    A["1 环境<br/>(检查)"] --> B["2 密钥<br/>(录入 .env)"]
    B --> C["3 ★Gate 0<br/>密钥+tool calling<br/>2次调用 几分钱"]
    C --> D["4 数据<br/>fetch_data.py<br/>~1.4G"]
    D --> E["5 ★Gate 1<br/>数据与起点<br/>离线 ~15s"]
    E --> F["6 ★Gate 2<br/>代码与埋点<br/>默认离线"]
    F --> G["7 跑组"]
    G --> H["8 判分"]
    H --> I["9 配对/分析"]
```

关键：Gate 0 从第 5 位提前到第 3 位，紧跟密钥；`data → Gate 1` 的依赖关系不变。

## 改动清单

### 1. `repro_tui.py`：只改 `STEPS` 元组顺序

`detect()` 返回的是 **dict（按 key 索引）**、`build_job()` 也按 key 分派、
`refresh_steps()` / `run_check()` 都用 `enumerate(STEPS)` 自动编号 ——
**没有任何逻辑依赖顺序**，所以只需把元组重排，编号会自动跟着变。

```python
STEPS = (
    Step("env",    "环境",      "conda 环境 + 5 个直接依赖", runnable=False),
    Step("secret", "密钥",      ".env 里的 DEEPSEEK_API_KEY（按 d 录入）"),
    Step("gate0",  "Gate 0",    "密钥 + tool calling（2 次真实调用，几分钱）"),
    Step("data",   "数据",      "data/dev_databases：11 库 / 11 sqlite"),
    Step("gate1",  "Gate 1",    "数据与起点验收（离线，~15 s）"),
    Step("gate2",  "Gate 2",    "代码与埋点验收（默认离线）"),
    Step("run",    "跑组",      "按选中的组跑（全量 500 题或冒烟）"),
    Step("score",  "判分",      "官方 execute_sql + calculate_ex"),
    Step("pair",   "配对/分析", "McNemar 配对检验 + 阶段 8 离线分析"),
)
```

**顺带清理**：`detect()` 里现有的 `# 1 环境` `# 2 密钥` `# 3 数据` `# 4/5/6 gate 类`
`# 7 跑组` `# 8 判分` `# 9 配对` 这些**按旧顺序写的编号注释会全部过期**，
改为按 key 命名的分组注释（不再带数字），避免下次再被误导。

### 2. `README.md`：环境配置补新增依赖

**（a）「快速开始」步骤 2** —— 主命令保持不变，另起一段可选的 TUI 依赖：

```bash
# 可选：复刻流程 TUI（repro_tui.py）。跑实验本身不需要它
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple textual==8.2.8
```

并加一句：`fetch_data.py` 只用标准库 + 已有的 `tqdm`，**不需要额外依赖**。

**（b）「依赖清单 · 直接依赖」** 在 `python-dotenv` 行之后插入
（不放到表尾，避免打断末尾"官方评测脚本依赖"三行的分组）：

| 包 | 版本 | 用途 |
| --- | --- | --- |
| `textual` | 8.2.8 | **复刻流程 TUI**（`repro_tui.py`）；跑实验本身不需要 |

**（c）「随附的关键间接依赖」** 表尾追加 7 行（全部由 textual 带入，本机 pip freeze 实测）：

| 包 | 版本 |
| --- | --- |
| `rich` | 15.0.0 |
| `pygments` | 2.21.0 |
| `platformdirs` | 4.11.12 |
| `markdown-it-py` | 4.2.0 |
| `mdit-py-plugins` | 0.6.1 |
| `linkify-it-py` | 2.2.0 |
| `mdurl` | 0.1.2 |

## 主要改动文件

| 文件 | 改动 |
| --- | --- |
| `repro_tui.py` | `STEPS` 元组重排 + `detect()` 分组注释去编号 |
| `README.md` | 安装命令补 textual；两张依赖表各补行 |

不改 `config.py` / `gate*.py` / `run.py` / `fetch_data.py` / `.gitignore`。

## 验证

| # | 验证 | 期望 |
| --- | --- | --- |
| 1 | `py_compile repro_tui.py` | 通过 |
| 2 | `--check` 输出 | 顺序为 `1 环境 / 2 密钥 / 3 Gate 0 / 4 数据 / 5 Gate 1 / 6 Gate 2 / 7 跑组 / 8 判分 / 9 配对` |
| 3 | 无头断言 `ol.get_option_at_index(2).id` | `"gate0"`（第 3 项确实是 Gate 0）、总条目 9 |
| 4 | 无头断言 `build_job` 各 key | `gate0` 命令仍为 `gate2_check.py`… 无关；`gate0` → `gate0_check.py`、`gate1` → `gate1_check.py`、`data` → `fetch_data.py` 不变 |
| 5 | README 代码块 bash 语法 | 抽出新增的 `pip install` 块过 `bash -n`，无语法错误 |
| 6 | README 行存在性 | `textual`、`rich`、`pygments` 等 8 个包名都能 grep 到 |
| 7 | `--check` 里「数据」步骤 | 仍显示 `○ dev_databases 缺失`（**符合现状**：你已重置数据，与本改动无关） |

> 注意：**不跑 `gate1_check.py` 作回归**——它现在必然 FAIL，因为你已删除
> `data/dev_databases`，那是与本改动无关的既有状态，跑了只会产生误导性输出。

## 明确不做（后续再说）

- **不改 README 的执行顺序表述**：正文「复现实验」仍是 `1→0`。本次只按用户要求动
  "环境配置"与 TUI；README 里那处顺序不一致留待你决定是否统一。
- **不新增 `requirements.txt`**：仓库本来就没有，README 是唯一事实源。
- **不补 `repro_tui.py` 的使用说明章节**（用户本次只要求"加上新增的库"）。
- **不修顺带发现的一处 README 失准**：README 写 `pymysql | 1.2.3`，本机实测装的是
  `2.2.8`（其余版本都对得上）。属既有文档问题，不在本次范围。

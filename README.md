# db_agent

在**冻结的 API 模型**（`deepseek-flash`，不训练）上，对比 one-shot 与 agent 闭环在
text-to-SQL 上的表现。数据集 BIRD **Mini-Dev V1**（SQLite，500 题 / 11 库 / 79 表），
判分复用官方 `execute_sql` + `calculate_ex`。

仓库只保留三组实验（**n = 500，同一批题、同一批次跑完，两两配对比较**）：

| 组 | 方法 | 温度 | 工具 | EX | 相对 O1 |
| --- | --- | --- | --- | --- | --- |
| `O1` | one-shot ×1 | 0.0 | — | 57.80% | 基准 |
| `O3` | one-shot 采样 ×3 + 按执行结果聚类 | 0.7 | — | 59.40% | +1.60pt，**不显著**（p=0.0963） |
| **`A1`** | **agent 循环 + 输出形态自检** | 0.0 | `run_sql` | **64.40%** | **+6.60pt，p=0.0007 ✅** |

**结论：agent 闭环 + 输出形态自检相对 one-shot 提升 +6.60pt EX，统计显著，
代价约为 8 倍 token。** 完整论证、代价表与剩余错误结构见
[`实验结论.md`](实验结论.md)（另一次独立运行为 +5.00pt / p=0.0059，方向与量级一致）。

> `A1 ≡ 历史文档中的 "A1v2"`。其余对照组（A2~A10）的代码、归档产物与结论章节
> 已随本次收敛移除。

---

本仓库使用 **conda** 管理 Python 版本，环境名为 `db_agent`。
conda 只负责提供一个隔离干净的 Python 解释器，第三方包统一用 **pip** 安装。

---

## 环境要求

| 项目 | 值 |
| --- | --- |
| 操作系统 | macOS 26.6.2（Apple Silicon / arm64） |
| conda | Miniforge3 `26.7.2`，安装于 `~/miniforge3` |
| Python | `3.12.14` |
| 环境路径 | `~/.conda/envs/db_agent` |
| 包管理器 | conda 管 Python，pip 管第三方包 |

---

## 仓库结构

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
│   │   └── metrics.py           #   列级召回 + 幻觉率（sqlglot 解析）
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
│       └── repro_tui.py         # 复刻流程傻瓜式 TUI（Textual 8.x）
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

> 根目录**不再有 `.py`**：16 个实现文件已迁入 `db_agent/` 下的
> `core / experiment / evaluation / ops / tui`，入口统一为 `python -m db_agent <子命令>`。

三条值得先知道的设计约束：

1. **起点冻结**：O 组与 A 组共用同一个 `build_context`，差别只在多出来的自变量。
2. **在线信号与离线判分分离**：agent 永远看不到答案对错，只用「执行成功/报错/几行几列」。
3. **判分链路唯一**：所有比较都走 `score.py` + `paired.py`，不手算、不跨批次比数字。

---

## 快速开始

### 1. 创建环境

```bash
conda create -n db_agent python=3.12 pip -y
```

> ⚠️ 末尾的 `pip` 不能省。conda-forge 的 python 包**默认不带 pip**，漏掉会在装包时报
> `No module named pip`。

### 2. 安装依赖

```bash
conda activate db_agent

pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  openai==3.16.2 \
  sqlglot==30.18.0 \
  pandas==3.0.6 \
  tqdm==4.70.1 \
  matplotlib==3.11.2 \
  python-dotenv==1.2.3
```

可选装**复刻流程 TUI**（`repro_tui.py`）——跑实验本身不需要它：

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple textual==8.2.8
```

> `fetch_data.py` 只用标准库 + 已装的 `tqdm`，**不需要额外依赖**。

想省掉每次都敲 `-i`，可以先固化镜像源（写入用户级 `~/.config/pip/pip.conf`）：

```bash
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3. 激活环境

```bash
conda activate db_agent
cd ~/db_agent
python your_script.py
```

退出环境：`conda deactivate`

---

## 数据准备（克隆仓库后必做）

数据集**不在版本库中**：`data/dev_databases/` 约 **1.4 GB**，提交进去不可逆且无必要。
clone 后必须先按本节重建，否则 `gate1_check.py` 会大面积 FAIL。

### 数据集口径

| 项 | 值 |
| --- | --- |
| 数据集 | BIRD **Mini-Dev**，SQLite 版 |
| 版本 | **V1 / 500 题**（非 V2 / 780 题） |
| 库数 | 11 |
| 难度分布 | simple 148 / moderate 250 / challenging 102 |
| gold 格式 | `mini_dev_sqlite_gold.sql`，制表符分隔，500 行 |

### 步骤 1 · 下载

```bash
cd ~/Downloads
curl -L -o minidev.zip "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
unzip -q minidev.zip
```

实测约 764 MB / 45 秒。解压后得到 `minidev/MINIDEV/`——**注意比手册多一层**
（手册说会多一层 `mini_dev_data/`，实际叫 `MINIDEV`）。

### 步骤 2 · 摆正到 `data/`

`config.py` 期望 `data/` 下**直接**是 `dev_databases/` 和 json，**不能多一层**：

```bash
cd ~/db_agent
mkdir -p data
cp ~/Downloads/minidev/MINIDEV/mini_dev_sqlite.json      data/
cp ~/Downloads/minidev/MINIDEV/mini_dev_sqlite_gold.sql  data/
cp ~/Downloads/minidev/MINIDEV/dev_tables.json           data/
cp -a ~/Downloads/minidev/MINIDEV/dev_databases          data/
```

### 步骤 3 · 生成官方脚本需要的 JSONL

官方 `--diff_json_path` 要 JSONL，而下载包给的是 JSON 数组：

```bash
~/.conda/envs/db_agent/bin/python - <<'EOF'
import json
d = json.load(open('data/mini_dev_sqlite.json', encoding='utf-8'))
with open('data/mini_dev_sqlite.jsonl', 'w', encoding='utf-8') as f:
    for i, x in enumerate(d):
        x['qidx'] = i                 # 唯一标识，见下方「已知陷阱」
        f.write(json.dumps(x, ensure_ascii=False) + '\n')
print('写出', len(d), '条')
EOF
```

### 步骤 4 · 官方评测脚本

仓库**不含数据库**，只有脚本和示例。评测脚本必须单独获取：

```bash
git clone --depth 1 https://github.com/bird-bench/mini_dev.git /tmp/bird_mini_dev
cp -r /tmp/bird_mini_dev/evaluation reference/evaluation
cp -r /tmp/bird_mini_dev/llm        reference/llm
rm -rf /tmp/bird_mini_dev
```

> ⚠️ **GitHub 走 HTTP/2 会报 `Error in the HTTP2 framing layer`**（实测）。加参数绕开：
> ```bash
> git -c http.version=HTTP/1.1 clone --depth 1 <url> <dir>
> ```

评测脚本的依赖**即使只跑 SQLite 也必须装**（`psycopg2`/`pymysql` 写在模块顶层，import 时即执行）：

```bash
pip install func_timeout psycopg2-binary pymysql
```

### 步骤 5 · 验收

```bash
~/.conda/envs/db_agent/bin/python -m db_agent gate 1
```

全部通过、退出码 0（具体项数以脚本输出为准；本项目收敛后已移除与 `desc_index`
相关的 2 项检查）。

### 已知陷阱（均已实测，`data.py` 已处理）

| 陷阱 | 后果 | 处理 |
| --- | --- | --- |
| **`question_id` 有重复值**（137/138 各 2 次） | 用它做断点续跑 key 会**漏跑 2 题**，最终只有 498 条 | 改用 `qidx`（0..499 下标） |

---

## 未纳入版本库的文件

`.gitignore` 屏蔽了以下路径。**clone 后它们都不存在**，需要按上表重建或重新生成。

| 路径 | 体积 | 屏蔽原因 | 如何恢复 |
| --- | --- | --- | --- |
| `.env` | 254 B | **含真实 API 密钥**，绝不提交 | `cp .env.example .env` 后填入密钥 |
| `data/dev_databases/` | **1.4 G** | 体积过大；可从官方重下 | 见「数据准备」步骤 1–2 |
| `data/*.sqlite` | — | 同上（备用规则） | 同上 |
| `results/` | 动态 | 运行产物，可随时重跑重生成 | 跑 `run.py` 产出 |
| `__pycache__/`、`*.py[cod]` | 动态 | Python 字节码，机器相关 | 自动生成 |
| `.vscode/`、`.idea/` | 小 | 编辑器配置，个人偏好 | 自行配置 |
| `.DS_Store` | 小 | macOS 目录元数据 | 系统自动生成 |
| `minidev.zip` | 764 M | 下载残留，解压后即可删 | 见「数据准备」步骤 1 |
| `*.log` | 动态 | 日志 | 自动生成 |

### 提交前自查

```bash
git check-ignore -v .env              # 有输出 = 已被正确忽略
git add -An | grep -E "dev_databases|\.env'"   # 应为空
```

> ⚠️ **`git add .` 之前务必确认 `data/dev_databases/` 仍被忽略**。
> 一旦把 1.4 GB 提交进去，历史里删不掉，只能重写历史。

### 版本化的数据文件（对照）

以下 `data/` 下的小文件**已提交**（合计约 800 KB），因为它们是实验口径的一部分：

| 文件 | 说明 |
| --- | --- |
| `data/mini_dev_sqlite.json` | 500 题原始题目 |
| `data/mini_dev_sqlite_gold.sql` | gold SQL，制表符分隔 |
| `data/mini_dev_sqlite.jsonl` | 官方评测脚本输入 |
| `data/dev_tables.json` | 数据集 schema 参考 |

---

## 密钥与配置

API 密钥通过**项目级 `.env` 文件**管理，不使用全局环境变量。`.env` 已被
`.gitignore` 屏蔽，不会进入版本库。

### 文件职责

| 文件 | 是否提交 | 说明 |
| --- | --- | --- |
| `.env` | ❌ 绝不提交 | 存放真实密钥，权限 `600` |
| `.env.example` | ✅ 可以提交 | 只有占位符，供他人复制参考 |
| `.gitignore` | ✅ 提交 | 已屏蔽 `.env` |

### 首次配置

```bash
cd ~/db_agent
cp .env.example .env
# 然后编辑 .env，填入真实密钥
```

`.env` 内容 —— **只放密钥**：

```ini
DEEPSEEK_API_KEY=sk-your-key-here
```

> **单一事实源原则**：模型名、`BASE_URL`、采样参数统一定义在 `config.py`，
> `.env` 只存密钥。两处都定义会导致「改了一处不生效」，极难排查。

### 代码里怎么读

密钥的读取**统一封装在 `config.py`**，业务代码不直接碰 `os.environ`：

```python
# config.py
API_KEY_ENV = "DEEPSEEK_API_KEY"
BASE_URL    = "https://api.deepseek.com"
MODEL       = "deepseek-flash"
TEMPERATURE = 0.0

def require_api_key():
    """取密钥；缺失时给出可操作的报错，而不是裸 KeyError"""
    k = os.environ.get(API_KEY_ENV, "").strip()
    if not k:
        raise RuntimeError(f"{API_KEY_ENV} 未设置，请在 {ROOT / '.env'} 写入")
    return k
```

`.env` 的加载也在 `config.py` 顶部完成（`override=False`，即**系统环境变量优先**），
因此任何 `import config` 的脚本都自动拿到密钥，**不需要在每个入口重复 `load_dotenv()`**：

```python
from openai import OpenAI
import config

client = OpenAI(api_key=config.require_api_key(), base_url=config.BASE_URL)

resp = client.chat.completions.create(
    model=config.MODEL,
    temperature=config.TEMPERATURE,
    max_tokens=config.MAX_TOKENS,
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

> ⚠️ **本项目必须显式关闭思考模式**（`config.THINKING = {"type": "disabled"}`）。
> 调用时要透传 `extra_body`，否则 `temperature` 静默失效、O3 采样机制不成立：
> ```python
> extra_body={"thinking": config.THINKING}
> ```
> 详见下方「模型参数说明」和 `config.py` 内注释。

### 模型参数说明（`deepseek-flash`）

| 参数 | 说明 |
| --- | --- |
| 上下文长度 | 1M |
| 最大输出 | **393216**（API 实测上限）；不设时默认 8K（非思考）/ 64K（思考） |
| 思考模式 | **默认开启**（`thinking.type = "enabled"`），`reasoning_effort` 默认 `high` |
| `temperature` | 取值 ≤ 2。**思考模式下不生效**，此时由 `top_p` 控制（有效区间 0.95–1.0） |
| `tool_choice` | 思考模式下**不支持 `required` 及指定具体工具**，会返回 400 |

> ⚠️ 三个容易踩的坑：
> 1. 思考模式下**推理 token 与正式输出共享 `max_tokens`**，推理写满额度会让正文
>    （含 `tool_call` 参数）被截断，`finish_reason` 返回 `length`。
>    本项目一律**关闭思考模式**（见下一条），故 `MAX_TOKENS` 保持 2048 即可。
> 2. 强制工具调用（`tool_choice="required"`）在思考模式下直接 400；
>    要么改用 `auto`，要么先 `thinking={"type": "disabled"}` 关掉思考模式。
> 3. **官方价格页把 MAX OUTPUT 写成 "384K"，与 API 实际接受的 393216 不一致。**
>    以 API 报错信息为准（发 400000 会返回
>    `the valid range of max_tokens is [1, 393216]`），别照抄价格页。

### 提交前自查：确认密钥未被提交

```bash
git check-ignore -v .env    # 有输出 = 已被正确忽略
git add -An                 # 确认 .env 不在待提交列表里
```

**切勿**把密钥写进 `config.py` 等源码文件，也不要 `export` 到 `~/.zshrc`——
源码和 shell 配置都可能被提交、备份或同步出去。

---

## 复现实验

按顺序执行即可。**只有第 3 步会花钱**（全实验约 ¥7）。

### 前置

```bash
cd ~/db_agent          # ← 换成你自己的仓库根目录（如本机是 test_hm/ECNU-DB-HOMEWORK）

# ① 环境 + 依赖（见「快速开始」，含官方评测脚本依赖 func_timeout/psycopg2-binary/pymysql）
conda activate db_agent

# ② 数据（见「数据准备」）：把 MINIDEV 摆到 data/，生成 .jsonl，clone 官方评测脚本
~/.conda/envs/db_agent/bin/python -m db_agent gate 1     # 数据准备验收，应全部通过

# ③ 密钥：.env 写入 DEEPSEEK_API_KEY=sk-...
~/.conda/envs/db_agent/bin/python -m db_agent gate 0     # 期望 Gate 0 通过
```

### 跑各组

本项目**只有三组**，且三组共用同一批 500 题，逐题配对比较：

| 组 | 方法 | 温度 | 工具 | 同批次结果 |
| --- | --- | --- | --- | --- |
| `O1` | one-shot ×1 | 0.0 | — | 57.80% |
| `O3` | one-shot 采样 ×3 + 按执行结果聚类 | 0.7 | — | 59.40%（p=0.0963，不显著） |
| **`A1`** | **agent 循环 + 输出形态自检** | 0.0 | `run_sql` | **64.40%（p=0.0007）** |

> **`O3` 只存在于这一批次**，因此三组的绝对数字**必须同批次重跑后才能互相比较**
> （另一次独立运行只有 O1/A1，见 `实验结论.md` §2.3）。

> **`A1 ≡ 历史文档中的 "A1v2"`**：`prompts.AGENT_SYSTEM` 第 1 步的「输出形态自检」
> 是唯一被证据支持的机制改进，因此**基线冻结**——改动措辞会毁掉与归档结果的
> 可比性（`gate2_check.py` 有守卫）。

```bash
# 三组都跑，然后判分 + 配对比较（O3 贵一档，可先只跑 O1/A1）
for G in O1 O3 A1; do ~/.conda/envs/db_agent/bin/python -m db_agent run --group $G; done
```

每组支持**断点续跑**（以 `qidx` 为 key，重复执行只补未完成部分）。

冒烟（固定取前 N 条）：

```bash
~/.conda/envs/db_agent/bin/python -m db_agent run --group A1 --limit 20 --out A1_smoke.jsonl
```

> **`--out` 很重要**：不加它，`--limit 20` 会去读 `<group>.jsonl` 的断点记录；
> 若该文件已是全量 500 条，前 20 题会被判定"已完成"从而**一题不跑**。

### 傻瓜式 TUI（覆盖上面全流程）

不想手敲命令时，用 `repro_tui.py` 的全屏仪表盘，把「验收 → 跑组 → 判分 → 配对」串成 9 步：

```bash
~/.conda/envs/db_agent/bin/python -m db_agent tui
```

界面：**左侧是 9 个步骤的状态与选项，右侧是子进程实时输出与进度条**。
子进程 stdout/stderr 原样流进右侧日志，tqdm 的 `\r` 进度被提出来喂给进度条。

| 按键 | 作用 |
| --- | --- |
| `↑` `↓` | 在左侧步骤列表中选择（选中即打印该步状态与说明） |
| `空格` | 勾选/取消分组（O1 / O3 / A1） |
| `d` | 执行当前选中的步骤（**唯一执行入口**，点选不会自动跑） |
| `r` | 刷新状态表 |
| `s` | 停止当前子进程（先 `TERM`，5 秒未退则 `KILL`） |
| `q` | 退出（重开自动接上，不需要重跑已完成步骤） |

步骤顺序已对齐「阶段编号 0→1→2」，**Gate 0 紧跟密钥、排在数据之前**——它只依赖 `.env`，
2 次真实调用就能暴露密钥 / tool-calling 问题，不必先等 1.4 GB 下载：

| # | 步骤 | 做什么 | 产物 / 状态判据 |
| --- | --- | --- | --- |
| 1 | 环境 | 检查 5 个直接依赖（只检查，不执行命令） | 依赖齐全 |
| 2 | 密钥 | 按 `d` 弹窗粘贴 `DEEPSEEK_API_KEY`，写入 `.env`（权限 600） | `.env` 可读到密钥 |
| 3 | Gate 0 | 密钥 + tool calling（2 次真实调用，几分钱） | `results/env.json` + 台账 |
| 4 | 数据 | 下载并摆正 Mini-Dev（11 库 / 11 sqlite） | `data/dev_databases` |
| 5 | Gate 1 | 数据与起点验收（离线，约 15 s） | 台账 |
| 6 | Gate 2 | 代码与埋点验收（默认离线；开关可含真实调用） | 台账 |
| 7 | 跑组 | 按勾选的分组跑 500 题（或 `limit` 冒烟） | `results/<group>[_smoke].jsonl` |
| 8 | 判分 | 官方 `execute_sql` + `calculate_ex` | `results/<group>*_scored.jsonl` |
| 9 | 配对/分析 | McNemar 配对检验 + 阶段 8 离线分析 | `results/.tui/paired_*.txt`、`results/analyze.md` |

**左侧控件**：`并发`（默认 20）、`limit`（留空 = 全量 500 题，填数字 = 冒烟并写独立的
`_smoke` 文件，不会污染全量断点）、`Gate 2 含真实 API 调用` 开关。
执行前会弹确认框给出**将执行的确切命令**；花钱的步骤（Gate 0、跑组）还给出预计成本
（O1 ¥0.4 / O3 ¥1.2 / A1 ¥5.3，冒烟按题数折算）。

> **状态判据**：有产物的步骤（数据 / 跑组 / 判分 / 配对）一律**读文件系统**判定，TUI 自己
> 不维护进度——所以关掉重开自动接上，`run.py` 的 `qidx` 断点续跑也自动生效；只有
> Gate 1 / Gate 2 这种「跑完什么都不留」的步骤才查台账 `results/.tui/state.json`，
> 删掉它只会让这两步显示「未运行」。

非交互场景（CI / 只想要状态表，不花钱）：

```bash
~/.conda/envs/db_agent/bin/python -m db_agent check                      # 打印 9 步状态表
printf 'sk-...' | ~/.conda/envs/db_agent/bin/python -m db_agent set-key   # 密钥从 stdin 读
```

> `check` / `set-key` 都不启动全屏界面。TUI 只认终端，检测到 stdio 非 tty 时会直接提示
> 改用上述命令，不会挂住。

### 配对比较（**不看这一步就会得出错误结论**）

**T=0 也不可复现**：同配置重跑，SQL 仅约 30% 相同，30 题里约 2 题 EX 翻盘。
所以**绝不能拿历史数字当对照**，必须同期重跑 + 配对检验：

```bash
P=~/.conda/envs/db_agent/bin/python
for G in O1 A1; do $P -m db_agent score --group $G; done
$P -m db_agent pair O1_scored.jsonl A1_scored.jsonl     # McNemar 精确检验 + 分层 + 翻盘题清单
```

### 判分

```bash
for G in O1 A1; do ~/.conda/envs/db_agent/bin/python -m db_agent score --group $G; done
```

产出 `results/<group>_scored.jsonl`，含 EX、列级召回、幻觉率、逐轮 EX@k。

### 验收

```bash
~/.conda/envs/db_agent/bin/python -m db_agent gate 1              # 数据与起点
~/.conda/envs/db_agent/bin/python -m db_agent gate 2 --offline    # 代码与埋点（不发 API）
~/.conda/envs/db_agent/bin/python -m db_agent gate 2              # 另跑 1 题 A1 做端到端校验
```

### 规模与耗时（实测）

| 项 | 值 |
| --- | --- |
| 样本 | 500 题 / 11 库 / 79 表 |
| 单组全量耗时 | **约 2 分钟**（并发 20） |
| 单组全量成本 | A1 ¥5.3（低谷价）；O1 约 ¥0.4 |
| 并发提示 | `--concurrency 20` 是安全值；模型侧限流 2500 并发 |

> 历史记录里「约 15 分钟（并发 5）」是早期用默认并发 5 跑出来的。
> 并发提到 20 后单组全量约 2 分钟——**时间瓶颈一直是并发，不是模型延迟**。

### 复现时最容易踩的坑

| 坑 | 症状 | 处理 |
| --- | --- | --- |
| `data/` 多一层目录 | `FileNotFoundError` | 见「数据准备」步骤 2 |
| 未装评测脚本依赖 | `ModuleNotFoundError: psycopg2` | `pip install func_timeout psycopg2-binary pymysql` |
| GitHub 拉不动 | `HTTP2 framing layer` 报错 | `git -c http.version=HTTP/1.1 clone ...` |
| `card_games` 打不开 | `unable to open database file` | 该库是 WAL 模式，`get_conn` 已内置 `immutable=1` 回退 |
| 用 `question_id` 做断点 key | 只跑出 498 条 | 已改用 `qidx`（`question_id` 有 2 个重复值） |
| 实验结论 | 见 [`实验结论.md`](实验结论.md) | — |

---

## 依赖清单

### 直接依赖

| 包 | 版本 | 用途 |
| --- | --- | --- |
| `python` | 3.12.14 | 运行时 |
| `openai` | 3.16.2 | LLM 调用 |
| `sqlglot` | 30.18.0 | SQL 解析 / 方言转换 |
| `pandas` | 3.0.6 | 数据处理 |
| `tqdm` | 4.70.1 | 进度条 |
| `matplotlib` | 3.11.2 | 绘图 |
| `python-dotenv` | 1.2.3 | 从 `.env` 读取密钥 |
| `textual` | 8.2.8 | **复刻流程 TUI**（`repro_tui.py`）；跑实验本身不需要 |
| `func_timeout` | 4.3.5 | **官方评测脚本依赖**，给 SQL 执行加超时 |
| `psycopg2-binary` | 2.9.13 | **官方评测脚本依赖**（postgres 分支；只跑 SQLite 也需装） |
| `pymysql` | 1.2.3 | **官方评测脚本依赖**（mysql 分支；只跑 SQLite 也需装） |

### 随附的关键间接依赖

| 包 | 版本 |
| --- | --- |
| `numpy` | 2.5.3 |
| `pydantic` | 2.13.5 |
| `httpx2` | 2.13.0 |
| `anyio` | 4.15.1 |
| `pillow` | 12.3.0 |
| `fonttools` | 4.65.0 |
| `rich` | 15.0.0 |
| `pygments` | 2.21.0 |
| `platformdirs` | 4.11.12 |
| `markdown-it-py` | 4.2.0 |
| `mdit-py-plugins` | 0.6.1 |
| `linkify-it-py` | 2.2.0 |
| `mdurl` | 0.1.2 |

> 后 7 行（`rich` 起）是由 `textual` 带入的，只在装了复刻流程 TUI 依赖时出现；
> 不装 `textual` 它们不会进入环境。

---

## 常用命令

```bash
conda env list                  # 列出所有环境
conda activate db_agent         # 进入环境
conda deactivate                # 退出环境
conda list -n db_agent          # 查看环境内 conda 包
pip list --not-required         # 查看顶层 pip 包
conda env remove -n db_agent    # 删除环境
```

---

## 注意事项

### 1. conda-forge 的 python 默认不带 pip

建环境时务必显式加上 `pip`：

```bash
conda create -n <环境名> python=3.12 pip -y
```

若已建好才发现缺 pip，补救方式：

```bash
conda install -n <环境名> pip -y
```

### 2. pandas 为 3.x，存在破坏性变更

当前为 `3.0.6`，相对 2.x 有一批不兼容改动（默认 Copy-on-Write、部分 `apply`
与字符串 API 行为收紧等）。参考 2.x 时代的教程或代码时若遇到异常行为，先怀疑
版本差异；确需降级可执行：

```bash
pip install "pandas<3"
```

### 3. matplotlib 中文显示为方块

matplotlib 默认字体不含中文，图内中文标题与标签会渲染成 `□□□`。
macOS 自带 PingFang SC 等中文字体，在脚本开头加两行即可修复：

```python
import matplotlib

matplotlib.rcParams["font.sans-serif"] = ["PingFang SC", "Arial Unicode MS", "Heiti SC"]
matplotlib.rcParams["axes.unicode_minus"] = False   # 负号同样需要，勿漏
```

`axes.unicode_minus = False` 管的是负号，只设字体的话负号仍会是方块。

### 4. 不要对同一个包混用 conda install 和 pip install

- **LangChain / openai 等纯 Python 生态的包一律用 pip** —— conda-forge 上要么没有，要么版本严重滞后。
- 只有编译型底层库（如 `faiss`、CUDA 版 `pytorch`）才考虑用 conda 装。
- 在环境内优先用 `python -m pip` 而非裸 `pip`，确保装进当前环境。

### 5. 每个项目独立环境

不要把项目依赖装进 `base`。本机已关闭 base 自动激活，未激活环境时 `python3`
仍指向系统自带的 `/usr/bin/python3`（3.9.6），属预期行为。

### 6. conda 已配置清华镜像

`~/.condarc` 中 conda-forge 渠道已指向清华镜像，实测约 7.8 MB/s（官方源约
0.89 MB/s）。相关配置：

```yaml
channels:
  - conda-forge
custom_channels:
  conda-forge: https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud
channel_priority: strict
auto_activate: false
envs_dirs:
  - /Users/wangwenkai/.conda/envs
```

---

## 环境卸载

```bash
conda env remove -n db_agent
```

如需连同 conda 一起卸载：

```bash
rm -rf ~/miniforge3 ~/.conda ~/.condarc
```

并删除 `~/.zshrc` 中 `# >>> conda initialize >>>` 至 `# <<< conda initialize <<<`
之间的整段内容。

# DB Agent 实验手册（0 → 1 完整版）

**怎么用**：每一节 = 【做什么】【代码】【怎么验收】。按顺序做，**每个 Gate 不过就停下修**。
本手册中所有文件名、目录层级、命令、baseline 数值均已对官方仓库核实，未核实的项会明确标注。

---

# 第一部分 · 设计与约定

## 1.1 目标

课程作业。在同一 LLM 下，对比 **agent 闭环机制**与 one-shot 生成在 text-to-SQL 上的表现，并量化"每个工具贡献了多少"。

- 数据集：**BIRD Mini-Dev**，SQLite 版
- 模型：DeepSeek API，不训练、不本地部署（500 题约 6 元）
- 不用 LangChain（技术含量在 agent 循环本身，要能讲清自己实现了什么）

## 1.2 五组对照

| 组 | 方法 | 温度 | 工具 | 回答什么问题 |
|---|---|---|---|---|
| `O1` | one-shot ×1 | 0.0 | 无 | 基准 |
| `O3` | one-shot 并行采样 ×3 + **按执行结果投票** | **0.7** | 无 | **回灌迭代值不值**；cost 基准线 |
| `A1` | agent 循环 | 0.0 | `run_sql` | 回灌迭代的净贡献（vs O3） |
| `A2` | agent 循环 | 0.0 | + `get_column_values` | 值域探测的增量 |
| `A3` | agent 循环 | 0.0 | + `get_column_desc` | 列释义的增量 |

**四个关键差值**：`A1−O3`（回灌迭代）· `A2−A1`（值域获取）· `A3−A2`（语义获取）· `A3−O1`（总增量）。

**O3 温度为什么不同**：T=0 时同一 prompt 采样三次会得到三条完全相同的 SQL，投票无意义，O3 退化成 O1。**这是 O3 机制的内在要求，报告里必须写明。**

## 1.3 三条不可违反的原则

**① 起点冻结**：O 和 A 共享同一个 `build_context()` 产出，逐字相同——全量 schema、同样给 `evidence`、同样不给 few-shot。差异只在 §1.2 那张表里。

**② 在线信号 vs 离线判分严格分离**：agent 只能看到「执行成功 / 报错 / 返回几行」；「结果对不对」只有评测代码能看到。**agent 永远不知道答案对不对**，gold 一旦泄漏进循环，实验作废。

**③ 判分链路唯一**：O 和 A 走**同一个执行函数 + 同一份官方评测脚本**。不要自写比对（官方 EX 用的是 `set(pred) == set(gt)`，会忽略列序与重复行——自写极易不一致）。

## 1.4 已冻结的参数

| 项 | 值 | 说明 |
|---|---|---|
| 样本量 | 全量（见 §2.2 的坑） | 全量覆盖三层难度，分层统计可靠 |
| evidence | **给** | 与官方 baseline 口径一致 |
| `MAX_STEPS` | 3 | **上限而非固定轮数**，成功即停（EX@k 曲线的前提） |
| `K_CANDIDATES` | 3 | 与 MAX_STEPS 对齐 |
| 温度 | 0.0 / O3 用 0.7 | 见 §1.2 |

## 1.5 五指标

| 指标 | 定义 | 采集 |
|---|---|---|
| **EX** | 官方脚本判定结果集一致 | `reference/evaluation/evaluation_ex.py` |
| **Latency** | 端到端墙钟，**报 P50/P95/P99** | 计时器包住整题 |
| **QET** | **仅** SQL 在库上的执行耗时 | 执行前后计时 |
| **列级召回** | `\|生成列 ∩ gold列\| / \|gold列\|` | sqlglot 解析 |
| **幻觉率** | 引用白名单外表/列的比例 | 复用白名单 |

**Latency 与 QET 必须分开报**：Latency 高 = agent 结构代价（多轮）；QET 高 = 生成的 SQL 变差（全表扫、多余 join）。两者可背离——agent 可能"更慢但更正确"。若 `EX +12 点` 且 `QET +40%`，结论即"**agent 用执行效率换准确率**"。

**幻觉率天然对 agent 有利**（执行报错会纠正幻觉），是机制结果、**不是独立指标**，报告要说明因果。

## 1.6 异常样本处理

| 情况 | 处理 |
|---|---|
| API 重试 3 次仍失败 | 标 `api_error`，**从 EX 分母剔除**，报告说明剔除数 |
| SQL 超时 / 超行 | 标 `timeout`，**计 EX=0 不剔除**（方法缺陷） |
| 用尽 MAX_STEPS | 返回最后一轮 SQL，计 EX=0 |
| 抽不出 SQL | 计 EX=0，归 `parse_error` |

---

# 第二部分 · 从零开始

## 阶段 0 · 环境与依赖（~40 min）

### 步骤 0.1 装 Python 与依赖

```bash
brew install python@3.12       # 不要用 macOS 自带的 3.9（已 EOL，且不应往上装包）

mkdir -p db_agent && cd db_agent
/opt/homebrew/bin/python3.12 -m venv .venv && source .venv/bin/activate

pip install openai sqlglot pandas tqdm matplotlib
mkdir -p data results reference
```

**官方要求**（来自仓库 badge）：Python **3.11+**、SQLite **3.41+**、OpenAI SDK **1.30+**。

**版本组合**（2026-09 本机实测）：python 3.12.14 / openai 3.16.2 / sqlglot 30.18.0 / pandas 3.0.6 / tqdm 4.70.1。

### 步骤 0.2 配 API Key

去 **`platform.deepseek.com`** 注册 → 充值 → 「API keys」页面创建 key（形如 `sk-...`）。

```bash
echo 'export DEEPSEEK_API_KEY="sk-你的key"' >> ~/.zshrc && source ~/.zshrc
```

### 步骤 0.3 写 `config.py`

> ⚠️ 下面两个文件名**已对官方仓库核实过**，不要改。

```python
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------- 路径（解压后把 data/ 指向 mini_dev_data/ 的内容，见阶段 1）----------
DATA_DIR       = ROOT / "data"                 # 里面应直接是 dev_databases/ 和 *.json
DB_DIR         = DATA_DIR / "dev_databases"
QUESTIONS_FILE = DATA_DIR / "mini_dev_sqlite.json"
DIFF_JSONL     = DATA_DIR / "mini_dev_sqlite.jsonl"     # 给官方脚本用，见步骤 1.5
GOLD_FILE      = DATA_DIR / "mini_dev_sqlite_gold.sql"  # ⚠️ 是 sqlite_gold，不是 sql_gold
RESULTS_DIR    = ROOT / "results"
EVAL_DIR       = ROOT / "reference" / "evaluation"      # 官方评测脚本

# ---------- 模型 ----------
API_KEY_ENV = "DEEPSEEK_API_KEY"
BASE_URL    = "https://api.deepseek.com"
MODEL       = "deepseek-v4-flash"   # ⚠️ 去控制台复制带日期的完整版本号，别用会漂移的别名
MAX_TOKENS  = 1024

TEMPERATURE          = 0.0    # O1 / A1 / A2 / A3
TEMPERATURE_SAMPLING = 0.7    # O3 专用

# ---------- 实验参数（冻结）----------
MAX_STEPS    = 3
K_CANDIDATES = 3
SMOKE_N      = 20

# ---------- 执行沙箱 ----------
RESULT_ROW_LIMIT      = 100
QUERY_TIMEOUT_SEC     = 5
TOOL_OUTPUT_MAX_CHARS = 2000
VALUE_SAMPLE_LIMIT    = 20

# ---------- 重试与并发 ----------
API_MAX_RETRIES  = 3
API_BACKOFF_BASE = 2
CONCURRENCY      = 5
```

### 步骤 0.4 验收 → Gate 0

```python
# gate0_check.py
import os, sqlite3, sys, json
from openai import OpenAI
import config

dev = {"python": sys.version, "sqlite": sqlite3.sqlite_version, "model": config.MODEL}
config.RESULTS_DIR.mkdir(exist_ok=True)
json.dump(dev, open(config.RESULTS_DIR / "env.json", "w"), ensure_ascii=False, indent=2)
print(dev)
assert sqlite3.sqlite_version_info >= (3, 41), "SQLite 需 >= 3.41"

c = OpenAI(api_key=os.environ[config.API_KEY_ENV], base_url=config.BASE_URL)

r = c.chat.completions.create(          # ① usage 分项字段
    model=config.MODEL, temperature=0,
    messages=[{"role": "user", "content": "say ok"}],
)
print("usage 字段:", list(r.usage.model_dump().keys()))

r2 = c.chat.completions.create(          # ② tool calling（整个 A 组的地基）
    model=config.MODEL, temperature=0,
    messages=[{"role": "user", "content": "orders 表有多少行？"}],
    tools=[{"type": "function", "function": {
        "name": "run_sql", "description": "执行 SQL 查询",
        "parameters": {"type": "object",
            "properties": {"sql": {"type": "string"}}, "required": ["sql"]}}}],
)
print("tool_calls:", r2.choices[0].message.tool_calls)
```

**Gate 0 通过标准**：

1. `usage 字段` 里有 **`prompt_cache_hit_tokens`** 和 **`prompt_cache_miss_tokens`** → 拿不到则成本分项补不回来，**别往下走**
2. `tool_calls` **不是 `None`** 且 `function.name == "run_sql"` → 不过则 A 组做不了

> openai SDK 3.x 是大版本跳跃（官方主推 Responses API，DeepSeek 走 `chat.completions`），**不可假定兼容，必须实测**。

---

## 阶段 1 · 数据准备（~1.5 h）

### 步骤 1.1 下载

**两条路，选一条。**

**路线 A —— 官方 zip（推荐，最简单）**：

```bash
cd ~/db_agent
curl -L -o minidev.zip "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
unzip -q minidev.zip
```

**路线 B —— Hugging Face**：

```bash
pip install huggingface_hub
hf download birdsql/bird_mini_dev --repo-type dataset --local-dir hf_download
```

> 仓库徽章给的官方下载入口是 zip（`bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip`），Hugging Face 是同源镜像。官网另有一个 Google Drive 备用链接。

**评测脚本单独下**（仓库里**没有**数据库，只有脚本和示例）：

```bash
cd ~/db_agent
git clone --depth 1 https://github.com/bird-bench/mini_dev.git /tmp/bird_mini_dev
cp -r /tmp/bird_mini_dev/evaluation reference/evaluation
cp -r /tmp/bird_mini_dev/llm       reference/llm        # 里面含 gold SQL
```

### 步骤 1.2 摆正目录（关键，最容易错）

下载包解出来是 `mini_dev_data/`。`config.py` 期望 `data/` 下**直接**是 `dev_databases/` 和 json，**不要多一层**：

```bash
cd ~/db_agent
rm -rf data && mkdir -p data
cp -r mini_dev_data/* data/                      # 把里面的内容拷进来，而不是拷目录本身
cp reference/llm/mini_dev_data/mini_dev_sqlite_gold.sql data/

# 摆好后的结构应该是：
# data/
#   mini_dev_sqlite.json          ← 题目
#   mini_dev_sqlite_gold.sql      ← gold SQL
#   dev_databases/<db_id>/<db_id>.sqlite
#   dev_databases/<db_id>/database_description/*.csv
```

```bash
ls data/                          # 应看到 dev_databases/ + mini_dev_sqlite.json + gold.sql
ls data/dev_databases | wc -l     # 期望 11
ls data/dev_databases/california_schools/   # 应有 california_schools.sqlite + database_description/
```

> **⚠️ 坑一：V2 已更新到 780 题。** 官方 2025-07-22 起 Mini-Dev V2 = 原 500 题（11 库，SELECT-only）+ 270 道新题（18 个新库，含 CRUD 与 JSON 操作）。你下到的可能是 780 条。
> ```bash
> python -c "import json;d=json.load(open('data/mini_dev_sqlite.json'));print(len(d));print(sorted({x['db_id'] for x in d}))"
> ```
> **跑之前先看这个数字**：若为 780，你的实验规模是 780 而非 500（分层统计按实际数量算即可，不影响设计）。
>
> **⚠️ 坑二：V2 的 MySQL 版尚未发布**（官方 README 明确写了仍在开发）。若将来要用 MySQL，只能用旧版 500 题的 MySQL 数据。

### 步骤 1.3 装官方评测脚本的依赖 ⚠️

```bash
grep -hE "^\s*(import|from) " reference/evaluation/*.py | sort -u
```

**你会看到 `psycopg2` 和 `pymysql`——即使你只跑 SQLite 也必须装**，因为它们写在 `evaluation_utils.py` 的模块顶层，import 时就执行：

```bash
pip install func_timeout psycopg2-binary pymysql
```

```bash
cd reference/evaluation
python -c "from evaluation_utils import execute_sql; print('官方脚本依赖 OK')"
```

> 不装的话，阶段 5 跑评测会直接 `ModuleNotFoundError`。

### 步骤 1.4 人工探查 `evidence` 与 `database_description`

```bash
python - <<'EOF'
import json
d = json.load(open('data/mini_dev_sqlite.json'))
if isinstance(d, dict): d = list(d.values())
print("总数", len(d), "｜evidence 为空", sum(1 for x in d if not str(x.get('evidence','')).strip()))
print("字段:", sorted(d[0].keys()))
for x in d[:3]:
    print("-", x.get('question')[:80]); print("  evidence:", repr(x.get('evidence'))[:160])
EOF

# 列释义文件结构 —— 重点看 value_description 有没有内容
head -3 data/dev_databases/california_schools/database_description/*.csv
```

> **决策点**：若 `value_description` 基本为空、且 desc 与 evidence 重叠严重 ⇒ `A3 − A2` 大概率无增量，**此时砍掉 A3，省一轮全量**。在跑之前判断。

### 步骤 1.5 生成官方脚本需要的 `.jsonl`

官方 `--diff_json_path` 要的是 **JSONL**（`evaluation_utils.load_jsonl` 逐行 `json.loads`），而下载包里是 `.json`。若是 JSON 数组就先转换：

```bash
python - <<'EOF'
import json
d = json.load(open('data/mini_dev_sqlite.json'))
if isinstance(d, dict):                       # HF datasets 有时给 dict
    d = list(d.values())
with open('data/mini_dev_sqlite.jsonl', 'w', encoding='utf-8') as f:
    for x in d:
        if 'difficulty' not in x:             # 官方按 difficulty 分层统计，缺了会报错
            x['difficulty'] = 'moderate'
        f.write(json.dumps(x, ensure_ascii=False) + '\n')
print("写出", len(d), "条 → data/mini_dev_sqlite.jsonl")
EOF
```

> 若文件本来就是 JSONL（`head -c 200` 看首字符是不是 `{`），跳过这步，只改名或直接指向它。
> **如果 `difficulty` 字段缺失**，官方 `compute_acc_by_diff` 会 `KeyError`——上面脚本给了兜底，但**请先确认原文有没有这个字段**，别让兜底掩盖数据问题。

### 步骤 1.6 写 `data.py`

```python
import json, csv, sqlite3
from functools import lru_cache
import config

def load_questions():
    d = json.load(open(config.QUESTIONS_FILE, encoding="utf-8"))
    if isinstance(d, dict):
        d = list(d.values())
    for i, x in enumerate(d):
        x.setdefault("question_id", i)       # 用下标当 id，与官方 predict json 的 key 对齐
    return d

def db_path(db_id):
    return config.DB_DIR / db_id / f"{db_id}.sqlite"

def get_conn(db_id):
    return sqlite3.connect(f"file:{db_path(db_id)}?mode=ro", uri=True)

@lru_cache(maxsize=None)
def schema_whitelist(db_id):
    """{table: set(cols)} —— 工具参数校验 + 幻觉检测"""
    conn = sqlite3.connect(f"file:{db_path(db_id)}?mode=ro", uri=True)
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')}
        if cols:
            out[name] = cols
    conn.close()
    return out

@lru_cache(maxsize=None)
def schema_text(db_id):
    """真实 DDL。sorted 保证确定性 —— Gate 1 会验"""
    conn = sqlite3.connect(f"file:{db_path(db_id)}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
    ).fetchall()
    conn.close()
    return "\n".join(sorted(r[0] for r in rows))

@lru_cache(maxsize=None)
def desc_index(db_id):
    """{(table, col): (column_description, value_description)}"""
    idx, ddir = {}, config.DB_DIR / db_id / "database_description"
    if not ddir.exists():
        return idx
    for f in sorted(ddir.glob("*.csv")):
        with open(f, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                col = (row.get("original_column_name") or row.get("column_name") or "").strip()
                if col:
                    idx[(f.stem, col)] = (
                        (row.get("column_description") or "").strip(),
                        (row.get("value_description") or "").strip(),
                    )
    return idx

def build_context(db_id, evidence):
    """O 与 A 共用的起点，必须逐字相同"""
    ev = (evidence or "").strip() or "None"
    return f"Database schema:\n{schema_text(db_id)}\n\nExternal knowledge:\n{ev}\n\n"
```

> `desc_index` 里假设的 CSV 列名（`original_column_name` / `column_name` / `column_description` / `value_description`）**未逐一核实**。步骤 1.4 的 `head` 输出会告诉你真实列名，对不上就改这里。

### 步骤 1.7 验收 → Gate 1

```python
from data import build_context, schema_whitelist, load_questions

qs = load_questions()
print("题目数:", len(qs))
print("db_id 列表:", sorted({q['db_id'] for q in qs}))

q = qs[0]
ctx = build_context(q["db_id"], q.get("evidence"))
assert ctx == build_context(q["db_id"], q.get("evidence")), "build_context 不确定！"
print("确定性 OK")
print(ctx[:600])
print("该库表数:", len(schema_whitelist(q["db_id"])))
```

**Gate 1 通过标准**：

1. `db_id 列表` 是 11 个库（V2 则 29 个）
2. 确定性断言通过
3. 肉眼：schema 有表名/列名/类型；evidence 已拼上（空则显示 `None`）
4. `schema_whitelist` 表数 > 0

**不过怎么办**：确定性失败 ⇒ 排查 `set` 迭代顺序、时间戳、随机数混入。

---

## 阶段 2 · 写代码（~3 h）

**按顺序写，每写完一个跑它的验收再写下一下。**

### 步骤 2.1 `db.py`

```python
import time, sqlite3
import config

class QueryTimeout(Exception):
    pass

  def execute(conn, sql, row_limit=None, timeout=None):
    """返回 {rows, qet, truncated}；失败抛原始异常（QueryTimeout 单列）"""
    row_limit = row_limit or config.RESULT_ROW_LIMIT
    timeout   = timeout   or config.QUERY_TIMEOUT_SEC

    deadline = time.time() + timeout
    # SQLite 没有原生查询超时，用 progress handler 打断
    conn.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10_000)

    t0 = time.perf_counter()
    try:
        rows = conn.execute(sql).fetchmany(row_limit + 1)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e).lower():
            raise QueryTimeout(f"查询超过 {timeout}s 被中断") from e
        raise
    finally:
        qet = time.perf_counter() - t0        # 成功失败都记
        conn.set_progress_handler(None, 0)

    truncated = len(rows) > row_limit
    return {"rows": rows[:row_limit], "qet": qet, "truncated": truncated}
```

**验收**：

```python
from db import execute, QueryTimeout
from data import get_conn, load_questions
db_id = load_questions()[0]["db_id"]
conn = get_conn(db_id)
tbl = next(iter(__import__('data').schema_whitelist(db_id)))

r = execute(conn, f'SELECT * FROM "{tbl}" LIMIT 5')
print("行数", len(r["rows"]), "QET", round(r["qet"], 4), "截断", r["truncated"])
assert r["qet"] > 0

try:      # 笛卡尔积触发超时
    execute(conn, f'SELECT count(*) FROM "{tbl}" a, "{tbl}" b, "{tbl}" c, "{tbl}" d')
except QueryTimeout as e:
    print("超时 OK:", e)
```

**标准**：正常查询返回行数与正的 QET；笛卡尔积抛 `QueryTimeout`。

---

### 步骤 2.2 `llm.py`

```python
import os, re, time
from openai import OpenAI
import config

_client = OpenAI(api_key=os.environ[config.API_KEY_ENV], base_url=config.BASE_URL)

def chat(messages, tools=None, temperature=None):
    """返回 (message, meta)。meta 含【每次调用单独计时】与 token 分项"""
    temperature = config.TEMPERATURE if temperature is None else temperature
    last_err = None

    for attempt in range(config.API_MAX_RETRIES):
        kwargs = dict(model=config.MODEL, messages=messages,
                      temperature=temperature, max_tokens=config.MAX_TOKENS)
        if tools:
            kwargs["tools"] = tools

        t0 = time.perf_counter()
        try:
            resp = _client.chat.completions.create(**kwargs)
        except Exception as e:                      # 限流 / 网络
            last_err = e
            time.sleep(config.API_BACKOFF_BASE ** attempt)
            continue
        call_latency = time.perf_counter() - t0     # ← 每次调用单独计时

        u = resp.usage                              # ← token 分项，事后补不回来
        meta = dict(
            call_latency=call_latency,
            prompt_tokens=getattr(u, "prompt_tokens", 0),
            completion_tokens=getattr(u, "completion_tokens", 0),
            cache_hit_tokens=getattr(u, "prompt_cache_hit_tokens", 0),
            cache_miss_tokens=getattr(u, "prompt_cache_miss_tokens", 0),
        )
        return resp.choices[0].message, meta

    raise RuntimeError(f"API 重试 {config.API_MAX_RETRIES} 次仍失败: {last_err}")

def extract_sql(text):
    """抽 SQL；抽不到返回 None（计 EX=0，归 parse_error）"""
    if not text:
        return None
    for pat in (r"```sql\s*(.+?)```", r"```\s*(.+?)```"):
        m = re.search(pat, text, re.S | re.I)
        if m:
            return m.group(1).strip().rstrip(";")
    m = re.search(r"(?is)\b(SELECT|WITH)\b.+", text)
    return m.group(0).strip().rstrip(";") if m else None
```

**验收**：

```python
from llm import chat, extract_sql
msg, meta = chat([{"role": "user", "content": "写一条 SQLite 查询：统计 orders 表行数"}])
print(repr(msg.content))
print(meta)
for k in ("call_latency", "prompt_tokens", "completion_tokens",
          "cache_hit_tokens", "cache_miss_tokens"):
    assert k in meta, f"缺 {k}"
print("抽出:", extract_sql(msg.content))
```

**标准**：五个字段齐全（cache 两项为 0 也算通过）；能抽出 SQL。

---

### 步骤 2.3 `prompts.py`

```python
import config

AGENT_SYSTEM = """You are a SQLite expert working with a live database. You can call tools to
inspect the database and to validate your queries before answering.

{context}
Your task: produce a SQL query that correctly answers the user's question.

Workflow:
1. If anything is unclear — what a column means, what values a column actually stores,
   whether a term in the question matches the schema — call the appropriate tool
   to check BEFORE writing the final SQL.
2. Validate your query by calling run_sql.
3. If run_sql returns an error, read the message carefully, fix the query, and run it again.
4. If run_sql succeeds but returns 0 rows, that may mean a wrong filter value or a
   wrong join condition. Reconsider before finishing.
5. You have at most {max_steps} attempts. When you are done, output the final SQL
   in a ```sql fenced block.
"""

ONESHOT = """You are a SQLite expert. Write a single SQL query that answers the question.

{context}Question: {question}

Rules:
- Use SQLite syntax only.
- Use the exact table and column names given above. Do not invent names.
- Return ONLY the SQL query. No explanation, no markdown fences.
"""

def oneshot_prompt(context, question):
    return ONESHOT.format(context=context, question=question)

def agent_system(context):
    return AGENT_SYSTEM.format(context=context, max_steps=config.MAX_STEPS)
```

**验收**：

```python
from prompts import oneshot_prompt, agent_system
from data import build_context, load_questions
q = load_questions()[0]
ctx = build_context(q["db_id"], q.get("evidence"))
o, a = oneshot_prompt(ctx, q["question"]), agent_system(ctx)
assert ctx in o and ctx in a, "O/A 起点不一致！"
print("起点一致 OK"); print(o[:400])
```

**标准**：断言通过；O 无工具说明、A 有。

---

### 步骤 2.4 `tools.py`

```python
import config
from db import execute, QueryTimeout
from data import schema_whitelist, desc_index

TOOLS = [
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "在数据库上执行一条 SQL 查询并返回结果状态。"
                       "写好的 SQL 必须先用这个工具执行验证，不要直接给出未经执行的答案。"
                       "如果返回错误信息，请仔细阅读并修正查询后重新执行。",
        "parameters": {"type": "object",
            "properties": {"sql": {"type": "string", "description": "要执行的 SQLite 查询语句"}},
            "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "get_column_values",
        "description": "查看某一列在数据库中实际存储的取值（去重后的前若干个）。"
                       "当你不确定用户提到的业务名词（地区名、状态、类别、产品名等）"
                       "在数据库里具体存成什么字符串或编码时，用这个工具先确认。"
                       "这可以避免 WHERE 条件里写错值，导致查询返回 0 行却看不出问题。",
        "parameters": {"type": "object",
            "properties": {
                "table":  {"type": "string", "description": "表名，必须来自上面给出的 schema"},
                "column": {"type": "string", "description": "列名，必须来自上面给出的 schema"}},
            "required": ["table", "column"]}}},
    {"type": "function", "function": {
        "name": "get_column_desc",
        "description": "查看某个表某一列的官方说明文档，包含该列的业务含义与取值编码解释。"
                       "当列名是缩写、含义不明确（例如 Tot_liab、crt_dt 这类），"
                       "或你需要确认某字段代表什么业务指标时使用。",
        "parameters": {"type": "object",
            "properties": {
                "table":  {"type": "string", "description": "表名"},
                "column": {"type": "string", "description": "列名"}},
            "required": ["table", "column"]}}},
]

def _cut(s):
    return s[:config.TOOL_OUTPUT_MAX_CHARS]

def _run_sql(conn, sql):
    try:
        r = execute(conn, sql)
    except QueryTimeout as e:
        return _cut(f"执行失败：{e}"), None
    except Exception as e:
        return _cut(f"执行失败：{e}"), None     # 报错原样回灌，模型多半能自己修
    tail = "（结果被截断）" if r["truncated"] else ""
    return _cut(f"执行成功，返回 {len(r['rows'])} 行{tail}。预览：{r['rows'][:3]}"), r["qet"]

def _col_values(conn, db_id, table, column):
    wl = schema_whitelist(db_id)
    if table not in wl or column not in wl[table]:
        return _cut(f"错误：{table}.{column} 不存在，请从 schema 里选择"), None
    r = execute(conn, f'SELECT DISTINCT "{column}" FROM "{table}" LIMIT {config.VALUE_SAMPLE_LIMIT}')
    vals = [row[0] for row in r["rows"]]
    return _cut(f"{table}.{column} 的取值样例（{len(vals)} 个）：{vals}"), r["qet"]

def _col_desc(conn, db_id, table, column):
    d, v = desc_index(db_id).get((table, column), ("", ""))
    if not d and not v:
        return f"没有 {table}.{column} 的说明文档", None
    return _cut(f"{table}.{column} 说明：{d}\n取值说明：{v}"), None

def dispatch(conn, db_id, name, args):
    """返回 (文本, qet)"""
    if name == "run_sql":
        return _run_sql(conn, args.get("sql", ""))
    if name == "get_column_values":
        return _col_values(conn, db_id, args.get("table", ""), args.get("column", ""))
    if name == "get_column_desc":
        return _col_desc(conn, db_id, args.get("table", ""), args.get("column", ""))
    return f"未知工具：{name}", None

def tools_for(group):
    return {"A1": TOOLS[0:1], "A2": TOOLS[0:2], "A3": TOOLS[0:3]}.get(group)
```

> **`run_sql` 成功时只回行数+预览，不回完整结果**——这样 A1 拿到的仅是需要「验证信号」，而不是数据内容。想拿数据内容必须调 `get_column_values`，**保持 `A1−O3`（验证）与 `A2−A1`（值域获取）两层差异的干净**。报告要披露这一点。

**验收**：

```python
from tools import dispatch, tools_for
from data import get_conn, load_questions, schema_whitelist
db_id = load_questions()[0]["db_id"]
conn = get_conn(db_id)
tbl, col = next((t, sorted(c)[0]) for t, c in schema_whitelist(db_id).items() if c)

print(dispatch(conn, db_id, "run_sql", {"sql": f'SELECT * FROM "{tbl}" LIMIT 3'}))
print(dispatch(conn, db_id, "run_sql", {"sql": "SELECT * FROM nope"}))
print(dispatch(conn, db_id, "get_column_values", {"table": tbl, "column": col}))
print(dispatch(conn, db_id, "get_column_values", {"table": tbl, "column": "fake_col"}))
print(dispatch(conn, db_id, "get_column_desc", {"table": tbl, "column": col}))
assert len(tools_for("A1")) == 1 and len(tools_for("A3")) == 3
```

**标准**：都不抛异常；错的表/列名返回**友好错误字符串**；`get_column_values` 返回真实取值。

---

### 步骤 2.5 `methods.py`

```python
import json
import config
from llm import chat, extract_sql
from prompts import oneshot_prompt, agent_system
from tools import dispatch
from data import get_conn
from db import execute

def _blank():
    return {"final_sql": None, "turns": [], "candidates": [], "first_exec_ok": None}

# ---------------- O1 ----------------
def run_oneshot(context, question, db_id, temperature):
    r = _blank()
    msg, meta = chat([{"role": "user", "content": oneshot_prompt(context, question)}],
                     temperature=temperature)
    sql = extract_sql(msg.content)
    r["final_sql"] = sql
    r["turns"].append({"turn": 0, "sql": sql, "tool_calls": [], "tool_results": [],
                       "qet": None, **meta})
    return r

# ---------------- O3：并行采样 + 按执行结果投票 ----------------
def run_selfconsistency(context, question, db_id, temperature):
    r = _blank()
    conn = get_conn(db_id)
    cands, hashes = [], []

    for i in range(config.K_CANDIDATES):
        msg, meta = chat([{"role": "user", "content": oneshot_prompt(context, question)}],
                         temperature=temperature)            # ← O3 用 0.7
        sql = extract_sql(msg.content)
        cands.append(sql)
        try:
            res = set(map(tuple, execute(conn, sql)["rows"]))   # 与官方 EX 同口径
            h = hash(frozenset(res))
        except Exception:
            h = None
        hashes.append(h)
        r["turns"].append({"turn": i, "sql": sql, "tool_calls": [], "tool_results": [],
                           "qet": None, "result_hash": h, **meta})

    r["candidates"] = cands
    groups = {}
    for sql, h in zip(cands, hashes):
        if h is not None:
            groups.setdefault(h, []).append(sql)
    r["final_sql"] = (max(groups.values(), key=len)[0] if groups
                      else next((s for s in cands if s), None))
    conn.close()
    return r

# ---------------- A1/A2/A3：agent 循环 ----------------
def run_agent(context, question, db_id, temperature, tools):
    r = _blank()
    conn = get_conn(db_id)
    messages = [{"role": "system", "content": agent_system(context)},
                {"role": "user", "content": question}]

    for step in range(config.MAX_STEPS):
        msg, meta = chat(messages, tools=tools, temperature=temperature)
        sql = extract_sql(msg.content)
        turn = {"turn": step, "sql": sql, "tool_calls": [], "tool_results": [],
                "qet": None, "called_tools": False, **meta}

        if not msg.tool_calls:                  # 不再请求工具 = 要交答案了 → 成功即停
            r["turns"].append(turn)
            r["final_sql"] = sql
            break

        turn["called_tools"] = True
        messages.append(msg)
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            out, qet = dispatch(conn, db_id, tc.function.name, args)
            turn["tool_calls"].append({"name": tc.function.name, "args": args})
            turn["tool_results"].append(out)
            if tc.function.name == "run_sql":
                if r["first_exec_ok"] is None:  # 自纠成功率用
                    r["first_exec_ok"] = not out.startswith("执行失败")
                turn["qet"] = qet
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})

        r["turns"].append(turn)
        r["final_sql"] = sql                    # 用尽轮数时返回最后一轮

    conn.close()
    return r
```

**验收**：

```python
from methods import run_oneshot, run_selfconsistency, run_agent
from tools import tools_for
from data import build_context, load_questions
q = load_questions()[0]
ctx = build_context(q["db_id"], q.get("evidence"))

print("O1:", run_oneshot(ctx, q["question"], q["db_id"], 0.0)["final_sql"])

o3 = run_selfconsistency(ctx, q["question"], q["db_id"], 0.7)
for c in o3["candidates"]: print("  候选:", c)
assert len({c for c in o3["candidates"] if c}) > 1, "三个候选完全相同 → 温度没生效"

for g in ("A1", "A2", "A3"):
    a = run_agent(ctx, q["question"], q["db_id"], 0.0, tools_for(g))
    print(g, "轮数", len(a["turns"]), "首轮执行成功", a["first_exec_ok"], "|", a["final_sql"])
```

**标准**：O1 抽出 SQL；**O3 三个候选不能完全相同**；A1/A2/A3 每轮有 `tool_calls`/`call_latency`，`first_exec_ok` 为 True/False。

---

### 步骤 2.6 `run.py`

```python
import json, os, time, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import config
from data import load_questions, build_context
from methods import run_oneshot, run_selfconsistency, run_agent
from tools import tools_for

GROUPS = {
    # 组名: (方法,               温度,                  工具)
    "O1": (run_oneshot,          config.TEMPERATURE,          None),
    "O3": (run_selfconsistency,  config.TEMPERATURE_SAMPLING, None),
    "A1": (run_agent,            config.TEMPERATURE,          tools_for("A1")),
    "A2": (run_agent,            config.TEMPERATURE,          tools_for("A2")),
    "A3": (run_agent,            config.TEMPERATURE,          tools_for("A3")),
}

def load_done(path):
    done = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["question_id"])
                except Exception:
                    pass
    return done

def run_one(q, group):
    method, temp, tools = GROUPS[group]
    ctx = build_context(q["db_id"], q.get("evidence"))
    t0 = time.perf_counter()
    r = method(ctx, q["question"], q["db_id"], temp) if tools is None \
        else method(ctx, q["question"], q["db_id"], temp, tools)
    return {
        "question_id": q["question_id"], "db_id": q["db_id"],
        "difficulty": q.get("difficulty", "moderate"), "group": group,
        "prompt_context": ctx, "latency_total": time.perf_counter() - t0,
        "model_version": config.MODEL, **r,
    }

def main(group, limit=None, concurrency=None):
    config.RESULTS_DIR.mkdir(exist_ok=True)
    qs = load_questions()
    if limit:
        qs = qs[:limit]                                  # 冒烟：固定前 N 条
    out_path = config.RESULTS_DIR / f"{group}.jsonl"
    done = load_done(out_path)
    todo = [q for q in qs if q["question_id"] not in done]
    print(f"[{group}] 待跑 {len(todo)}（已完成 {len(done)}）")

    with open(out_path, "a", encoding="utf-8") as f, \
         ThreadPoolExecutor(concurrency or config.CONCURRENCY) as ex:
        futs = {ex.submit(run_one, q, group): q for q in todo}
        for fut in tqdm(as_completed(futs), total=len(todo), desc=group):
            q = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:                        # 单题失败不拖死整批
                rec = {"question_id": q["question_id"], "db_id": q["db_id"],
                       "group": group, "failure_type": "api_error", "error": str(e)}
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            f.flush()                                     # 立即落盘

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True, choices=list(GROUPS))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=None)
    a = ap.parse_args()
    main(a.group, a.limit, a.concurrency)
```

### 步骤 2.7 验收 → Gate 2

```bash
python run.py --group A1 --limit 3
python - <<'EOF'
import json
for line in open("results/A1.jsonl", encoding="utf-8"):
    r = json.loads(line)
    t = r["turns"][0]
    assert t["call_latency"] > 0, "缺每次调用单独计时"
    for k in ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens"):
        assert k in t, f"缺 {k}"
    print(r["question_id"], "轮数", len(r["turns"]), "总延迟", round(r["latency_total"], 2))
print("字段完整 OK")
EOF
```

再**手动 Ctrl-C 中断后重跑**，确认输出显示"已完成 3"，待跑数减少（断点续跑生效）。

**Gate 2 通过标准**：jsonl 字段齐全，尤其**每次调用单独延迟**与 **cache 分项 token**（事后补不回来）；断点续跑生效。

---

## 阶段 3 · 冒烟与吞吐实测（~30 min）

```bash
python run.py --group O1 --limit 20
python run.py --group A1 --limit 20
```

- 确认 **A1 每轮 SQL 都落盘**（EX@k 曲线全靠它）
- **实测吞吐**：记录每分钟完成题数、限流表现
- 据此外推全量 × 5 组的时长与成本

**Gate 3**：外推时长可接受。频繁 429 ⇒ 下调 `CONCURRENCY` 重测。

---

## 阶段 4 · 指标自检（~1 h）

| 检查项 | 怎么做 | 期望 |
|---|---|---|
| QET ⊂ Latency | 抽 3 题看时间分解 | QET **严格小于** `latency_total` |
| 列级召回 | 手工对 2 题数一遍 `(表,列)` 交集 | 与代码一致 |
| 幻觉检测 | 手造 `SELECT fake_col FROM t` | 被标记 |
| EX 判分 | 抽 2 题手工执行 gold 与生成 SQL | 与官方脚本结论一致 |
| `explain.py` 解析 | 拿 3 条 SQL 跑 `EXPLAIN QUERY PLAN` | 识别 `SCAN` / `SEARCH ... USING INDEX` / `USE TEMP B-TREE`；解析失败**单列** |

**Gate 4**：五项全过。指标算错，后面全要重跑。

---

## 阶段 5 · O1 全量 + 对账 ⭐ 最关键的 Gate（~40 min）

```bash
python run.py --group O1
python score.py --group O1
```

**官方 Mini-Dev SQLite EX baseline（已核实，来自仓库 README）**：

| 模型 | SQLite EX |
|---|---|
| mixtral-8x7b | 21.60 |
| llama3-8b-instruct | 24.40 |
| phi-3-medium-128k | 30.60 |
| gpt-35-turbo-instruct | 33.60 |
| gpt-35-turbo | 38.00 |
| llama3-70b-instruct | 40.80 |
| gpt-4-turbo | 45.80 |
| gpt-4-32k | 47.00 |
| gpt-4 | 47.80 |
| TA + gpt-4-turbo | 58.00 |
| **TA + gpt-4o** | **63.00** |

**Gate 5 通过标准**：O1（one-shot，无提示工程）用 DeepSeek 应落在 **~40–55** 这个区间。

> ⚠️ 低于 30 或高于 70 ⇒ 大概率是 prompt 拼装或判分链路有问题。**停下修，绝不往下跑**——否则后面四组全是废数据。

---

## 阶段 6 · 逐组跑（每组 ~40 min，合计 ~2.5 h）

严格按 **O3 → A1 → A2 → A3** 顺序，每次只加一个变量。**每组先跑 20 题子集**：

```bash
python run.py --group O3 --limit 20

# 确认工具确实被调用了
python - <<'EOF'
import json, collections
c = collections.Counter()
for line in open("results/O3.jsonl", encoding="utf-8"):
    for t in json.loads(line)["turns"]:
        for tc in t["tool_calls"]:
            c[tc["name"]] += 1
print(c)
EOF
```

| 组 | 子集必须验证什么 | 不通过怎么办 |
|---|---|---|
| `O3` | 3 个候选都执行了、投票有输出，**且三条 SQL 不完全相同** | 一样 ⇒ 温度没生效，查 `GROUPS` 表 |
| `A1` | **报错确实被回灌** —— 打印一轮完整 messages 亲眼确认 | 查 tool result 是否进了 `messages` |
| `A2` | **`get_column_values` 被调用了**（上面的 Counter 不为 0） | 改 §2.4 的 description 文案 |
| `A3` | **`get_column_desc` 被调用了** | 同上；若 20 题为 0，考虑砍掉 A3 |

子集通过后跑该组全量。

**Gate 6**：`max_steps` 打满的比例不异常偏高。若大量题跑满 3 轮 ⇒ 模型不收敛，**查 prompt**。

---

## 阶段 7 · 离线分析（~3 h）

### 步骤 7.1 `score.py` —— 接官方脚本

**关键事实（已核实）**：
- 官方 EX = `set(pred_res) == set(gold_res)`（忽略列序与重复行）
- `evaluation_utils.execute_sql(pred, gold, db_path, dialect, calc_func)` 执行两条 SQL 并返回 0/1，**失败会抛异常**
- `db_path` 形如 `<db_root_path>/<db_id>/<db_id>.sqlite`
- gold 文件每行是 `<SQL>\t<db_id>`（**制表符分隔**）

```python
import json, sys, argparse
from collections import defaultdict
from func_timeout import func_timeout, FunctionTimedOut
import config

sys.path.insert(0, str(config.EVAL_DIR))
from evaluation_utils import execute_sql
from evaluation_ex import calculate_ex      # 官方：set(pred)==set(gold)

def load_gold():
    out = []
    for line in open(config.GOLD_FILE, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        sql, db_id = line.split("\t")       # 官方格式：制表符分隔
        out.append((sql, db_id))
    return out

def judge(pred_sql, gold_sql, db_id, timeout=30.0):
    """返回 1/0，并给失败类型"""
    if not pred_sql or not str(pred_sql).strip():
        return 0, "parse_error"
    db_path = str(config.DB_DIR / db_id / f"{db_id}.sqlite")
    try:
        r = func_timeout(timeout, execute_sql,
                         args=(pred_sql, gold_sql, db_path, "SQLite", calculate_ex))
        return r, ("ok" if r == 1 else "result_mismatch")
    except FunctionTimedOut:
        return 0, "timeout"
    except Exception:
        return 0, "exec_error"

def main(group):
    gold = load_gold()
    recs = [json.loads(l) for l in open(config.RESULTS_DIR / f"{group}.jsonl", encoding="utf-8")]
    recs = [r for r in recs if r.get("failure_type") != "api_error" and "final_sql" in r]

    ex_at_k = defaultdict(lambda: defaultdict(int))     # EX@k
    for r in recs:
        qid = r["question_id"]
        gsql, db_id = gold[qid]                          # 下标与题目顺序对齐
        ok, ftype = judge(r.get("final_sql"), gsql, db_id)
        r["is_correct"], r["failure_type"] = ok, ftype
        # EX@k：逐轮判分（离线，零额外 API 成本）
        for t in r["turns"]:
            if t.get("sql"):
                k = t["turn"] + 1
                o, _ = judge(t["sql"], gsql, db_id)
                ex_at_k[k][r["difficulty"]] += o

    out = config.RESULTS_DIR / f"{group}_scored.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    n = len(recs)
    acc = sum(r["is_correct"] for r in recs) / n * 100
    print(f"[{group}] n={n}  EX={acc:.2f}")
    by_diff = defaultdict(lambda: [0, 0])
    for r in recs:
        d = by_diff[r["difficulty"]]; d[0] += r["is_correct"]; d[1] += 1
    for d, (c, t) in sorted(by_diff.items()):
        print(f"   {d:12} {c/t*100:6.2f}  (n={t})")
    print("   EX@k:", {k: round(sum(v.values())/max(sum(v.values()),1)*100, 2) for k, v in sorted(ex_at_k.items())})
    print(f"→ {out}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--group", required=True)
    main(ap.parse_args().group)
```

> **为什么要自己跑逐题判分**：官方 `evaluation_ex.py` 的主流程只输出**聚合**结果（按难度），拿不到逐题对错；而逐题结果是 EX@k、列级召回、失败案例筛选的基础。这里**复用了官方的 `execute_sql` 和 `calculate_ex`**，判分口径与官方一致。

### 步骤 7.2 `analyze.py` —— 五指标 + EX@k + 成本 + 延迟分解

```python
import json, argparse, statistics as st
from collections import defaultdict, Counter
import config

def pct(xs, p):
    xs = sorted(xs)
    return xs[min(int(len(xs) * p), len(xs) - 1)] if xs else 0

def load(group):
    p = config.RESULTS_DIR / f"{group}_scored.jsonl"
    if not p.exists():
        p = config.RESULTS_DIR / f"{group}.jsonl"
    return [json.loads(l) for l in open(p, encoding="utf-8")]

def tokens(r):
    inp = sum(t.get("prompt_tokens", 0) for t in r.get("turns", []))
    out = sum(t.get("completion_tokens", 0) for t in r.get("turns", []))
    hit = sum(t.get("cache_hit_tokens", 0) for t in r.get("turns", []))
    miss = sum(t.get("cache_miss_tokens", 0) for t in r.get("turns", []))
    return inp, out, hit, miss

def report(group):
    rs = load(group)
    lat = [r["latency_total"] for r in rs]
    qets = [t["qet"] for r in rs for t in r.get("turns", []) if t.get("qet")]
    ex = sum(r.get("is_correct", 0) for r in rs) / len(rs) * 100
    tin = sum(tokens(r)[0] for r in rs); tout = sum(tokens(r)[1] for r in rs)
    hit = sum(tokens(r)[2] for r in rs); miss = sum(tokens(r)[3] for r in rs)

    print(f"===== {group}  n={len(rs)} =====")
    print(f"EX            {ex:6.2f} %")
    print(f"Latency  P50/P95/P99  {pct(lat,.5):6.2f} / {pct(lat,.95):6.2f} / {pct(lat,.99):6.2f} s")
    print(f"QET      P50/P95      {pct(qets,.5):6.4f} / {pct(qets,.95):6.4f} s")
    print(f"tokens   in/out {tin}/{tout}    cache hit/miss {hit}/{miss}")
    print(f"平均轮数      {st.mean([len(r.get('turns',[])) for r in rs]):.2f}")
    tools = Counter(tc["name"] for r in rs for t in r.get("turns", []) for tc in t.get("tool_calls", []))
    print(f"工具调用分布  {dict(tools)}")
    ft = Counter(r.get("failure_type", "?") for r in rs)
    print(f"失败类型      {dict(ft)}")

def ex_at_k(group):
    """EX@k 曲线：逐轮 SQL 的累计正确率。前提：成功即停"""
    rs = load(group)
    curve = defaultdict(list)
    for r in rs:
        for t in r.get("turns", []):
            if t.get("sql"):
                curve[t["turn"] + 1].append(1 if r.get("is_correct") and t["turn"] == len(r["turns"]) - 1 else 0)
    # 更准确的做法：EX@k 用 score.py 存的逐轮判分；这里给的是最终轮次分布
    print(" 轮次分布:", {k: len(v) for k, v in sorted(curve.items())})

def tail_decompose(group):
    """长尾分解：轮数方差 vs 单次调用抖动 —— 第十节 tail latency 论证的前提"""
    rs = load(group)
    per_call = [t["call_latency"] for r in rs for t in r.get("turns", []) if t.get("call_latency")]
    by_turn = defaultdict(list)
    for r in rs:
        n = len(r.get("turns", []))
        by_turn[n].append(r["latency_total"])
    print(f"单次调用延迟 P50/P95/P99: {pct(per_call,.5):.2f}/{pct(per_call,.95):.2f}/{pct(per_call,.99):.2f}")
    for n, v in sorted(by_turn.items()):
        print(f"  {n} 轮的任务 P50/P95: {pct(v,.5):.2f}/{pct(v,.95):.2f}  (n={len(v)})")

def cost_per_point(groups):
    """每提升 1 个 EX 点要花多少 token"""
    base = None
    for g in groups:
        rs = load(g)
        ex = sum(r.get("is_correct", 0) for r in rs) / len(rs) * 100
        tk = sum(sum(tokens(r)) for r in rs) / len(rs)
        if base is None: base = (ex, tk)
        d_ex, d_tk = ex - base[0], tk - base[1]
        print(f"{g}: EX={ex:.2f}  avg_tokens={tk:.0f}  ΔEX={d_ex:+.2f}  cost/point={d_tk/d_ex if d_ex else float('nan'):.0f}")

def failed_cases(group, mult_tok=3.0, mult_lat=3.0):
    """失败案例筛选：花了钱/耗时但仍答错"""
    rs = load(group)
    tok = [sum(tokens(r)) for r in rs]; lat = [r["latency_total"] for r in rs]
    mt, ml = st.median(tok), pct(lat, .95)
    hits = [r for r in rs if not r.get("is_correct", 0) and
            (sum(tokens(r)) > mult_tok * mt or r["latency_total"] > mult_lat * ml)]
    print(f"失败案例 {len(hits)} 条 → results/{group}_failed.json")
    json.dump([{"question_id": r["question_id"], "db_id": r["db_id"],
                "difficulty": r.get("difficulty"), "final_sql": r.get("final_sql"),
                "failure_type": r.get("failure_type"), "tokens": sum(tokens(r))}
               for r in hits],
              open(config.RESULTS_DIR / f"{group}_failed.json", "w"),
              ensure_ascii=False, indent=2)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", nargs="+", default=["O1", "O3", "A1", "A2", "A3"])
    a = ap.parse_args()
    for g in a.groups: report(g); ex_at_k(g); tail_decompose(g); print()
    cost_per_point(a.groups)
    for g in a.groups: failed_cases(g)
```

### 步骤 7.3 `explain.py` —— Query Plan / 执行算子 / 数据模型

```python
import json, argparse, sqlite3, re
from collections import Counter, defaultdict
import config
from data import db_path, schema_whitelist

RE_SCAN   = re.compile(r"\bSCAN\s+(\S+)")
RE_SEARCH = re.compile(r"\bSEARCH\s+(\S+)\s+USING\s+(\w+)\s+\(?([^)]*)\)?")
RE_TEMPBT = re.compile(r"USE TEMP B-TREE")

def plan_of(db_id, sql):
    """返回算子摘要；失败返回 None（单列，不混入统计）"""
    try:
        conn = sqlite3.connect(f"file:{db_path(db_id)}?mode=ro", uri=True)
        rows = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        conn.close()
    except Exception:
        return None
    txt = " | ".join(r[3] for r in rows)
    return {
        "text": txt,
        "n_scan": len(RE_SCAN.findall(txt)),
        "n_search": len(RE_SEARCH.findall(txt)),
        "temp_btree": bool(RE_TEMPBT.search(txt)),
        "join": "JOIN" in txt.upper(),
    }

def analyze(group):
    rs = [json.loads(l) for l in open(config.RESULTS_DIR / f"{group}_scored.jsonl", encoding="utf-8")]
    agg, bad, bysz = Counter(), 0, defaultdict(lambda: [0, 0])
    for r in rs:
        p = plan_of(r["db_id"], r["final_sql"]) if r.get("final_sql") else None
        if p is None:
            bad += 1; continue
        agg["scan"] += p["n_scan"]; agg["search"] += p["n_search"]
        agg["temp_btree"] += p["temp_btree"]; agg["join"] += p["join"]
        sz = "small" if len(schema_whitelist(r["db_id"])) <= 5 else \
             "medium" if len(schema_whitelist(r["db_id"])) <= 10 else "large"
        bysz[sz][0] += r.get("is_correct", 0); bysz[sz][1] += 1
    total_op = agg["scan"] + agg["search"]
    print(f"===== {group} =====")
    print(f"解析失败单列: {bad} 条")
    print(f"SCAN {agg['scan']} / SEARCH {agg['search']}  全表扫描占比 {agg['scan']/max(total_op,1)*100:.1f}%")
    print(f"含临时 B-tree 排序: {agg['temp_btree']}   含 join: {agg['join']}")
    print("按 schema 复杂度分层 EX:", {k: f"{c/t*100:.1f}%(n={t})" for k, (c, t) in sorted(bysz.items())})

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", nargs="+", default=["O1", "A1", "A3"])
    a = ap.parse_args()
    for g in a.groups: analyze(g); print()
```

### 步骤 7.4 验收 → Gate 7

```bash
python score.py --group O1
python analyze.py --groups O1 O3 A1 A2 A3
python explain.py --groups O1 A1 A3
```

**Gate 7**：EX@k 曲线单调不减。若 `EX@2 < EX@1` ⇒ "成功即停"逻辑有 bug。

---

## 阶段 8 · 报告产出（~3 h）

> **本阶段已基于实跑完成**：`O1` / `O3` / `A1` 三组各 500 题全部跑完并判分，
> 同一批次，原始产物在 `results/repro3/`（该目录被 gitignore）。
> 与更早一次独立运行的 O1/A1 相比，见 8.1 的「稳健性」一栏。
>
> ⚠️ 手册 §1.2 写的是**五组**（O1/O3/A1/A2/A3），实际收敛为**三组**：
> A2/A3 及其对照组已随代码裁剪移除。写报告时以三组为准，§1.2 与阶段 6 的表述需同步。

### 8.1 三次试验的实测结果（可直接入报告）

**主表**（n = 500，同批次，逐题配对）

| 指标 | O1 | O3 | **A1** |
|---|---|---|---|
| **EX** | 57.80% | 59.40% | **64.40%** |
| simple（n=148） | 72.3% | 71.6% | **79.1%** |
| moderate（n=250） | 54.4% | 57.2% | **60.8%** |
| challenging（n=102） | 45.1% | 47.1% | **52.0%** |
| 列级召回 | 90.81% | 90.61% | 89.02% |
| 幻觉率 | 0.78% | 0.97% | **0.68%** |
| `exec_error` | 9 | 5 | **0** |
| `timeout` | 2 | 2 | 2 |
| 调用/题 | 1.00 | 3.00 | 3.57 |

**配对检验（McNemar 精确）** —— 净翻转 = 后组 − 前组

| 对比 | 净变化 | 不一致对 | p 值 | 结论 |
|---|---|---|---|---|
| O3 − O1 | +8 题（+1.60pt） | **18** | 0.0963 | 不显著 |
| **A1 − O1** | **+33 题（+6.60pt）** | 91 | **0.0007** | **显著** |
| **A1 − O3** | **+25 题（+5.00pt）** | 93 | **0.0124** | **显著** |

```bash
cd <仓库根>
P=~/.conda/envs/db_agent/bin/python
$P paired.py repro3/O1_scored.jsonl repro3/A1_scored.jsonl
$P paired.py repro3/O1_scored.jsonl repro3/O3_scored.jsonl
$P paired.py repro3/A1_scored.jsonl repro3/O3_scored.jsonl
```

**代价**（同批次实测）

| | O1 | O3 | A1 |
|---|---|---|---|
| 调用/题 | 1.00 | 3.00 | 3.57 |
| 输入 token | 488,730 | 1,466,190 | 3,894,598 |
| 缓存命中率 | 80.4% | 80.4% | 90.4% |
| 输出 token | 28,628 | 85,930 | 225,815 |
| Latency P50 | 0.7 s | 2.0 s | 2.6 s |
| Latency P95 | 1.0 s | 2.8 s | 7.0 s |

> O3 恰为 O1 的 **3.00 倍**（输入输出都精确 3×，因为它是三条独立的单轮调用）；
> A1 约为 O1 的 **8 倍**（输入 7.97×、输出 7.89×）。

**机制发现（报告的核心论据，按证据强度排序）**

1. **A1 的增益来自"输出形态自检"，不是"agent 结构"本身。**
   改动前 220 条错误里 **48.6% 是列数不同**（多带"佐证列"）；官方 EX 是
   `set(pred)==set(gold)`，**多一列即判错**。修复（prompt 第 1 步先定输出形态 +
   工具回报列名列数）后，列数差异占比 **48.6% → 22.7%**，错误 220 → 181 条。
   → 目标错误类被精确压下，**因果链闭合**。

2. **采样投票（O3）的机制天花板只有 23% 的题。**
   `score.py --group O3` 的机制体检显示：**380/500（76%）的题上三个候选执行的
   结果集完全相同**，"按结果集投票"是空操作。真正存在分歧的只有 115 题，
   而实测真正翻盘的只有 **18 题**。
   → 这与 §3.2 引用的 DAIL-SQL/XiYan-SQL 结论一致（无选择模型的自一致性会掉点）。

3. **同样约 3 次调用/题的预算，投给"迭代"远优于投给"采样重选"。**
   O3：3.00 调用/题 → +1.60pt（不显著）；A1：3.57 调用/题 → +6.60pt（p=0.0007）。
   → 报告里这是一个比"agent 更准"更强的论点：**同一预算下的机制选择问题**。

4. **列级召回与 EX 严重脱节，不能作为方法排序依据。**
   三组列级召回都在 89%~91%，而 EX 从 57.8% 到 64.4%；**A1 的列级召回（89.02%）
   甚至低于 O1（90.81%），EX 却高 6.6pt**。

**稳健性**（用于回应"你是不是挑了一次好看的"）

| | 更早一次独立运行 | 本批次 | 差 |
|---|---|---|---|
| O1 EX | 58.40% | 57.80% | −0.60pt |
| A1 EX | 63.40% | 64.40% | +1.00pt |
| A1 − O1 | +25 题 / p=0.0059 | +33 题 / p=0.0007 | 同向 |

**翻盘题级重合**：两次运行 A1「救回」的题交集 **46/51**（Jaccard 0.69）；
若相互独立，期望仅 6.3 题 —— **是随机的 7.3 倍**，说明增益作用在同一批具体题目上。

**噪声基线**（同配置重跑，无处理差异）：O1 自身摆动 −0.60pt / 翻盘 15 题；
A1 自身 +1.00pt / 翻盘 21 题 → **噪声底 ±1pt、±3~5 题**；而处理效应是 +6.60pt / +33 题。

### 8.2 产出清单与来源（已按实际存在的脚本修正）

| 产出 | 来源 | 状态 |
|---|---|---|
| 主结果对比表（EX / 列级召回 / 幻觉率 / Latency） | `score.py` + `run.py` 的 `latency_total` | ✅ 见 8.1 |
| 分层 EX 表 | `score.py`（按 difficulty 分层） | ✅ 见 8.1 |
| 配对检验（McNemar） | `paired.py` | ✅ 见 8.1 |
| 代价（调用数 / token / 缓存 / 延迟） | `results/*.jsonl` 的 `turns` 埋点 | ✅ 见 8.1 |
| O3 机制体检 | `score.py --group O3`（`o3_mechanism_health`） | ✅ 见 8.1 |
| 交叉验证与噪声基线 | 两次运行的 `_scored.jsonl` 对拷 | ✅ 见 8.1 |
| EX@k 曲线 | `score.py` 末尾已打印逐轮数字 | ⚠️ **只有数字，未绘图** |
| 延迟分解（轮数方差 vs 单次调用抖动） | `turns[].call_latency` + `qet` | ❌ 未做 |
| QET 对比（Latency/QET 分离，§1.5） | `turns[].qet` | ❌ 未做 |
| 成本-收益帕累托前沿 | 8.1 代价表 + EX | ❌ 未做 |
| `EXPLAIN QUERY PLAN` / `SCAN`-`SEARCH` 分桶 | §3.1 | ❌ 未做 |
| 2~3 个 case study | `paired.py` 输出的翻盘题 qidx → 原始轨迹 | ⚠️ **入口已有，未挑题** |
| 工具调用分布 | `turns[].tool_calls` | ⚠️ 数据在，未汇总 |

> ⚠️ **手册阶段 7 里的 `analyze.py` / `explain.py` 从未实现**（`plot_utils.py` 也只是空壳）。
> 上表凡标 ❌ 的产出，都需要新写脚本，**不能在报告里声称已完成**。

### 8.3 必须披露的项（漏了会被当成不公平对比或夸大结论）

- **O3 采样温度 0.7，其余组 0.0**——机制要求，须说明原因；且在本设计下
  **"投票"与"温度"完全共线，无法分离出投票的净贡献**，不能表述为"投票带来 +1.6pt"
- 写「温度设为 0 以**最大化**可复现性」，**不写「完全可复现」**——
  本仓库实测同配置重跑，O1 摆动 ±0.6pt、翻盘 15 题（见 8.1 噪声基线）
- **三组必须同批次**；与更早一次运行的数字**不可跨批相减**（同上）
- 从 EX 分母剔除的 `api_error` 数：**本批 0**（另两组亦为 0）
- `timeout` **计 EX=0 不剔除**：O1 2 / O3 2 / A1 2
- **两条采集兜底（不是方法效果，必须单列）**：
  A1 末轮答案执行失败 → 退回"最后一条真的执行成功过的 SQL"：**3/500**；
  O3 三个候选全部执行失败 → 退回"首个非空候选 SQL"：**5/500**
- `run_sql` 成功时**不回完整结果**，只回行数 + 列名 + 3 行预览（§2.4）
- **幻觉率天然对 agent 有利**——但本批三组为 0.68%~0.97%，**无区分度**，
  不能拿它当"agent 更好"的证据（§1.5 已声明它不是独立指标）
- evidence 是**外部给定**的知识，不是模型自己查出来的
- Python / SQLite / 模型版本（`results/env.json`）
- **`A1` ≡ 历史文档中的 "A1v2"**（含输出形态自检的版本）；旧 A1 已删除，避免同名歧义

### 8.4 尚未完成、报告中不得声称已做的部分

1. **延迟长尾的归因**：必须有 `call_latency` 分解才能说"长尾来自 agent 多轮"还是
   "API 自身抖动"。未分解前，只能说"agent 的 P95 更高"，**不能归因**。
2. **EX@k 与 cost-per-point 曲线**：数字已有，图未画（§3.3 的"代价非线性"论证依赖它）。
3. **QET 对比**：只报了 Latency，未报 QET，因此 **§1.5 那句"agent 用执行效率换准确率"
   尚无数据支撑**。
4. **DB 基础问题 3 点（§3.1）**：需要 `EXPLAIN QUERY PLAN` 与 schema 分桶分析，未做。
   注意课程要求"至少 3 点"，这是硬要求，**必须补**。
5. **case study**：翻盘题清单已由 `paired.py` 给出（A1 救回 62 题 / 弄坏 29 题），
   挑 2~3 题读原始轨迹即可，但尚未挑。

---

# 第三部分 · 报告要求对照

课程四项硬要求：

| # | 要求 | 满足方式 |
|---|---|---|
| 1 | 数据库基本问题**至少 3 点** | §3.1 |
| 2 | 指标从给定池中**至少 2 个** | 已选 5 个（§1.5） |
| 3 | SIGMOD/VLDB/ICDE/CIDR/OSDI/FAST 选 **2 篇** | §3.2 |
| 4 | 含**失败案例** | §3.3 |

## 3.1 DB 基础问题 3 点（阶段 7 纯分析，零额外 API 成本）

**组织论点**：

> **agent 是"执行密集"负载——每轮都要真的跑 SQL，因此比 one-shot 对 DB 内核特性敏感得多。**

one-shot 生成完即结束，DB 侧如何组织数据对其无影响；agent 把 LLM 决策与 DB 内核耦合：执行计划、算子选择、索引命中都会通过"执行反馈"回流到下一轮决策。**这不只是"agent 更准"，而是"agent 放大了数据库侧优化的收益"。**

| # | 要点 | 分析对象 | 产出 |
|---|---|---|---|
| 1 | **Query Plan** | 全部生成的 SQL 跑 `EXPLAIN QUERY PLAN` | 计划形态分布，回答"agent 生成的 SQL 好在哪" |
| 2 | **执行算子** | 解析计划中的算子 | `SCAN` vs `SEARCH` 比例、join、`USE TEMP B-TREE` 出现率 |
| 3 | **数据模型** | 11 个库的 schema | 按复杂度（表数 / 外键深度）分桶，看 agent 的 EX 提升是否随复杂度上升 |

> ⚠️ **SQLite 只有 B-tree，没有原生 Hash 索引**，清单里的 "B+树 vs Hash" 对比在 SQLite 侧做不了——作为报告局限性写明即可。

## 3.2 两篇论文对比（阶段 8）

| | 论文 | 出处 | 为什么是它 |
|---|---|---|---|
| A | **DAIL-SQL** — Text-to-SQL Empowered by Large Language Models: A Benchmark Evaluation | PVLDB 17(5): 1132–1145, 2024 | 同基准、同范式（冻结模型 + prompt 工程），其 **execution-guided selection 正是 O3 的正统版本** |
| B | **Reward-SQL** | SIGMOD 2026 | 正面处理"执行反馈粒度"，走**训练路线**（CTE 分解 + PRM + RL） |

> ⚠️ **Reward-SQL 目前仅见厂商技术博客，引用前必须核实原文**。备选：**CodeS**（PACMMOD/SIGMOD 2024, 2(3):127）或 **XiYan-SQL**（SIGMOD 2025，其 M-Schema 列值检索正对应本项目的两个探测工具）。

**四个问题**：

**① 当前方法到什么程度**：DAIL-SQL 确立"不训练的上限"，证明**按执行结果投票 > 按 SQL 文本投票**；Reward-SQL 用训练让 8B 小模型达到 BIRD 70.3%。

**② 本项目与论文差在哪**

| 维度 | DAIL-SQL | 本项目 |
|---|---|---|
| 采样 | **并行**采样 N 个候选 | agent 组**串行**迭代 |
| 执行的作用 | 只用于**选择** | **回灌驱动修正** |
| 信息获取 | 无工具，schema 一次注入 | 3 个工具主动探测 |

| 维度 | Reward-SQL | 本项目 |
|---|---|---|
| 反馈粒度 | **步级**（每个 CTE 可独立验证） | **轮级**（整条 SQL 重试） |
| 反馈来源 | 训练出的 PRM | **免费的报错文本** |
| 模型 | 自训 8B | 冻结的 API 模型 |

**③ 哪些结果不能推广**：依赖可重复执行 ⇒ 非确定性查询上 selection 失效；**训练路线对闭源 API 模型完全不可迁移**（本项目反而不训练、跨模型可迁移，是相对优势）；CTE 分解有前提（窗口函数、递归 CTE、跨表聚合未必可分）；成本口径不可比（GPU 小时 vs token），**避免硬比**。

**④ 最值得解决的问题**

> **不训练的前提下，用什么最省的机制逼近步级反馈。**

现有工作二选一：粗粒度重试（便宜但低效）或细粒度 PRM（昂贵）。三个方向：① prompt 实现的 CTE 式分解验证；② 选择性验证（只验高风险片段）；③ **成本感知的终止策略**——本项目的 EX@k 曲线正好能提供数据。

## 3.3 失败案例（阶段 7-8）

从 **Cost Trade-off** 与 **Tail Latency** 论证 agent 不如 one-shot。

**⚠️ 表述纪律**：agent 更贵更慢是**结构性必然**，不是发现。不能停在"贵 3 倍、P99 差 5 倍"，必须做成三条：

1. **量化代价**——贵几倍、P99 差几倍
2. **证明代价非线性**——第 3 轮边际成本高但边际收益 ≈ 0（靠 **EX@k 曲线**）
3. **给出帕累托前沿**——给定 EX 目标下哪个方案最划算

**⚠️ 长尾不可直接归因于 agent**：延迟长尾可能主要来自 LLM API 自身抖动。必须靠每轮 `call_latency` 分解成「轮数方差」vs「单次调用抖动」。若主要来自单次抖动 ⇒ 结论须改为"agent 放大了 API 固有长尾"。

---

# 附录 A · Gate 速查

| Gate | 内容 | 不过怎么办 |
|---|---|---|
| 0 | cache 分项 token 可采集 + tool calling 可用 + SQLite ≥ 3.41 | 换别名 / 换 SDK 版本 |
| 1 | 题目数与 db_id 正常、`build_context` 确定性 | 排查随机源；检查目录层级 |
| 2 | jsonl 字段完整（尤其分项计时与 token） | 补埋点，别往下跑 |
| 3 | 外推时长可接受 | 降并发重测 |
| 4 | 五项指标自检 | 修指标 |
| **5** | **O1 落在 40–55 区间** | **停下修 prompt / 判分** |
| **6** | **各组子集埋点生效（工具被调用）** | **改 description 再跑全量** |
| 7 | EX@k 单调不减 | 查"成功即停"逻辑 |

# 附录 B · 异常处理

| 现象 | 处理 |
|---|---|
| 下载后目录多一层 `mini_dev_data/` | 见步骤 1.2，把**内容**拷进 `data/` |
| 评测脚本 `ModuleNotFoundError: psycopg2` | 只跑 SQLite 也要装：`pip install func_timeout psycopg2-binary pymysql` |
| 评测脚本 `KeyError: 'difficulty'` | `diff_json_path` 缺 difficulty 字段，见步骤 1.5 |
| 预测文件被判分脚本报错 | 格式必须是 `{"0": "SQL\t----- bird -----\t<db_id>"}`，分隔符是制表符 |
| 频繁 429 | 下调 `CONCURRENCY`；单题重试 3 次仍失败 → 标 `api_error`，**从 EX 分母剔除**并说明剔除数 |
| SQL 超时 / 超行 | 标 `timeout`，**计 EX=0 不剔除** |
| 模型不调工具 | 改 description 文案（写清"什么时候该用"），不是改模型 |
| 抽不出 SQL | 计 EX=0，归 `parse_error` |
| 判分与手工不符 | 用官方 `execute_sql` + `calculate_ex`，别自写比对 |

# 附录 C · 已核实事实与修正记录

**已对官方仓库核实**：下载入口、目录层级、`mini_dev_sqlite_gold.sql` 文件名、gold 文件制表符格式、评测脚本路径与全部参数、预测 JSON 格式、EX 判定口径（`set` 比较）、评测脚本依赖（含 psycopg2/pymysql）、11 个 db_id 及题量、难度分布 30/50/20、SQLite EX baseline 表、MySQL 灌库步骤。

**本手册修正了我此前给出的错误信息**：

| 此前写的（错） | 实际（已核实） |
|---|---|
| `mini_dev_sql_gold.sql` | **`mini_dev_sqlite_gold.sql`** |
| `data/mini_dev_sqlite.json` | 下载后是 `mini_dev_data/mini_dev_sqlite.json`，需摆正 |
| 下载步骤缺失 | zip / HF 两条路，入口见 §1.1 |
| 未说评测脚本依赖 | **`func_timeout` + `psycopg2` + `pymysql`，只跑 SQLite 也必须装** |
| Mini-Dev = 500 题 | **V2 已 780 题**（500 原题 + 270 新题）；**V2 无 MySQL 版** |
| baseline 未给 | gpt-4 47.80 / TA+gpt-4o 63.00（SQLite EX） |

**仍未核实、需你在运行时确认的**：
1. `database_description/*.csv` 的**实际列名**（§1.6 的 `desc_index` 按 `original_column_name` 等列名解析——步骤 1.4 的 `head` 会告诉你真实列名）
2. `mini_dev_sqlite.json` 是 JSON 数组还是 JSONL（步骤 1.5 处理）
3. 下载包实际是 500 题还是 780 题（步骤 1.2 处理）

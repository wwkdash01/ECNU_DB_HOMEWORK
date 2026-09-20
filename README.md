# db_agent

> 项目简介待补充。

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

`config.py` 只存**环境变量名**而非密钥本身，因此该文件可以安全提交：

```python
# config.py
API_KEY_ENV = "DEEPSEEK_API_KEY"
BASE_URL    = "https://api.deepseek.com"
MODEL       = "deepseek-flash"
TEMPERATURE = 0.0
MAX_TOKENS  = 393216
```

调用处：

```python
import os

from dotenv import load_dotenv
from openai import OpenAI

from config import API_KEY_ENV, BASE_URL, MODEL, TEMPERATURE, MAX_TOKENS

load_dotenv()   # 从当前目录向上查找 .env

client = OpenAI(
    api_key=os.environ[API_KEY_ENV],
    base_url=BASE_URL,
)

resp = client.chat.completions.create(
    model=MODEL,
    temperature=TEMPERATURE,
    max_tokens=MAX_TOKENS,
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### 模型参数说明（`deepseek-flash`）

| 参数 | 说明 |
| --- | --- |
| 上下文长度 | 1M |
| 最大输出 | **384K（393216）**；不设时默认 8K（非思考）/ 64K（思考） |
| 思考模式 | **默认开启**（`thinking.type = "enabled"`），`reasoning_effort` 默认 `high` |
| `temperature` | 取值 ≤ 2。**思考模式下不生效**，此时由 `top_p` 控制（有效区间 0.95–1.0） |
| `tool_choice` | 思考模式下**不支持 `required` 及指定具体工具**，会返回 400 |

> ⚠️ 两个容易踩的坑：
> 1. `MAX_TOKENS` 若小于思考所需，`reasoning_content` 会吃光额度导致正文截断，
>    `finish_reason` 返回 `length`。
> 2. 强制工具调用（`tool_choice="required"`）在思考模式下直接 400；
>    要么改用 `auto`，要么先 `thinking={"type": "disabled"}` 关掉思考模式。

### 提交前自查

```bash
git check-ignore -v .env    # 有输出 = 已被正确忽略
git add -An                 # 确认 .env 不在待提交列表里
```

**切勿**把密钥写进 `config.py` 等源码文件，也不要 `export` 到 `~/.zshrc`——
源码和 shell 配置都可能被提交、备份或同步出去。

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

### 随附的关键间接依赖

| 包 | 版本 |
| --- | --- |
| `numpy` | 2.5.3 |
| `pydantic` | 2.13.5 |
| `httpx2` | 2.13.0 |
| `anyio` | 4.15.1 |
| `pillow` | 12.3.0 |
| `fonttools` | 4.65.0 |

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

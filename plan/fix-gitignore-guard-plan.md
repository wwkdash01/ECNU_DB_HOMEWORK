---
name: fix-gitignore-guard
overview: 修 _gitignored() 的逻辑错误 —— 无 git 仓库时误判为"未被忽略"导致拒绝写 .env
todos:
  - id: fix-guard
    content: _gitignored 返回 (safe, reason)，区分「非仓库 / 已屏蔽 / 未屏蔽 / 无法确认」四种情形
    status: pending
  - id: wire
    content: write_env_key 适配新返回签名；拒绝时带原因，跳过守卫时在成功消息里显式提示
    status: pending
  - id: verify
    content: 用临时 git 仓库覆盖四个分支（非仓库 / 已屏蔽 / 未屏蔽 / 仓库外），并确认真实 .env 未被污染
    status: pending
isProject: false
---

# 修 #1：gitignore 守卫在无 git 仓库时误拒

## Context

用户选择「只修 #1（真 bug，最小改动）」。

**Bug**：`_gitignored()` 把「git 不可用 / 不是仓库」与「路径未被 .gitignore 屏蔽」当成同一件事，
两者都返回 `False` → `write_env_key()` 一律拒绝写入。

**实测证据**（非 git 目录下）：

```
$ git check-ignore -q .env
fatal: not a git repository (or any of the parent directories): .git
退出码 = 128          → _gitignored 返回 False → 拒绝写 .env
```

**后果**：用 ZIP 下载的仓库副本（Windows 上最常见的获取方式）**密钥录入功能完全不可用**，
且报错文案是"请先修 .gitignore"——误导用户去改一个没问题的文件。

**根因**：这个守卫的原始意图是"防 .gitignore 失守"，却实现成了"防一切不确定"。
没有 `.git` 时根本不存在提交风险，应当**放行**。

## 改动清单

### 1. `_gitignored(path)` → 返回 `(safe, reason)`

| 情形 | 判定 | 返回值 |
| --- | --- | --- |
| 路径在仓库外 | 无提交风险 | `(True, "路径在仓库外，不存在被提交的风险")` |
| `ROOT/.git` 不存在 | **不是 git 仓库**（ZIP 副本）→ 无提交风险 | `(True, "未检测到 git 仓库（可能是 ZIP 下载的副本），跳过 gitignore 守卫")` |
| git 返回 0 | 已被屏蔽 | `(True, "<rel> 已被 .gitignore 屏蔽")` |
| git 返回 1 | **未**被屏蔽 | `(False, "<rel> 未被 .gitignore 屏蔽，写进去可能被提交进版本库")` |
| git 缺失 / 超时 / 其它返回码 | 有仓库但无法确认 | `(False, "…无法确认安全性，拒绝写入")` ← 保守不放行 |

**顺带（同一行内的一个词）**：`str(...)` → `...as_posix()`。原写法把 `data\x.env`
这种反斜杠路径当 git pathspec 传给 git（git 用 `/`，反斜杠是转义符）。macOS 上
`as_posix()` 与 `str()` 结果完全相同（**零行为差异**），仅在 Windows 上修正语义。
另外给 `check-ignore` 加 `--` 分隔符，避免以 `-` 开头的路径被当成选项。

### 2. `write_env_key()` 适配

```python
safe, why = _gitignored(env_path)
if not safe:
    return False, f"拒绝写入：{why}。", None
guard_note = "" if "屏蔽" in why else f"（{why}）"      # 守卫被跳过时显式告知
...
return True, f"已写入 {env_path}（{config.API_KEY_ENV}={_mask(key)}）{guard_note}", backup
```

- 拒绝文案改为**带上具体原因**，不再一律让人去改 `.gitignore`
- 守卫被跳过时，成功消息里**显式带出原因**，不静默放行

## 主要改动文件

| 文件 | 改动 |
| --- | --- |
| `repro_tui.py` | 修改 `_gitignored()`（约 12 行 → 22 行）+ `write_env_key()` 3 行 |

不改 `config.py` / `.gitignore` / `fetch_data.py`；**不动** #2~#5、#10（用户明确只要最小改动）。

## 验证

| # | 验证 | 期望 |
| --- | --- | --- |
| 1 | `py_compile` | 通过 |
| 2 | 真实仓库里的 `ROOT/.env` | `(True, "…已被 .gitignore 屏蔽…")` — 与修前一致 |
| 3 | 真实仓库里的 `data/_guard_test.env` | `(False, "…未被 .gitignore 屏蔽…")`，且**拒绝落盘** — 守卫仍然有效 |
| 4 | **临时 `git init` 仓库 + 无 .gitignore** | `(False, "…未被 .gitignore 屏蔽…")` |
| 5 | **临时 `git init` 仓库 + `.gitignore` 含 .env** | `(True, "…已被 .gitignore 屏蔽…")` |
| 6 | **无 `.git` 的目录（模拟 ZIP 副本，monkeypatch ROOT）** | `(True, "未检测到 git 仓库…")` ← **这就是本次修的 bug** |
| 7 | 仓库外路径 | `(True, "路径在仓库外…")` |
| 8 | `--check` + `ls .env` | 真 `.env` **仍不存在**（测试未污染真实配置） |

全部在 `/tmp` 与现状文件上做，**不碰真实 `.env`**。

## 明确不做（后续再说）

- 不修 #2（as_posix 已顺带）、#3（Windows chmod）、#4（shlex 显示）、#5（UTF-8 输出）、
  #10（Windows 目录 rename 重试）—— 用户选择只修 #1
- 不动项目本体的 `data.py` SQLite URI（#6，需 Windows 实测）
- 不修 `fetch_data.py` 的 zip-slip 驱动器号检查（#11，低风险）

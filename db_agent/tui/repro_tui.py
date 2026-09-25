#!/usr/bin/env python3
import argparse
import asyncio
import importlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- 依赖自检
try:
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import (Button, Footer, Header, Input, Label,
                                 OptionList, ProgressBar, RichLog,
                                 SelectionList, Static, Switch)
    from textual.widgets.option_list import Option
except ImportError as e:
    sys.exit(f"缺少依赖：{e}\n请先执行：\n"
             f"  {sys.executable} -m pip install textual==8.2.8 "
             f"-i https://pypi.tuna.tsinghua.edu.cn/simple")

from db_agent import config

# ---------------------------------------------------------------- 常量
ROOT      = config.ROOT
PY        = sys.executable
RESULTS   = config.RESULTS_DIR
TUI_DIR   = RESULTS / ".tui"
LEDGER    = TUI_DIR / "state.json"
ANALYZE_MD = RESULTS / "analyze.md"

BACKUP_DIR = Path.home() / ".db_agent_env_backups"
KEY_HINT = "sk-"

FULL_ROWS  = 500
DB_COUNT   = 11
SMALL_FILES = ("mini_dev_sqlite.json", "mini_dev_sqlite_gold.sql",
               "mini_dev_sqlite.jsonl", "dev_tables.json")
GROUP_COST = {"O1": 0.4, "O3": 1.2, "A1": 5.3}
GROUP_ORDER = ("O1", "O3", "A1")
DEPS = ("openai", "sqlglot", "pandas", "tqdm", "textual")

RE_TQDM = re.compile(r"(\d[\d,]*)/(\d[\d,]*)\s*[\[\(]")

DONE, PARTIAL, FAIL, TODO, INFO = "done", "partial", "fail", "todo", "info"
ICON = {DONE: "✓", PARTIAL: "◐", FAIL: "✗", TODO: "○", INFO: "i"}
COLOR = {DONE: "green", PARTIAL: "yellow", FAIL: "red", TODO: "grey50", INFO: "cyan"}


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    info: str
    runnable: bool = True


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


# ---------------------------------------------------------------- 上下文
@dataclass
class Ctx:
    groups: tuple = GROUP_ORDER
    concurrency: int = 20
    limit: int | None = None
    gate2_full: bool = False

    @property
    def suffix(self):
        return "" if self.limit is None else "_smoke"

    def raw(self, g):
        return RESULTS / f"{g}{self.suffix}.jsonl"

    def scored(self, g):
        return RESULTS / f"{g}{self.suffix}_scored.jsonl"


# ---------------------------------------------------------------- 工具
def count_lines(p):
    try:
        with open(p, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def load_ledger():
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_ledger(d):
    TUI_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _mask(key):
    return f"{key[:6]}...{key[-4:]} (len={len(key)})" if len(key) > 12 else "***"


def mask_key():
    p = ROOT / ".env"
    if not p.exists():
        return None
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == config.API_KEY_ENV:
                v = v.strip().strip('"').strip("'")
                return _mask(v) if v else None
    except OSError:
        return None
    return None


def _gitignored(path):
    try:
        rel = Path(path).resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return True, "路径在仓库外，不存在被提交的风险"

    if not (ROOT / ".git").exists():
        return True, "未检测到 git 仓库（可能是 ZIP 下载的副本），跳过 gitignore 守卫"

    try:
        r = subprocess.run(["git", "check-ignore", "-q", "--", rel],
                           cwd=str(ROOT), capture_output=True, timeout=10)
    except (FileNotFoundError, OSError) as e:
        return False, f"仓库存在但无法调用 git（{e}），无法确认安全性"
    except subprocess.TimeoutExpired:
        return False, "git check-ignore 超时，无法确认安全性"

    if r.returncode == 0:
        return True, f"{rel} 已被 .gitignore 屏蔽"
    if r.returncode == 1:
        return False, f"{rel} 未被 .gitignore 屏蔽，写进去可能被提交进版本库"
    return False, f"git check-ignore 返回 {r.returncode}，无法确认安全性"


def write_env_key(key, env_path=None, backup_dir=None):
    key = (key or "").strip()
    if not key:
        return False, "密钥为空，未写入。", None
    env_path = Path(env_path) if env_path else (ROOT / ".env")
    backup_dir = Path(backup_dir) if backup_dir else BACKUP_DIR

    safe, why = _gitignored(env_path)
    if not safe:
        return False, f"拒绝写入：{why}。", None
    guard_note = "" if "屏蔽" in why else f"（{why}）"

    old = ""
    if env_path.exists():
        try:
            old = env_path.read_text(encoding="utf-8")
        except OSError as e:
            return False, f"读取 {env_path} 失败：{e}", None

    backup = None
    if env_path.exists():
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"{env_path.name}.{time.strftime('%Y%m%d%H%M%S')}.bak"
            shutil.copy2(env_path, backup)
            os.chmod(backup, 0o600)
        except OSError as e:
            return False, f"备份失败，已中止（不覆盖原文件）：{e}", None

    new_line = f"{config.API_KEY_ENV}={key}"
    lines = old.splitlines()
    hit = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("#") or "=" not in s:
            continue
        if s.split("=", 1)[0].strip() == config.API_KEY_ENV:
            lines[i] = new_line
            hit = True
            break
    if not hit:
        lines.append(new_line)
    content = "\n".join(lines).rstrip("\n") + "\n"

    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = env_path.parent / f".{env_path.name}.tmp"
        tmp.write_text(content, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
        os.chmod(env_path, 0o600)
    except OSError as e:
        return False, f"写入失败：{e}", backup

    return True, f"已写入 {env_path}（{config.API_KEY_ENV}={_mask(key)}）{guard_note}", backup


def missing_deps():
    out = []
    for m in DEPS:
        try:
            importlib.import_module(m)
        except Exception:
            out.append(m)
    return out


def pairs_of(groups):
    gs = [g for g in GROUP_ORDER if g in groups]
    return [(gs[i], gs[j]) for i in range(len(gs)) for j in range(i + 1, len(gs))]


# ---------------------------------------------------------------- 状态推导
def detect(ctx: Ctx):
    led = load_ledger()
    st = {}

    # --- 环境 ---
    miss = missing_deps()
    st["env"] = ((DONE, f"{sys.version.split()[0]} · 5 依赖齐全") if not miss
                 else (FAIL, f"缺 {', '.join(miss)}"))

    # --- 密钥 ---
    mk = mask_key()
    st["secret"] = ((DONE, f"{config.API_KEY_ENV}={mk}") if mk
                    else (FAIL, f".env 缺 {config.API_KEY_ENV}"))

    # --- 数据 ---
    db = config.DB_DIR
    dirs = sorted(d.name for d in db.iterdir() if d.is_dir()) if db.exists() else []
    sqlites = sorted(db.glob("*/*.sqlite")) if db.exists() else []
    missf = [f for f in SMALL_FILES if not (config.DATA_DIR / f).exists()]
    if len(dirs) == DB_COUNT and len(sqlites) == DB_COUNT and not missf:
        st["data"] = (DONE, f"{len(dirs)} 库 / {len(sqlites)} sqlite / 4 小文件")
    elif not dirs and not sqlites:
        st["data"] = (TODO, "dev_databases 缺失 → 跑 python -m db_agent fetch")
    else:
        st["data"] = (PARTIAL, f"{len(dirs)} 库 / {len(sqlites)} sqlite"
                              + (f" / 缺 {missf}" if missf else ""))

    def gate(key, extra=""):
        rec = led.get(key)
        if not rec:
            return (TODO, "未运行")
        return ((DONE, f"exit={rec.get('code')} · {rec.get('at', '')} {extra}")
                if rec.get("code") == 0 else
                (FAIL, f"exit={rec.get('code')} · {rec.get('at', '')} {extra}"))

    st["gate1"] = gate("gate1")
    if (RESULTS / "env.json").exists() and led.get("gate0", {}).get("code") == 0:
        st["gate0"] = (DONE, "env.json 已落盘 + exit=0")
    else:
        st["gate0"] = gate("gate0", "(env.json 缺失)" if not (RESULTS / "env.json").exists() else "")
    st["gate2"] = gate("gate2", "含真实调用" if led.get("gate2", {}).get("mode") == "full" else "离线")

    # --- 跑组 ---
    rows = {g: count_lines(ctx.raw(g)) for g in ctx.groups}
    want = ctx.limit or FULL_ROWS
    if rows and all(v >= want for v in rows.values()):
        st["run"] = (DONE, " ".join(f"{g}:{v}" for g, v in rows.items()))
    elif any(v for v in rows.values()):
        st["run"] = (PARTIAL, " ".join(f"{g}:{v}/{want}" for g, v in rows.items())
                              + "  → 可断点续跑")
    else:
        st["run"] = (TODO, f"目标 {want} 题/组：" + "/".join(ctx.groups))

    # --- 判分 ---
    srows = {g: count_lines(ctx.scored(g)) for g in ctx.groups}
    if srows and all(v >= want for v in srows.values()):
        st["score"] = (DONE, " ".join(f"{g}:{v}" for g, v in srows.items()))
    elif any(v for v in srows.values()):
        st["score"] = (PARTIAL, " ".join(f"{g}:{v}/{want}" for g, v in srows.items()))
    else:
        st["score"] = (TODO, "未判分")

    # --- 配对 + 离线分析 ---
    ps = pairs_of(ctx.groups)
    missing = [f"{a}-{b}" for a, b in ps
               if not (TUI_DIR / f"paired_{a}_{b}{ctx.suffix}.txt").exists()]
    has_an = ANALYZE_MD.exists()
    if ps and not missing and has_an:
        st["pair"] = (DONE, f"{len(ps)} 组配对 + analyze.md")
    elif not missing and not ps:
        st["pair"] = (TODO, "至少选 2 个组才能配对")
    else:
        st["pair"] = (PARTIAL if (len(missing) < len(ps) or has_an) else TODO,
                      f"待配对 {len(missing)}/{len(ps)}"
                      + ("" if has_an else " · 无 analyze.md"))
    return st


# ---------------------------------------------------------------- 命令构造
def build_job(step: Step, ctx: Ctx):
    k = step.key
    if k == "data":
        return [([PY, "-m", "db_agent", "fetch"], None)]
    if k == "gate1":
        return [([PY, "-m", "db_agent", "gate", "1"], None)]
    if k == "gate0":
        return [([PY, "-m", "db_agent", "gate", "0"], None)]
    if k == "gate2":
        args = [PY, "-m", "db_agent", "gate", "2"] + ([] if ctx.gate2_full else ["--offline"])
        return [(args, None)]
    if k == "run":
        job = []
        for g in ctx.groups:
            a = [PY, "-m", "db_agent", "run", "--group", g, "--concurrency", str(ctx.concurrency)]
            if ctx.limit:
                a += ["--limit", str(ctx.limit), "--out", ctx.raw(g).name]
            job.append((a, None))
        return job
    if k == "score":
        return [([PY, "-m", "db_agent", "score", "--group", g,
                  *(("--out", ctx.raw(g).name) if ctx.limit else ())], None)
                for g in ctx.groups]
    if k == "pair":
        job = [([PY, "-m", "db_agent", "pair", ctx.scored(a).name, ctx.scored(b).name],
                TUI_DIR / f"paired_{a}_{b}{ctx.suffix}.txt") for a, b in pairs_of(ctx.groups)]
        job.append(([PY, "-m", "db_agent", "analyze", "--results-dir", "results",
                     "--out", ANALYZE_MD.name], None))
        return job
    return []


def cost_of(step: Step, ctx: Ctx):
    if step.key != "run":
        return 0.0
    ratio = (ctx.limit / FULL_ROWS) if ctx.limit else 1.0
    return sum(GROUP_COST.get(g, 0) for g in ctx.groups) * ratio


def yuan(v):
    return f"¥{v:.2f}" if v < 1 else f"¥{v:.1f}"


def explain(step: Step, ctx: Ctx, state):
    if step.key == "env":
        miss = missing_deps()
        if not miss:
            return "环境已就绪，无需操作。"
        return ("缺依赖：" + ", ".join(miss) + "\n\n修复：\n"
                f"  {PY} -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "
                "openai==3.16.2 sqlglot==30.18.0 pandas==3.0.6 tqdm==4.70.1 "
                "matplotlib==3.11.2 python-dotenv==1.2.3 func_timeout "
                "psycopg2-binary pymysql")
    if step.key == "secret":
        return (f"按 d 打开输入框，粘贴 {config.API_KEY_ENV}（明文输入，日志只显示掩码）。\n\n"
                f"落点   : {ROOT / '.env'}（权限 600）\n"
                f"备份   : 旧文件备份到仓库外 {BACKUP_DIR}\n"
                "保险   : 若 .env 未被 .gitignore 屏蔽则拒绝写入\n\n"
                "录完接着选「Gate 0」按 d，用 2 次真实调用验证密钥可用。")
    return step.info


# ---------------------------------------------------------------- 子进程
class Child:

    def __init__(self, argv, tee=None):
        self.argv = argv
        self.tee = tee
        self.proc = None

    async def stream(self, on_text):
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=str(ROOT), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        fh = None
        if self.tee:
            self.tee.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.tee, "w", encoding="utf-8")
        buf = ""
        try:
            while True:
                chunk = await self.proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                pieces = re.split(r"[\r\n]", buf)
                buf = pieces.pop()
                for p in pieces:
                    if p.strip():
                        on_text(p)
                        if fh:
                            fh.write(p + "\n")
            if buf.strip():
                on_text(buf)
                if fh:
                    fh.write(buf + "\n")
        finally:
            if fh:
                fh.close()
        await self.proc.wait()
        return self.proc.returncode

    def terminate(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()

    def kill(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()


# ---------------------------------------------------------------- 确认弹窗
class Confirm(ModalScreen):
    CSS = """
    Confirm { align: center middle; }
    #box { width: 66; height: auto; border: thick $error; background: $surface; padding: 1 2; }
    #ctitle { text-style: bold; color: $error; }
    #cbody { margin: 1 0; }
    #cbtns { height: auto; align-horizontal: right; }
    #cbtns Button { margin-left: 2; }
    """
    BINDINGS = [Binding("escape", "no", "取消"), Binding("y", "yes", "确认")]

    def __init__(self, title, body):
        super().__init__()
        self._t, self._b = title, body

    def compose(self) -> ComposeResult:
        with Container(id="box"):
            yield Label(self._t, id="ctitle")
            yield Static(self._b, id="cbody")
            with Horizontal(id="cbtns"):
                yield Button("取消", id="no")
                yield Button("确认执行", id="yes", variant="error")

    def on_button_pressed(self, e):
        self.dismiss(e.button.id == "yes")

    def action_no(self):
        self.dismiss(False)

    def action_yes(self):
        self.dismiss(True)


# ---------------------------------------------------------------- 密钥录入
class KeyScreen(ModalScreen):
    CSS = """
    KeyScreen { align: center middle; }
    #kbox { width: 76; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    #ktitle { text-style: bold; }
    #kmsg { height: auto; margin: 1 0; }
    #kerr { height: auto; color: $text-muted; }
    #kbtns { height: auto; align-horizontal: right; margin-top: 1; }
    #kbtns Button { margin-left: 2; }
    """
    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, env_path, note=""):
        super().__init__()
        self.env_path = Path(env_path)
        self.note = note

    def compose(self) -> ComposeResult:
        with Container(id="kbox"):
            yield Label(f"配置 {config.API_KEY_ENV}", id="ktitle")
            yield Static(f"写入 [b]{self.env_path}[/b]（权限 600）；"
                         f"旧文件备份到仓库外 [b]{BACKUP_DIR}[/b]。", id="kmsg")
            if self.note:
                yield Static(f"[yellow]⚠ {self.note}[/]")
            yield Input(placeholder="sk-...", id="kinput")
            yield Static("回车或点「保存」提交；明文输入，日志只显示掩码。", id="kerr")
            with Horizontal(id="kbtns"):
                yield Button("取消", id="kcancel")
                yield Button("保存", id="ksave", variant="primary")

    def on_mount(self):
        self.query_one("#kinput", Input).focus()

    def _save(self):
        key = self.query_one("#kinput", Input).value.strip()
        err = self.query_one("#kerr", Static)
        if not key:
            err.update("[red]密钥为空，请粘贴后重试。[/]")
            return
        if len(key) < 20:
            err.update(f"[red]长度只有 {len(key)}，看起来不是一个完整密钥。[/]")
            return
        ok, msg, backup = write_env_key(key, self.env_path)
        if not ok:
            err.update(f"[red]{msg}[/]")
            return
        self.dismiss({
            "msg": msg,
            "backup": str(backup) if backup else None,
            "warn": "" if key.startswith(KEY_HINT) else
                    f"密钥不以 {KEY_HINT} 开头，请确认没粘错。",
        })

    def on_button_pressed(self, e):
        if e.button.id == "ksave":
            self._save()
        else:
            self.dismiss(None)

    def on_input_submitted(self, _):
        self._save()

    def action_cancel(self):
        self.dismiss(None)


# ---------------------------------------------------------------- App
class ReproApp(App):
    TITLE = "repro · BIRD Mini-Dev (SQLite)"
    SUB_TITLE = f"模型 {config.MODEL} · 500 题 / 11 库"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("r", "refresh_steps", "刷新"),
        Binding("s", "stop", "停止"),
        Binding("d", "run_selected", "执行选中"),
    ]
    CSS = """
    #side { width: 40; border-right: solid $accent; padding: 0 1; overflow-y: auto; }
    #main { width: 1fr; }
    .hint { color: $text-muted; }
    #steps { height: auto; max-height: 12; }
    #groups { height: auto; }
    #opts { height: 3; }
    #conc { width: 6; } #limit { width: 12; }
    #bar { height: 3; }
    #log { height: 1fr; }
    #go { margin-top: 1; }
    """

    def __init__(self, initial: Ctx):
        super().__init__()
        self.ctx = initial
        self.busy = False
        self.child: Child | None = None
        self._kill_timer = None
        self._highlight = "data"

    # ---------------- 布局
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="side"):
                yield Static("[b]步骤[/b]（↑↓ 选择，d 执行）", classes="hint")
                yield OptionList(id="steps")
                yield Static("[b]分组[/b]（空格勾选）", classes="hint")
                yield SelectionList(
                    ("O1  one-shot      ¥0.4", "O1", True),
                    ("O3  采样投票      ¥1.2", "O3", False),
                    ("A1  agent 闭环    ¥5.3", "A1", True),
                    id="groups", compact=True)
                with Horizontal(id="opts"):
                    yield Input(value=str(self.ctx.concurrency), id="conc",
                                placeholder="并发", type="integer")
                    yield Input(value="", id="limit", placeholder="limit(空=全量)",
                                type="integer")
                yield Switch(value=self.ctx.gate2_full, id="g2full")
                yield Static("Gate 2 含真实 API 调用", classes="hint")
                yield Button("▶ 执行选中步骤", id="go", variant="primary")
                with Horizontal():
                    yield Button("⟳ 刷新", id="refresh")
                    yield Button("■ 停止", id="stop", variant="error", disabled=True)
            with Vertical(id="main"):
                yield RichLog(id="log", wrap=True, markup=True,
                              max_lines=3000, min_width=20)
                yield ProgressBar(total=None, id="bar", show_eta=True)
        yield Footer()

    # ---------------- 生命周期
    def on_mount(self):
        self.refresh_steps()
        self.log_write("[b]repro TUI[/b] 就绪。左侧选步骤后按 [b]d[/b] 或点按钮执行；"
                       "[b]q[/b] 退出，[b]s[/b] 停止当前子进程。")

    # ---------------- 小组件读写
    def log_write(self, text):
        self.query_one("#log", RichLog).write(text)

    def selected_groups(self):
        vals = self.query_one("#groups", SelectionList).selected
        return tuple(g for g in GROUP_ORDER if g in vals)

    def current_ctx(self):
        conc = self.query_one("#conc", Input).value.strip()
        lim = self.query_one("#limit", Input).value.strip()
        return Ctx(groups=self.selected_groups() or GROUP_ORDER,
                   concurrency=int(conc) if conc.isdigit() else 20,
                   limit=int(lim) if lim.isdigit() and int(lim) > 0 else None,
                   gate2_full=self.query_one("#g2full", Switch).value)

    def set_busy(self, busy):
        self.busy = busy
        self.query_one("#go", Button).disabled = busy
        self.query_one("#refresh", Button).disabled = busy
        self.query_one("#stop", Button).disabled = not busy

    def highlighted_step(self):
        ol = self.query_one("#steps", OptionList)
        if ol.highlighted is None or ol.option_count == 0:
            return STEPS[0]
        idx = min(ol.highlighted, len(STEPS) - 1)
        return STEPS[idx]

    # ---------------- 状态刷新
    def refresh_steps(self):
        try:
            self.ctx = self.current_ctx()
        except Exception:
            pass
        st = detect(self.ctx)
        ol = self.query_one("#steps", OptionList)
        ol.clear_options()
        for i, s in enumerate(STEPS, 1):
            state, detail = st[s.key]
            ol.add_option(Option(
                f"[{COLOR[state]}]{ICON[state]}[/] {i} {s.title:<10} "
                f"[{COLOR[state]}]{detail}[/]",
                id=s.key))
        keys = [s.key for s in STEPS]
        if self._highlight in keys:
            ol.highlighted = keys.index(self._highlight)
        if self.ctx.groups:
            rows = sum(count_lines(self.ctx.raw(g)) for g in self.ctx.groups)
            total = (self.ctx.limit or FULL_ROWS) * len(self.ctx.groups)
            self.query_one("#bar", ProgressBar).update(total=total, progress=rows)

    def action_refresh_steps(self):
        self.refresh_steps()
        self.log_write("[dim]状态已刷新[/]")

    def on_selection_list_selected_changed(self, _):
        self.refresh_steps()

    def on_option_list_option_highlighted(self, e):
        self._highlight = e.option.id or self._highlight

    def on_option_list_option_selected(self, e):
        self._highlight = e.option.id or self._highlight
        step = self.highlighted_step()
        state, detail = detect(self.current_ctx())[step.key]
        self.log_write(f"\n[b]{step.title}[/b] [{COLOR[state]}]{ICON[state]}[/] {detail}")
        self.log_write(f"[dim]{explain(step, self.current_ctx(), state)}[/]")
        if step.runnable:
            self.log_write("[dim]按 d 或点「▶ 执行选中步骤」开始。[/]")

    # ---------------- 执行
    def action_run_selected(self):
        if self.busy:
            self.log_write("[yellow]有任务在跑，先按 s 停止或等它结束。[/]")
            return
        self.run_step(self.highlighted_step())

    def on_button_pressed(self, e):
        if e.button.id == "go":
            self.action_run_selected()
        elif e.button.id == "refresh":
            self.action_refresh_steps()
        elif e.button.id == "stop":
            self.action_stop()

    @work(exclusive=True)
    async def run_step(self, step: Step):
        ctx = self.current_ctx()
        self.ctx = ctx

        if step.key == "secret":
            note = ""
            if os.environ.get(config.API_KEY_ENV):
                note = (f"系统环境变量里已有 {config.API_KEY_ENV}，而 config 加载时"
                        f"【系统变量优先】(override=False)，写 .env 不会立即生效。")
            res = await self.push_screen_wait(KeyScreen(ROOT / ".env", note))
            if not res:
                self.log_write("[yellow]已取消，未改动 .env。[/]")
                return
            self.log_write(f"[green]✓ {res['msg']}[/]")
            if res.get("backup"):
                self.log_write(f"[dim]旧文件已备份到 {res['backup']}[/]")
            if res.get("warn"):
                self.log_write(f"[yellow]{res['warn']}[/]")
            self.log_write("[dim]接着选「Gate 0」按 d，用 2 次真实调用验证密钥可用。[/]")
            self.refresh_steps()
            return

        job = build_job(step, ctx)

        if not job:
            self.log_write(f"\n[b]{step.title}[/b]：无需执行命令。")
            self.log_write(explain(step, ctx, None))
            return

        cost = cost_of(step, ctx)
        lines = [f"步骤：{step.title}", ""]
        for argv, tee in job:
            lines.append("  $ " + " ".join(shlex.quote(a) for a in argv))
            if tee:
                lines.append(f"    → 输出留存 {tee.relative_to(ROOT)}")
        if cost:
            lines += ["", f"[b]预计成本 ≈ {yuan(cost)}[/b]（README 口径，"
                          "实际按当期单价与缓存命中浮动）"
                          + (f"；冒烟按 {ctx.limit}/{FULL_ROWS} 题折算" if ctx.limit else "")]
        body = "\n".join(lines)
        if cost or step.key in ("data", "run"):
            ok = await self.push_screen_wait(
                Confirm(f"确认执行「{step.title}」？" if not cost else
                        f"确认执行「{step.title}」？预计花费 ≈ {yuan(cost)}", body))
            if not ok:
                self.log_write("[yellow]已取消。[/]")
                return

        self.set_busy(True)
        self.log_write(f"\n[bold cyan]▶ {step.title}[/]")
        rc_all = 0
        try:
            for argv, tee in job:
                self.log_write(f"[cyan]$ {' '.join(shlex.quote(a) for a in argv)}[/]")
                self.child = Child(argv, tee)
                rc = await self.child.stream(self.on_child_text)
                if rc != 0:
                    rc_all = rc
                    self.log_write(f"[red]✗ 退出码 {rc}，中止本步骤后续命令。[/]")
                    break
                self.log_write(f"[green]✓ 完成（exit=0）[/]")
        except asyncio.CancelledError:
            rc_all = -1
            self.log_write("[yellow]已中断。[/]")
            raise
        finally:
            self.child = None
            self.set_busy(False)
            self.query_one("#bar", ProgressBar).update(total=None, progress=0)

        led = load_ledger()
        led[step.key] = {"code": rc_all, "at": time.strftime("%m-%d %H:%M"),
                         "cmd": " ".join(job[-1][0]), "mode":
                         "full" if (step.key == "gate2" and ctx.gate2_full) else
                         ("smoke" if ctx.limit else "full")}
        save_ledger(led)
        self.refresh_steps()
        self.log_write(f"[bold]{step.title}[/] 结束，退出码 {rc_all}。")

    def on_child_text(self, text):
        m = RE_TQDM.search(text)
        if m:
            try:
                cur = int(m.group(1).replace(",", ""))
                tot = int(m.group(2).replace(",", ""))
            except ValueError:
                cur = tot = 0
            if tot > 0:
                self.query_one("#bar", ProgressBar).update(total=tot, progress=min(cur, tot))
                return
        self.log_write(text)

    def action_stop(self):
        if not self.busy or not self.child:
            self.log_write("[dim]当前没有在跑的任务。[/]")
            return
        self.log_write("[yellow]正在停止子进程…[/]")
        self.child.terminate()
        self._kill_timer = self.set_timer(5.0, self._hard_kill)

    def _hard_kill(self):
        if self.child:
            self.child.kill()
            self.log_write("[red]子进程未响应 TERM，已 KILL。[/]")


def run_check() -> int:
    ctx = Ctx(groups=GROUP_ORDER, concurrency=20, limit=None, gate2_full=False)
    st = detect(ctx)
    print("=" * 78)
    print("repro_tui --check · 复刻流程状态表（非交互，不花钱）")
    print("=" * 78)
    print(f"  项目根 : {ROOT}")
    print(f"  解释器 : {PY}")
    print(f"  分组   : {' '.join(ctx.groups)}   全量 {FULL_ROWS} 题")
    print("-" * 78)
    for i, s in enumerate(STEPS, 1):
        state, detail = st[s.key]
        print(f"  {ICON[state]} {i} {s.title:<11} {detail}")
    print("-" * 78)
    todo = [s.title for s in STEPS if st[s.key][0] in (TODO, FAIL, PARTIAL)]
    print("  待办：" + ("、".join(todo) if todo else "无，全流程已完成"))
    print("=" * 78)
    return 0


def main():
    ap = argparse.ArgumentParser(description="复刻流程傻瓜式 TUI")
    ap.add_argument("--check", action="store_true",
                    help="非交互：打印步骤状态表后退出（不启动 TUI，不花钱）")
    ap.add_argument("--set-key", action="store_true",
                    help="从 stdin 读取密钥写入 .env（明文绝不经 argv，避免进 shell history）")
    ap.add_argument("--env-path", default=None,
                    help=".env 落点（默认 <项目根>/.env）")
    a = ap.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)

    if a.set_key:
        key = sys.stdin.readline().strip()
        if not key:
            print("stdin 未读到密钥。用法："
                  "printf 'sk-...' | python -m db_agent set-key", file=sys.stderr)
            return 2
        ok, msg, backup = write_env_key(key, a.env_path)
        print(msg)
        if backup:
            print(f"旧文件已备份到 {backup}")
        return 0 if ok else 1

    if a.check:
        return run_check()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("检测到 stdio 不是终端，无法启动全屏 TUI。\n"
              "  非交互场景请用：python -m db_agent check\n"
              "  或直接用统一入口：python -m db_agent {run,score,pair,analyze,gate,check}",
              file=sys.stderr)
        return 2
    ReproApp(Ctx()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

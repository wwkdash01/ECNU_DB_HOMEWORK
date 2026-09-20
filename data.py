"""data.py —— 数据加载与 schema 上下文构建

相对手册（§1.6）的三处实测调整：
  1. mini_dev_sqlite.json 顶层是 list（手册兼容 dict 的写法保留作兜底）
  2. question_id 已存在且【有 2 个重复值】（137/138 各出现两次），不能用作断点续跑的 key
     → 新增 qidx（0..499 下标）作为唯一标识；JSON[i] 与 gold 第 i 行已实测 500/500 对齐
  3. database_description 的 CSV 列名已实测为
     original_column_name,column_name,column_description,data_format,value_description
     （与手册假设一致，无需修改）
"""
import json, csv, sqlite3
from functools import lru_cache
import config


def load_questions():
    d = json.load(open(config.QUESTIONS_FILE, encoding="utf-8"))
    if isinstance(d, dict):                 # HF datasets 有时给 dict
        d = list(d.values())
    for i, x in enumerate(d):
        x.setdefault("question_id", i)      # 实测已存在，此处仅为兜底
        x["qidx"] = i                       # 唯一标识：JSON[i] 与 gold 第 i 行对齐（已验证）
    return d


def db_path(db_id):
    return config.DB_DIR / db_id / f"{db_id}.sqlite"


def get_conn(db_id):
    """只读连接。

    ★ WAL 回退（本仓库实测）：
      card_games 是【WAL 模式】库（文件头第 19 字节 = 02，其余 10 个库 = 01）。
      WAL 库在只读打开时需要创建 -shm/-wal 辅助文件，无写权限时直接报
      "unable to open database file"。故 mode=ro 失败时回退 immutable=1 ——
      它同样是只读保证（SQLite 承诺不改动文件，且跳过 journal 检查），
      只是要求连接期间文件不被其他进程改写；评测数据是静态的，满足该条件。
    """
    p = db_path(db_id)
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.execute("SELECT 1").fetchone()   # connect 是惰性的，必须真跑一次才知道成败
        return conn
    except sqlite3.Error:
        return sqlite3.connect(f"file:{p}?immutable=1", uri=True)


@lru_cache(maxsize=None)
def schema_whitelist(db_id):
    """{table: set(cols)} —— 工具参数校验 + 幻觉检测"""
    conn = get_conn(db_id)
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
    conn = get_conn(db_id)
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
    ).fetchall()
    conn.close()
    return "\n".join(sorted(r[0] for r in rows))


@lru_cache(maxsize=None)
def desc_index(db_id):
    """{(table, col): (column_description, value_description)}

    CSV 列名已实测（california_schools/...，含 UTF-8 BOM）：
      original_column_name, column_name, column_description, data_format, value_description
    注意两点（均为实测）：
      * utf-8-sig：BOM 会让首列名变成 '\\ufefforiginal_column_name'，必须用 sig 解码
      * 编码混杂：european_football_2/{Player,Team}_Attributes.csv、formula_1/qualifying.csv、
        student_club/Budget.csv 是 **cp1252** 而非 UTF-8；不处理会在这些库上抛
        UnicodeDecodeError，导致 A3 整题失败
    """
    idx, ddir = {}, config.DB_DIR / db_id / "database_description"
    if not ddir.exists():
        return idx
    for f in sorted(ddir.glob("*.csv")):
        rows = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):   # latin-1 永不失败，作最终兜底
            try:
                with open(f, encoding=enc) as fh:
                    rows = list(csv.DictReader(fh))
                break
            except UnicodeDecodeError:
                continue
        if rows is None:
            continue
        for row in rows:
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

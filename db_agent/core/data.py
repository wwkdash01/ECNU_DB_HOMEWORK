import json, sqlite3
from functools import lru_cache
from db_agent import config


def load_questions():
    d = json.load(open(config.QUESTIONS_FILE, encoding="utf-8"))
    if isinstance(d, dict):
        d = list(d.values())
    for i, x in enumerate(d):
        x.setdefault("question_id", i)
        x["qidx"] = i
    return d


def db_path(db_id):
    return config.DB_DIR / db_id / f"{db_id}.sqlite"


def get_conn(db_id):
    p = db_path(db_id)
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.execute("SELECT 1").fetchone()
        return conn
    except sqlite3.Error:
        return sqlite3.connect(f"file:{p}?immutable=1", uri=True)


@lru_cache(maxsize=None)
def schema_whitelist(db_id):
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
    conn = get_conn(db_id)
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
    ).fetchall()
    conn.close()
    return "\n".join(sorted(r[0] for r in rows))


def build_context(db_id, evidence):
    ev = (evidence or "").strip() or "None"
    return f"Database schema:\n{schema_text(db_id)}\n\nExternal knowledge:\n{ev}\n\n"

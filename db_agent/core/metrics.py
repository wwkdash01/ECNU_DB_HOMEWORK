"""metrics.py —— 列级召回 与 幻觉率

两个指标都需要从 SQL 里解析出「引用了哪些表 / 哪些列」，不能用正则：
  * `WHERE x = 'foo'` 里的字符串字面量会被误当列名
  * 表别名、子查询别名、CTE 名都不是真实表名
  * `SELECT *` 解析不出列

★ 方言必须用 sqlite（实测）：500 条 gold SQL 中
    dialect='sqlite' -> 500/500 解析成功
    dialect=None     -> 457/500（43 条失败，SQLite 特有语法：反引号含空格、STRFTIME 等）
  用默认方言会静默丢掉 8.6% 的样本。

★ 解析失败的样本一律【单列统计，不混入分母】（手册原则），
   否则会把"解析不了"错算成"模型引用了幻觉列"。
"""
import sqlglot
from sqlglot import exp

from db_agent import config
from db_agent.core.data import schema_whitelist

DIALECT = "sqlite"


def parse(sql):
    """解析单条 SQL；失败返回 None（调用方负责单列）。"""
    if not sql or not str(sql).strip():
        return None
    try:
        return sqlglot.parse_one(sql, read=DIALECT)
    except Exception:
        return None


def _cte_names(tree):
    """收集 CTE 名与其列别名 —— 它们不是真实表/列，必须排除。

    实测：`WITH w AS (SELECT a FROM t) SELECT * FROM w` 中 w 会被
    find_all(exp.Table) 当成表名，若不排除会被误判为幻觉表。
    """
    names = set()
    for cte in tree.find_all(exp.CTE):
        alias = cte.alias
        if alias:
            names.add(alias)
        for c in cte.find_all(exp.Column):
            if c.name:
                names.add(c.name)
    return names


def tables_columns(sql):
    """返回 (tables:set, columns:set)；解析失败返回 (None, None)。"""
    tree = parse(sql)
    if tree is None:
        return None, None
    cte = _cte_names(tree)
    tables = {t.name for t in tree.find_all(exp.Table) if t.name and t.name not in cte}
    cols = {c.name for c in tree.find_all(exp.Column) if c.name and c.name not in cte}
    return tables, cols


def _norm(s):
    return str(s).strip().lower()


def column_recall(pred_sql, gold_sql, db_id=None):
    """列级召回 = |生成列 ∩ gold列| / |gold列|。

    返回 (recall, reason)：
      recall 为 None 表示【无法计算】而非 0 分（reason 说明原因）。
    """
    if not pred_sql or not str(pred_sql).strip():
        return None, "pred_empty"
    g_tabs, g_cols = tables_columns(gold_sql)
    p_tabs, p_cols = tables_columns(pred_sql)
    if g_cols is None:
        return None, "gold_parse_error"
    if p_cols is None:
        return None, "pred_parse_error"
    if not g_cols:
        # gold 用 SELECT * 之类，解析不出具体列 —— 单列，不混入分母
        return None, "gold_no_columns"

    # 归一化比较：SQLite 列名大小写不敏感
    gset = {_norm(c) for c in g_cols}
    pset = {_norm(c) for c in p_cols}
    return len(pset & gset) / len(gset), "ok"


def hallucination(pred_sql, db_id):
    """幻觉率：引用了白名单外的表/列的比例。

    返回 (rate, detail)：
      rate = 幻觉项数 / 引用项总数，为 None 表示无法计算。
      detail 含具体的幻觉项，便于报告里举实例。
    """
    if not pred_sql or not str(pred_sql).strip():
        return None, {"reason": "pred_empty"}
    tabs, cols = tables_columns(pred_sql)
    if tabs is None:
        return None, {"reason": "parse_error"}

    wl = schema_whitelist(db_id)
    known_tables = {_norm(t) for t in wl}
    known_cols = {_norm(c) for t in wl for c in wl[t]}

    bad_tables = sorted(t for t in tabs if _norm(t) not in known_tables)
    bad_cols = sorted(c for c in cols if _norm(c) not in known_cols)

    total = len(tabs) + len(cols)
    if total == 0:
        return None, {"reason": "no_references"}
    n_bad = len(bad_tables) + len(bad_cols)
    return n_bad / total, {"bad_tables": bad_tables, "bad_columns": bad_cols,
                           "n_tables": len(tabs), "n_columns": len(cols)}

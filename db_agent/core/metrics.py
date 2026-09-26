import sqlglot
from sqlglot import exp

from db_agent import config
from db_agent.core.data import schema_whitelist

DIALECT = "sqlite"


def parse(sql):
    if not sql or not str(sql).strip():
        return None
    try:
        return sqlglot.parse_one(sql, read=DIALECT)
    except Exception:
        return None


def _cte_names(tree):
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
    if not pred_sql or not str(pred_sql).strip():
        return None, "pred_empty"
    g_tabs, g_cols = tables_columns(gold_sql)
    p_tabs, p_cols = tables_columns(pred_sql)
    if g_cols is None:
        return None, "gold_parse_error"
    if p_cols is None:
        return None, "pred_parse_error"
    if not g_cols:
        return None, "gold_no_columns"

    gset = {_norm(c) for c in g_cols}
    pset = {_norm(c) for c in p_cols}
    return len(pset & gset) / len(gset), "ok"


def hallucination(pred_sql, db_id):
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

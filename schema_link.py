"""schema_link.py —— 词汇→表的检索与表间连通性（A4 组的新工具）

存在理由（本仓库实测，取代原 A2 的 get_column_values）
────────────────────────────────────────────────────
A1v2 剩余 181 条错误里，**表选错 68 条（37.6%）是最大且最明确的一类**，
其中漏表 40 / 多表 8 / 漏多并存 17。把漏表逐条打出来后，根因不是"模型没看见表"，
而是**模型无法从 DDL 判断两张表该不该一起用**：

  例1 debit_card_specializing「Please list the countries of the gas stations with
      transactions taken place in June, 2013.」
      gold = {gasstations, transactions_1k, yearmonth} / pred = {gasstations, transactions_1k}
      —— yearmonth 只在 (CustomerID, Date) 上聚合，**没有任何外键指向 transactions_1k**，
         模型从 DDL 里根本推不出这个连接，只能靠"两类信息存在共享列 CustomerID"。
  例2 student_club「Which student has been entrusted to manage the budget for the
      Yearly Kickoff?」gold 含 event，而 event 与 budget 之间隔着 link_to_event。

即：缺的是一张**表间连通图**，而不是更多的表名列表。
`get_column_values`（A2）给的是"某列存了什么值"，与"该用哪些表"正交，故停用。

本模块给模型两样它在 DDL 里拿不到的东西
────────────────────────────────────────
1. **共享列**（join 的真实依据）：
   统计每张表与给定表组共享的列名。SQLite 的隐式连接全靠这个，
   而 DDL 里没有外键时它完全不可见。
2. **基数比**（谁是事实表、谁是维度/查找表）：
   yearmonth（~千行）是维度表，transactions_1k（~千行）是事实表。
   再加上"与候选表共享 key 但未被提及"的**桥接表**，正是最常被漏掉的那批。

刻意【不】使用 FK 图做最短路：实测 FK 极稀疏且不可靠 ——
debit_card_specializing 全库仅 2 条 FK（都指向 customers），formula_1 的 FK 也不覆盖
yearmonth 这类聚合表。以 FK 为主会在这道最关键的题上直接失灵。
"""
import sqlite3

import config
from data import get_conn, schema_whitelist, desc_index

# 与问题里的泛指词对齐；这些词命中任何表都无信息量，故不参与打分
STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "for", "to", "from", "with", "and", "or",
    "is", "are", "was", "were", "be", "been", "what", "which", "who", "whom", "whose",
    "how", "many", "much", "list", "show", "give", "find", "get", "all", "each", "every",
    "that", "this", "these", "those", "there", "their", "his", "her", "its", "it",
    "by", "as", "not", "no", "than", "then", "more", "most", "less", "least", "other",
    "number", "count", "total", "name", "names", "value", "values", "please", "state",
    "identify", "return", "based", "between", "during", "per", "if", "do", "does", "did",
    "have", "has", "had", "can", "could", "will", "would", "should", "we", "you", "they",
    "table", "column", "record", "records", "row", "rows", "data", "database", "info",
}

# 缩写展开：BIRD 里大量列名是压缩写法，问题用的是全词，直接子串匹配会全漏
ABBREV = {
    "description": ("desc", "descr"), "number": ("num", "no", "nbr"),
    "identifier": ("id",), "identification": ("id",), "amount": ("amt",),
    "quantity": ("qty",), "date": ("dt",), "birth": ("birth", "dob"),
    "maximum": ("max",), "minimum": ("min",), "average": ("avg",),
    "percent": ("pct", "perc"), "percentage": ("pct", "perc"),
    "customer": ("cust",), "transaction": ("trans", "txn"),
    "account": ("acct",), "balance": ("bal",), "telephone": ("phone", "tel"),
    "address": ("addr",), "category": ("cat",), "department": ("dept",),
    "manager": ("mgr",), "organization": ("org",), "information": ("info",),
    "temperature": ("temp",), "pressure": ("press",), "measurement": ("meas",),
    "male": ("sex", "gender"), "female": ("sex", "gender"),
    "doctor": ("physician",), "patient": ("pt",), "student": ("pupil",),
    "school": ("schl",), "district": ("dist",), "county": ("cnty",),
}

_ROWCOUNT_CACHE = {}


def _rowcounts(db_id):
    """{table: n} —— 全库仅 79 张表，实测最坏 499ms（card_games），可接受。

    给模型基数是为了让它区分事实表与维度表：yearmonth/gasstations 是维度表，
    transactions_1k 是事实表；3 表题的漏表几乎都是"小维度表"。
    """
    if db_id in _ROWCOUNT_CACHE:
        return _ROWCOUNT_CACHE[db_id]
    out = {}
    conn = get_conn(db_id)
    try:
        for t in sorted(schema_whitelist(db_id)):
            try:
                out[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                out[t] = None
    finally:
        conn.close()
    _ROWCOUNT_CACHE[db_id] = out
    return out


def _stem(w):
    """极轻量归一：去复数。不引入 nltk —— 这层只需处理 table/table(s) 的对齐。"""
    w = w.lower()
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("es") and not w.endswith("ses"):
        return w[:-2]
    if len(w) > 2 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _terms(text):
    """把问题/关键词切成归一化词元（匹配用）。"""
    raw = []
    for ch in str(text).lower():
        raw.append(ch if (ch.isalnum() or ch == "_") else " ")
    words = "".join(raw).split()
    out = set()
    for w in words:
        w = w.strip("_")
        if len(w) < 3 or w in STOPWORDS:
            continue
        out.add(_stem(w))
        out.add(w)                                   # 原形也留一份
        for full, shorts in ABBREV.items():          # 全词 -> 连同其缩写一起作为词元
            if w == full or _stem(w) == _stem(full):
                out.update(shorts)
    return {t for t in out if len(t) >= 3 or t == "id"}


def _match_table(table, cols, terms, desc):
    """(命中词集合, 命中列集合)。表名与列名都算；列说明文档也算（A3 才有的资料在此复用）。"""
    hits, hit_cols = set(), set()
    tname = table.lower()
    for term in terms:
        if term in tname:
            hits.add(term)
    for c in cols:
        cl = c.lower()
        matched = None
        for term in terms:
            if term in cl:
                matched = term
                break
        if matched:
            hits.add(matched)
            hit_cols.add(c)
            continue
        # 说明文档兜底（只在列名完全没命中时用，避免把说明里的泛词也算进来）
        d = (desc.get((table, c), ("", ""))[0] or "").lower()
        if d:
            for term in terms:
                if len(term) >= 4 and term in d:
                    hits.add(term)
                    hit_cols.add(c)
                    break
    return hits, hit_cols


def _col_index(db_id):
    """{列名小写: [表,...]} —— 共享列=隐式 join 依据。"""
    idx = {}
    for t, cols in schema_whitelist(db_id).items():
        for c in cols:
            idx.setdefault(c.lower(), []).append(t)
    return idx


def _sample_values(conn, table, cols, k=3):
    """给 join key 取几个真实值（去重、排序、LIMIT）。

    ★ 只给"连接关系"是不够的（本仓库实测）：
      debit_card_specializing 里 transactions_1k.Date = '2012-08-25 00:00:00'，
      而 yearmonth.Date = '201208'（月份编码）。两者能 join，但**写法完全不同**：
      模型若照日期写法去过滤 yearmonth，会返回 0 行，而 run_sql 对 0 行不报错，
      错误完全不可见。给出真实值它才能一次写对。
    ★ 优先级（实测必要）：yearmonth 的**主键是 (Date, CustomerID)，Date 在前**，
      而它偏偏是第二个共享列。若只取"第一个共享列"的样例，恰好漏掉最关键的
      Date 编码 —— 故这里对【所有】共享列取样，并优先展示"值里含非数字字符"
      （日期/状态/编码）的列，再补 ID 列。
    ORDER BY 保证输出确定，否则同题两次运行顺序可能不同，破坏可复现性。
    """
    out = []
    for c in cols:
        if len(out) >= 2:                      # 最多展示两列的样例，控制 token
            break
        try:
            cur = conn.execute(f'SELECT DISTINCT "{c}" FROM "{table}" '
                               f'WHERE "{c}" IS NOT NULL ORDER BY "{c}" LIMIT {k}')
            vals = [r[0] for r in cur.fetchall()]
        except sqlite3.Error:
            continue
        if not vals:
            continue
        out.append(f"{c} e.g. {', '.join(repr(v)[:22] for v in vals)}")
    return "; ".join(out)


def _fk_map(db_id):
    """{table: [(本表列, 目标表, 目标列)]} —— 声明式外键。用于把桥接表找更深一层。"""
    conn = get_conn(db_id)
    out = {}
    try:
        for t in sorted(schema_whitelist(db_id)):
            rows = conn.execute(f'PRAGMA foreign_key_list("{t}")').fetchall()
            if rows:
                out[t] = [(r[3], r[2], r[4]) for r in rows]
    finally:
        conn.close()
    return out


def _relevant_columns(table, cols, terms):
    """把列清单裁成"与问题有关"的部分。

    ★ 实测动机：student_club.budget 有 7 列，全列打印 + 全库总览会把输出顶到
      2000 字符上限，**桥接表那一段被截断** —— 而桥接表正是本工具存在的理由
      （实测第一条 student_club 输出就在 'via notes -> connects t' 处断掉）。
      故这里只留命中列，外加所有以 _id/link_to_ 结尾的键列（join 必需）。
    """
    keep, rest = [], []
    for c in sorted(cols, key=str.lower):
        cl = c.lower()
        if any(t in cl for t in terms) or cl.endswith("_id") or cl.startswith("link_to"):
            keep.append(c)
        else:
            rest.append(c)
    return keep, rest


def explore_schema(db_id, keywords=None, tables=None, max_tables=5):
    """工具主体。返回给模型看的文本。

    ★ 输出按【信息价值】排序且带字符预算：桥接表若被截断，本工具就失去意义，
      故底部"全库总览"是可选项，只有预算还够时才附上（实测这一条踩过坑）。
    """
    wl = schema_whitelist(db_id)
    desc = desc_index(db_id)
    rc = _rowcounts(db_id)
    col_idx = _col_index(db_id)
    fkmap = _fk_map(db_id)
    conn = get_conn(db_id)
    n = {t: (f"{rc.get(t):,}" if rc.get(t) is not None else "?") for t in wl}
    budget = config.TOOL_OUTPUT_MAX_CHARS

    terms = set()
    for k in (keywords or []):
        terms |= _terms(k)
    for t in (tables or []):
        terms |= _terms(t)

    named = []
    for t in (tables or []):
        for real in wl:
            if real.lower() == str(t).strip().lower():
                named.append(real)
                break

    def emit(line):
        """带预算的追加。

        ★ 不能用 return "\n".join(out)[:budget] 收尾（本仓库实测踩到）：
          硬切会把【桥接表那一段】整段无声砍掉 —— 而桥接表正是本工具存在的理由。
          实测 thrombosis_prediction 的 3 表输出就被切在 "matched: patient" 之后，
          模型看到的是一张毫无用处的表清单。故改为逐行判定，并在被截断时明说。
        """
        if sum(len(x) + 1 for x in out) + len(line) + 1 > budget:
            out.append("… (output truncated)")
            return False
        out.append(line)
        return True

    out = []

    # ---- 1) 词元检索表 ----
    scored = []
    for t, cols in wl.items():
        hits, hit_cols = _match_table(t, cols, terms, desc) if terms else (set(), set())
        if hits:
            scored.append((len(hits), len(hit_cols), t, hits, hit_cols))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))

    cand = [s[2] for s in scored[:max_tables]] or named[:max_tables]
    if not cand and terms:
        # 词元一个都没命中：退化用全库表名当候选，让桥接段仍有内容
        cand = sorted(wl, key=str.lower)[:max_tables]

    if scored or named:
        emit("MATCHED TABLES (by question terms; [rows] shows size):")
        shown = []
        for t in (cand + [x for x in named if x not in cand])[:max_tables]:
            hits = next((s[3] for s in scored if s[2] == t), set())
            keep, rest = _relevant_columns(t, wl[t], terms)
            tag = f"  matched: {', '.join(sorted(hits))}" if hits else "  (you named this table)"
            emit(f"  {t} [{n[t]}]")
            if keep:
                emit(f"    cols: {', '.join(keep)}" +
                           (f"  …(+{len(rest)} more)" if rest else ""))
            else:
                emit(f"    cols: {', '.join(sorted(wl[t], key=str.lower))}")
            emit(tag)
            shown.append(t)
        if len(scored) > max_tables:
            emit(f"  … {len(scored) - max_tables} more tables matched weakly.")
        emit("")

    # ---- 2) 桥接表：与候选共享 key 或经 FK 可达，但自身没被词元命中 ----
    #       这正是 68 条表选错里被漏掉的那一类（yearmonth / event / patient…）
    bridges = []
    if cand:
        cand_set = set(cand)
        cand_cols = {c.lower() for t in cand for c in wl[t]}
        for other in wl:
            if other in cand_set:
                continue
            shares = sorted({c for c in wl[other] if c.lower() in cand_cols}, key=str.lower)
            reach = sorted({t for c in shares for t in col_idx.get(c.lower(), [])
                            if t in cand_set})
            fk_edges = []
            for lc, rt, rcol in fkmap.get(other, []):
                if rt in cand_set:
                    fk_edges.append(f"{other}.{lc} -> {rt}.{rcol or '?'}")
            for t in cand:
                for lc, rt, rcol in fkmap.get(t, []):
                    if rt == other:
                        fk_edges.append(f"{t}.{lc} -> {other}.{rcol or '?'}")
            why = []
            if reach:
                why.append(f"shares {', '.join(shares)} with {', '.join(reach)}")
            if fk_edges:
                why.append("FK: " + "; ".join(fk_edges[:2]))
            if why:
                bridges.append((len(reach) + 2 * len(fk_edges), other, shares, why))

        bridges.sort(key=lambda x: (-x[0], x[1]))
        if bridges:
            emit("BRIDGE / related tables — NOT named in the question but connected to the")
            emit("tables above. MISSING one of these is the most common cause of a query")
            emit("that runs fine and is still wrong (e.g. filtering by month needs a")
            emit("separate month table that has no foreign key pointing to it):")
            for score, other, shares, why in bridges[:6]:
                line = f"  {other} [{n[other]}]  via " + "; ".join(why)
                if sum(len(x) + 1 for x in out) + len(line) > budget - 60:
                    break
                emit(line)
                sv = _sample_values(conn, other, shares or [c for c in wl[other]
                                                            if c.lower().endswith("_id")])
                if sv and sum(len(x) + 1 for x in out) + len(sv) + 6 <= budget:
                    emit(f"    {sv}")
            emit("")
        else:
            rest = [t for t in sorted(wl, key=str.lower) if t not in cand_set]
            if rest:
                emit("OTHER TABLES (no shared key or FK detected with the tables above;")
                emit("if the question needs one of them, join them carefully):")
                emit("  " + ", ".join(f"{t}[{n[t]}]" for t in rest))
                emit("")

    # ---- 3) 全库总览：仅在预算还够时附加（实测：它曾把桥接段挤掉）----
    tail = f"All tables in `{db_id}`: " + ", ".join(
        f"{t}[{n[t]}]" for t in sorted(wl, key=str.lower))
    used = sum(len(x) + 1 for x in out)
    if used + len(tail) <= budget:
        emit(tail)

    if not terms:
        emit("Tip: pass the business terms of the question in `keywords` to rank tables.")
    elif not scored:
        emit("No term matched any table/column name — try different wording in `keywords`.")

    conn.close()
    return "\n".join(out)

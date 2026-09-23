"""semantics.py —— B 层：连接基数不变量（可证伪的库内语义信号）

★ 为什么做这一层（而不是再做一个"自检"工具）：
  本仓库实测，顾问式的软报告会被模型忽略到底 —— declare_shape 报出 5 个
  【真实】错误，49/49 次 SQL 一个字没改。所以新信号必须满足两条：
    1) 判定来自【数据库本身】，不是模型自己声明的东西
       （check_result 之所以变成同义反复，就是因为它复述模型自己的 SQL）
    2) 结论是【可枚举的事实】，不是"建议"、不是"请注意"

  连接基数（join cardinality）正好满足：它由 schema 的唯一性约束 + 真实行数决定。

★ 它抓的是 A1v2 现有工具【完全没有覆盖】的一类错误：
  多对一 join 写成一对多（fan-out）→ 行数被静默放大 → 聚合值翻倍 → 结果错。
  现有 run_sql 只报"成功/返回 N 行"，无法说明 N 是不是对的。

判据（全部为事实陈述，不含建议以外的推断）：
  * 基表 = FROM 子句里的第一张表
  * 对每个 JOIN：解析 ON 的等值条件，取【右侧那张表】上的列
  * 若右侧表在这些列上【不唯一】（无唯一索引且实测 DISTINCT < COUNT）
    且结果行数 > 基表行数 → 行被放大，报告放大倍数
  * 若 LEFT JOIN 的右侧键有 NULL → 报告"这些行原本会被内连接静默丢掉"
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

DIALECT = "sqlite"


def _parse(sql):
    if not sql or not str(sql).strip():
        return None
    try:
        return sqlglot.parse_one(sql, read=DIALECT)
    except Exception:
        return None


def _alias_map(tree):
    """别名 -> 真表名（含无别名时的表名->表名）"""
    m = {}
    for t in tree.find_all(exp.Table):
        if not t.name:
            continue
        m[(t.alias or t.name).lower()] = t.name
    return m


def _from_arg(node):
    """取 FROM 子句。

    ★ sqlglot 30.18 的键名是 `from_`（不是 `from`）——第一版写 `args.get("from")`
      恒为 None，导致基表取不到、扇出检测整条失效，且【静默】不报错。
      两个键都试，避免版本差异再次静默失效。
    """
    return node.args.get("from_") or node.args.get("from")


def _where_tables(tree):
    """FROM 基表：最外层 SELECT 的第一张表（排除子查询/CTE 引入的）"""
    for sel in tree.find_all(exp.Select):
        frm = _from_arg(sel)
        if frm is not None and isinstance(frm.this, exp.Table) and frm.this.name:
            return frm.this.name
    frm = _from_arg(tree)
    if frm is not None and isinstance(frm.this, exp.Table):
        return frm.this.name
    return None


def _cte_and_subq_names(tree):
    """CTE 名 + 子查询别名 —— 它们不是真实表，必须排除。

    ★ metrics._cte_names 早就解决过这个问题，第一版没复用，于是 q6 的
      `WITH totals AS (...), mins AS (...)` 把 totals/mins 当成真表去 PRAGMA，
      报出"无法实测（OperationalError）"并据此误判成多对多。
    """
    names = set()
    for cte in tree.find_all(exp.CTE):
        if cte.alias:
            names.add(cte.alias.lower())
    for sub in tree.find_all(exp.Subquery):
        if sub.alias:
            names.add(sub.alias.lower())
    return names


def _join_pairs(tree):
    """按【单个等值条件】返回连接边。

    [{"right":..., "rc":列, "left_tbl":..., "lc":列, "kind":...}]  —— rc/lc 为 None 表示
    该侧不是真实表列（子查询/CTE），此时跳过该边。

    ★ 粒度必须到单个等值条件（第二版是按 join 聚合所有键，q245 里唯一的那侧
      被另一个键淹没，于是正确的 join 被误报成多对多）。
    """
    amap = _alias_map(tree)
    fake = _cte_and_subq_names(tree)
    out = []
    for j in tree.find_all(exp.Join):
        right = j.this
        if not isinstance(right, exp.Table) or not right.name:
            continue
        rname = right.name
        kind = ((j.kind or "") + (" " + j.side.upper() if j.side else "")).strip() or "INNER"
        right_alias = (right.alias or right.name).lower()
        on = j.args.get("on")
        if on is None:
            continue
        for eq in on.find_all(exp.EQ):
            l, r = eq.left, eq.right
            if not (isinstance(l, exp.Column) and isinstance(r, exp.Column)):
                continue
            if (r.table or "").lower() == right_alias and r.name:
                rc, lt, lc = r.name, amap.get((l.table or "").lower(), l.table), l.name
            elif (l.table or "").lower() == right_alias and l.name:
                rc, lt, lc = l.name, amap.get((r.table or "").lower(), r.table), r.name
            else:
                continue
            if rname.lower() in fake or (lt or "").lower() in fake or not lt or not lc:
                continue                       # 子查询/CTE 不是真表，无法查唯一性
            out.append({"right": rname, "rc": rc.lower(), "left_tbl": lt,
                        "lc": lc.lower(), "kind": kind})
    return out


def join_cardinality(conn, sql, row_limit=None):
    """返回不可忽略的连接基数报告文本；无可报告内容时返回 None。

    ★ 判据（第四版）—— 只保留"多对多扇出"：
        * 两侧都非唯一        -> 该边多对多，行数会被放大（真扇出）
        * 结果行数 > 基表行数 且无 GROUP BY -> 直接量到的扇出
      恰好一侧唯一 = 正常多对一/一对多，【不产生任何输出】。

    ★ 已【离线否决】的判据，不要加回来（都量过）：
      * "INNER JOIN 的多侧键含 NULL" —— 在 A1v2 的 317 道【正确】题里命中 42 道
        (13.2%)、在 183 道【错误】题里命中 13 道 (7.1%)，**假阳:真阳 = 130:1**。
        真实库连接键有 NULL 是常态，这是噪声不是信号。第一/二版 23.3% / 13.6%
        的误杀率主要是它贡献的。
      * "基表有 N 行"这类中性事实陈述 —— 不含可行动结论，只占字数。
    """
    tree = _parse(sql)
    if tree is None:
        return None
    pairs = _join_pairs(tree)
    if not pairs:
        return None

    base = _where_tables(tree)
    n_base = _count(conn, f'SELECT COUNT(*) FROM "{base}"') if base else None
    facts = []

    for p in pairs:
        u_r, ev_r = _is_unique(conn, p["right"], {p["rc"]})
        u_l, ev_l = _is_unique(conn, p["left_tbl"], {p["lc"]})
        edge = f"{p['left_tbl']}.{p['lc']} = {p['right']}.{p['rc']}"
        if u_l is False and u_r is False:
            facts.append(
                f"多对多连接 {edge}：{p['left_tbl']}.{p['lc']} {ev_l}；"
                f"{p['right']}.{p['rc']} {ev_r}"
                f" —— 两侧都不唯一，join 后行数会被放大，"
                f"COUNT/SUM/AVG 会随之算错")

    # 直接量到的扇出
    n_res = _count(conn, f"SELECT COUNT(*) FROM ({sql})")
    if (n_res is not None and n_base and n_res > n_base
            and not list(tree.find_all(exp.Group))):
        facts.append(
            f"结果 {n_res:,} 行 > 基表 {base} 的 {n_base:,} 行，放大 {n_res/n_base:.2f}×，"
            f"且这条 SQL 没有 GROUP BY —— 行数被 join 放大了")

    if not facts:
        return None
    return ("[连接基数检查]\n  - " + "\n  - ".join(facts))


def _is_unique(conn, table, cols):
    """该列组合在表上是否唯一。

    先查 schema 的唯一索引（硬证据）；没有就用数据实测 DISTINCT vs COUNT。
    返回 (unique: bool, evidence: str)
    """
    cols = sorted(cols)
    # 1) 唯一索引 / 主键
    try:
        idxs = conn.execute(f'PRAGMA index_list("{table}")').fetchall()
    except Exception:
        idxs = []
    for row in idxs:
        # row: (seq, name, unique, origin, partial)
        if len(row) >= 3 and row[2]:
            try:
                info = conn.execute(f'PRAGMA index_info("{row[1]}")').fetchall()
            except Exception:
                continue
            icols = sorted(r[2].lower() for r in info if len(r) > 2 and r[2])
            if icols == cols:
                return True, f"唯一索引 {row[1]}({','.join(cols)})"
    # 2) 数据实测
    sel = ", ".join(f'"{c}"' for c in cols)
    try:
        n, d = conn.execute(f'SELECT COUNT(*), COUNT(DISTINCT {sel}) FROM "{table}"').fetchone()
    except Exception as e:
        return False, f"无法实测（{type(e).__name__}）"
    if n is None:
        return False, "空表"
    if d == 0:
        return False, "全为 NULL"
    if d < n:
        return False, f"实测不唯一 {d:,} distinct / {n:,} rows"
    return True, f"实测唯一 {d:,}/{n:,}"


def _count(conn, sql):
    try:
        return conn.execute(sql).fetchone()[0]
    except Exception:
        return None



def declared_vs_actual(conn, sql, intent):
    """方向一：把模型的【承诺】和数据库的【真值】对质。

    只对【可在库内证伪的项】做判定；证不了的必须放过（否则就是乱杀）。
    返回列表，每项都是事实陈述。

    ★ 已【离线否决】的判据（不要加回来）：
      "承诺 per_entity 但首列有重复值" —— gold-vs-gold 自伤 42/500 (8.4%)。
        理由有二：(1) 结果首列不是一个实体的身份标识（"列出 976 行描述"里
        Description 当然只有 27 个不同值，模型是对的）；
        (2) 表里本来就有重复行，那是数据事实，不是错误。
        一个在 gold SQL 上都会触发的判据不能上线 —— 它只会给模型灌垃圾。
    """
    out = []
    tree = _parse(sql)
    if tree is None or not intent:
        return out

    gb = [str(g).lower() for g in (intent.get("group_by") or []) if str(g).strip()]
    actual_gb = set()
    for node in tree.find_all(exp.Group):
        for c in node.find_all(exp.Column):
            if c.name:
                actual_gb.add(c.name.lower())
    has_group = bool(list(tree.find_all(exp.Group)))

    # ① 承诺要分组，SQL 里根本没有 GROUP BY —— 结构矛盾，无法辩解
    if gb and not has_group:
        out.append(f"你承诺按 {gb} 分组，但这条 SQL 里没有 GROUP BY —— 结果不会按该维度分组")
    # ② 承诺分组键与 SQL 的 GROUP BY 对不上（只报差集，不判对错）
    elif gb and actual_gb:
        missing = [g for g in gb if g not in actual_gb]
        extra = sorted(actual_gb - set(gb))
        if missing:
            out.append(f"你承诺的分组键 {missing} 没有出现在 SQL 的 GROUP BY "
                       f"{sorted(actual_gb)} 里")
        if extra:
            out.append(f"SQL 的 GROUP BY 里有你没承诺的键 {extra}")
    # ③ 承诺"不分组、只要一个聚合值"，SQL 却分组了 —— 反向矛盾
    elif not gb and has_group:
        out.append(f"你承诺结果不分组（group_by 为空），但这条 SQL 有 GROUP BY "
                   f"{sorted(actual_gb)} —— 结果会是多行而不是单个聚合值")
    return out

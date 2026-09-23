"""_measure_intent.py —— 方向一（A10）承诺证伪器的离线校准

两个问题（都不花钱）：

  Q1 【自伤检查】用 gold SQL 反推 intent，再让 gold SQL 与它配对 ——
     必须 0 触发。若 gold-vs-gold 都报不一致，说明证伪器有系统性错误，
     上线只会给模型灌垃圾。

  Q2 【覆盖率】同样用 gold 反推的 intent，去对质 A1v2 的错误 SQL ——
     能触发多少条。这是上界（真实场景下模型自己的 intent 更偏，触发率只会更高）。

★ 与 B 层的区别：这里量的是"承诺 vs 结构"的对质，不是无差别甩不变量。
"""
import json
import re
import sys
from collections import Counter

sys.path.insert(0, ".")
from semantics import declared_vs_actual, _parse, _from_arg
from sqlglot import exp
from data import get_conn


def intent_from_sql(sql, question=""):
    """从一条 SQL 反推"它承诺了什么解释"。

    真实场景里 intent 来自模型的读题，这里只能从 SQL 反推 —— 因此对
    group_by / row_grain 的判定是【同义反复】（必然一致），
    真正有信息量的是 filters/dimensions 与 SQL 主体的对质。
    这一点必须在报告里写明，否则会把同义反复当成覆盖率。
    """
    tree = _parse(sql)
    if tree is None:
        return None
    gb = []
    for node in tree.find_all(exp.Group):
        for c in node.find_all(exp.Column):
            if c.name:
                gb.append(c.name)
    gb = sorted(set(gb))
    aggs = {type(a.this).__name__.upper() for a in tree.find_all(exp.AggFunc)
            if getattr(a, "this", None) is not None}
    grain = "aggregate" if (aggs and not gb) else ("per_entity" if gb else "row")
    return {"entities": question[:40] or "unknown",
            "filters": "", "dimensions": ",".join(gb), "measures": ",".join(sorted(aggs)),
            "group_by": gb, "row_grain": grain}


def main():
    from score import load_gold
    gold = load_gold()
    recs = [json.loads(l) for l in open("results/A1v2_full_scored.jsonl",
                                        encoding="utf-8") if l.strip()]

    # Q1 自伤检查：gold SQL 对质 gold 反推的 intent
    fired_gold = []
    for qidx, (gsql, db) in enumerate(gold):
        it = intent_from_sql(gsql)
        if not it:
            continue
        conn = get_conn(db)
        try:
            out = declared_vs_actual(conn, gsql, it)
        except Exception:
            out = []
        finally:
            conn.close()
        if out:
            fired_gold.append((qidx, out[0][:80]))
    print(f"Q1 自伤检查：gold-vs-gold 触发 {len(fired_gold)}/500 "
          f"({len(fired_gold)/500:.1%})   [目标 0]")
    for q, m in fired_gold[:5]:
        print(f"    q{q}: {m}")

    # Q2 覆盖率：gold 反推的 intent 对质 A1v2 的错误 SQL
    errs = [r for r in recs if not int(r.get("is_correct") or 0)]
    oks = [r for r in recs if int(r.get("is_correct") or 0)]
    for tag, grp in (("错误题", errs), ("正确题(误杀)", oks)):
        c = Counter()
        fired = []
        for r in grp:
            it = intent_from_sql(gold[r["qidx"]][0])
            if not it:
                c["no_intent"] += 1
                continue
            conn = get_conn(r["db_id"])
            try:
                out = declared_vs_actual(conn, r.get("final_sql") or "", it)
            except Exception:
                out = []
            finally:
                conn.close()
            if out:
                fired.append(r["qidx"])
                c[out[0][:22]] += 1
        print(f"\nQ2 {tag} n={len(grp)}：触发 {len(fired)} ({len(fired)/len(grp):.1%})")
        print(f"    触发 qidx: {fired[:25]}")
        print(f"    触发原因分布: {dict(c)}")


if __name__ == "__main__":
    main()

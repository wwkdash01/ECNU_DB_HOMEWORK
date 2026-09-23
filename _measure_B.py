"""_measure_B.py —— 离线量 A10 两个信号的【覆盖率】与【误杀率】

不花 API 钱。这是上线前的守门：
  * 覆盖率 = 在 A1v2 的 183 个【错误】里，信号能标记多少条
             （标记 ≠ 一定救得回来，是上界）
  * 误杀率 = 在 317 个【正确】题里，信号会错误标记多少条
             （硬门最怕这个：误杀会把本来对的题搞错）

判据都来自数据库本身（唯一性约束 / 真实行数），不是模型自述。
"""
import json
import sys
from collections import Counter

sys.path.insert(0, ".")
from semantics import join_cardinality, _parse, _join_pairs
from data import get_conn


def signal_B_marks(conn, sql):
    """信号 B 的原始判定（不看措辞）：是否存在"右侧键不唯一 + 行被放大"。"""
    tree = _parse(sql)
    if tree is None:
        return None, "parse_fail"
    joins = _join_pairs(tree)
    if not joins:
        return False, "no_join"
    rpt = join_cardinality(conn, sql)
    if rpt is None:
        return False, "no_report"
    # 只有真正出现扇出或 NULL 丢行的才算"标记"（"基表有 N 行"这类中性陈述不算）
    marked = ("多对多" in rpt) or ("放大" in rpt) or ("静默丢" in rpt)
    return marked, "marked" if marked else "neutral"


def main():
    recs = [json.loads(l) for l in open("results/A1v2_full_scored.jsonl",
                                        encoding="utf-8") if l.strip()]
    errs = [r for r in recs if not int(r.get("is_correct") or 0)]
    oks = [r for r in recs if int(r.get("is_correct") or 0)]
    print(f"A1v2: 错 {len(errs)}  对 {len(oks)}\n")

    for tag, group in (("错误题(覆盖率分子)", errs), ("正确题(误杀率分子)", oks)):
        c = Counter()
        marked = []
        for r in group:
            conn = get_conn(r["db_id"])
            try:
                m, why = signal_B_marks(conn, r.get("final_sql") or "")
            except Exception as e:
                import traceback
                m, why = None, f"err:{type(e).__name__}"
                if not globals().get("_TRACED"):
                    globals()["_TRACED"] = True
                    print(f"首个异常 qidx={r['qidx']} db={r['db_id']}")
                    traceback.print_exc()
            finally:
                conn.close()
            c[why] += 1
            if m:
                marked.append(r["qidx"])
        n_join = sum(v for k, v in c.items() if k in ("marked", "neutral"))
        print(f"--- {tag}  n={len(group)} ---")
        print(f"  可分析(有 JOIN 且能出报告): {n_join}  ({n_join/len(group):.1%})")
        print(f"  被标记: {len(marked)}  ({len(marked)/len(group):.1%})")
        print(f"  明细: {dict(c)}")
        print(f"  被标记的 qidx: {marked[:30]}")
        print()


if __name__ == "__main__":
    main()

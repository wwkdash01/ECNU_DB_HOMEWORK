"""make_pilot_targeted.py —— 【定向】抽样：按失败类型对准特定机制

★ 两种抽样，用途完全不同，别混用（本项目三次假信号都源于混用）：

    make_pilot_targeted.py     按【失败类型】抽 → **只能定位机制**
       只取 A1v2 判错、且表集与 gold 不一致的题 —— 这才是 explore_schema
       声称要解决的那一类。用来回答"工具在靶子类上有没有效"。

    make_samples.py           按【难度】分层 → **才能估效果**（生成 data/*_qidx.json）
       比例与全量一致（29.6/50.0/20.4%）。用来回答"方法整体提升多少"。

   ⚠️ 用定向样本估效果会得到假提升（本仓库实测）：
      A4 在定向样本上 +37pt、A6/A7 在"A1v2 错误集"的 30 题上 +6.67pt，
      换成代表性样本后**全部归零或反向**。
      原因：从错误集里抽题 = 在地板上取样，回归均值必然造出虚假提升。

★ 为什么必须定向抽样（更早的教训）：
  第一版冒烟直接取前 8 题，结果 8 题全部来自 debit_card_specializing，
  **而这 8 题 A1v2 的表选择本来就是全对的** —— 样本打不到靶子上，
  62.5% vs 25% 的"提升"完全来自别的原因（日期条件写法），不能归因给工具。

用法：
    python make_pilot_targeted.py                 # 只打印分组，不发 API
    python make_pilot_targeted.py --write         # 写出 data/pilot_targeted_qidx.json 供 run.py 用
"""
import json
import sys
import argparse
from collections import Counter

import config
from data import load_questions
from metrics import tables_columns

GOLD = [l.rstrip("\n").rsplit("\t", 1)
        for l in open(config.GOLD_FILE, encoding="utf-8") if l.strip()]


def classify():
    qs = load_questions()
    recs = [json.loads(l) for l in
            open(config.RESULTS_DIR / "A1v2_full_scored.jsonl", encoding="utf-8")]

    wrong_tables, other_wrong, correct = [], [], []
    for r in recs:
        g = {t.lower() for t in (tables_columns(GOLD[r["qidx"]][0])[0] or [])}
        p = {t.lower() for t in (tables_columns(r.get("final_sql") or "")[0] or [])}
        if r.get("is_correct"):
            correct.append(r["qidx"])
        elif p != g:
            wrong_tables.append((r["qidx"], r["db_id"], len(g), len(g - p), len(p - g)))
        else:
            other_wrong.append((r["qidx"], r["db_id"], len(g)))
    return qs, wrong_tables, other_wrong, correct


def main(write=False):
    qs, wt, ow, ok = classify()
    print(f"A1v2 全量 500 题：表选错 {len(wt)} / 其他错 {len(ow)} / 答对 {len(ok)}")
    print()
    print(f"表选错那批（A4 的靶子）按 gold 表数分布：")
    for k, v in sorted(Counter(x[2] for x in wt).items()):
        print(f"  gold {k} 表: {v:3} 题")
    print(f"  其中漏表为主（漏>多）: {sum(1 for x in wt if x[3] > x[4])} 题")
    print()
    print(f"靶子题所在库分布（前 8）：")
    for db, c in Counter(x[1] for x in wt).most_common(8):
        print(f"  {c:3}  {db}")

    # 定向样本：multi-table(>=2) 的漏表题 + 等量"表对但答案错"的对照题
    target = [x for x in wt if x[2] >= 2 and x[3] > 0][:30]
    control = [x for x in ow if x[2] >= 2][:20]
    sel = sorted({x[0] for x in target} | {x[0] for x in control})
    print()
    print(f"→ 定向样本: 靶子 {len(target)} + 对照 {len(control)} = 去重后 {len(sel)} 题")
    print(f"   库分布: {dict(Counter(qs[i]['db_id'] for i in sel))}")

    if write:
        # 落 data/ 而不是 results/（后者被 .gitignore 忽略）—— 清单定义了"跑了哪些题"，
        # 是口径的一部分，必须能随代码复现。
        p = config.DATA_DIR / "pilot_targeted_qidx.json"
        p.write_text(json.dumps({"qidx": sel, "target": len(target),
                                 "control": len(control),
                                 "note": "按失败类型定向抽样；【只能定位机制，不能估效果】"},
                                ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"   已写 {p}")
    return sel


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    main(ap.parse_args().write)

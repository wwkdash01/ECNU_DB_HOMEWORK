# gate1_check.py —— 阶段 1 验收（Gate 1）
# 运行：~/.conda/envs/db_agent/bin/python gate1_check.py
#
# 手册 §步骤 1.7 的四条通过标准 + 本项目实测补充的检查项。
# 每条独立判定 PASS/FAIL，最后统一汇总；不因单条失败而中断，方便一次看全。
import json, sqlite3, sys
from collections import Counter
from tqdm import tqdm
import config
from data import (load_questions, build_context, schema_whitelist,
                  schema_text, desc_index, db_path)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if detail:
        for line in str(detail).splitlines():
            print(f"       {line}")


def main():
    print("=" * 72)
    print("Gate 1 · 阶段 1 验收")
    print("=" * 72)

    # ---------------------------------------------------------------- 数据文件
    print("\n--- 前置：数据文件 ---")
    for f, label in ((config.QUESTIONS_FILE, "questions json"),
                     (config.GOLD_FILE, "gold sql"),
                     (config.DATA_DIR / "mini_dev_sqlite.jsonl", "jsonl（官方脚本用）"),
                     (config.DB_DIR, "dev_databases/")):
        check(f"存在 {label}", f.exists(), f.name)

    qs = load_questions()
    n = len(qs)
    dbs = sorted({q["db_id"] for q in qs})

    # ------------------------------------------------- 标准 1：题目数与 db_id 列表
    print("\n--- 标准 1：题目数与 db_id 列表 ---")
    check("题目数 > 0", n > 0, f"n = {n}")
    check("db_id 数 > 0", len(dbs) > 0, f"{len(dbs)} 个库")
    print(f"       手册口径：11 个库（V1/500 题）或 29 个（V2/780 题）")
    print(f"       实测：      {len(dbs)} 个库 / {n} 题 "
          f"→ 判定为 {'V1（500 题）' if n == 500 else 'V2（780 题）' if n == 780 else '未知版本'}")
    print(f"       {dbs}")

    # ------------------------------------------------- 标准 2：build_context 确定性
    print("\n--- 标准 2：build_context 确定性 ---")
    ok_all = True
    for q in tqdm(qs, desc="build_context 确定性", unit="题", ncols=88):
        a = build_context(q["db_id"], q.get("evidence"))
        b = build_context(q["db_id"], q.get("evidence"))
        if a != b:
            ok_all = False
            print(f"       不确定：qidx={q['qidx']} db={q['db_id']}")
            break
    check("全部题目 build_context 两次调用逐字相同", ok_all, f"检查了 {n} 题")

    q0 = qs[0]
    ctx = build_context(q0["db_id"], q0.get("evidence"))

    # ------------------------------------------------- 标准 3：schema 内容与 evidence
    print("\n--- 标准 3：schema 拼装与 evidence ---")
    check("context 含 'Database schema:' 段", "Database schema:" in ctx)
    check("context 含 'External knowledge:' 段", "External knowledge:" in ctx)
    # schema 里应能看到至少一个表名与类型关键字
    stext = schema_text(q0["db_id"])
    check("schema 文本非空", len(stext) > 0, f"{len(stext)} 字符")
    check("schema 含 CREATE TABLE", "CREATE TABLE" in stext.upper())
    has_types = any(t in stext.upper() for t in
                    ("INTEGER", "TEXT", "REAL", "BLOB", "NUMERIC", "VARCHAR", "DATE"))
    check("schema 含列类型", has_types)
    # evidence 必须【逐字】出现在 context 里；仅当为空时才回退成 'None'
    nonempty = [q for q in qs if str(q.get("evidence") or "").strip()]
    if nonempty:
        qe = nonempty[0]
        ce = build_context(qe["db_id"], qe.get("evidence"))
        check("非空 evidence 逐字拼入 context",
              qe["evidence"].strip() in ce,
              f"qidx={qe['qidx']} evidence 长度={len(qe['evidence'].strip())}")
    empty_qs = [q for q in qs if not str(q.get("evidence") or "").strip()]
    if empty_qs:
        qn = empty_qs[0]
        cn = build_context(qn["db_id"], qn.get("evidence"))
        tail = cn.split("External knowledge:")[-1].strip()
        check("空 evidence 回退为 'None'", tail == "None",
              f"qidx={qn['qidx']} 实际尾部={tail!r}")

    n_ev_empty = len(empty_qs)
    print(f"       evidence 为空：{n_ev_empty}/{n}（空则 build_context 填 'None'）")

    # ------------------------------------------------- 标准 4：schema_whitelist
    print("\n--- 标准 4：schema_whitelist ---")
    zero = [d for d in dbs if len(schema_whitelist(d)) == 0]
    check("所有库表数 > 0", not zero, f"表数为 0 的库：{zero if zero else '无'}")
    total_tables = sum(len(schema_whitelist(d)) for d in dbs)
    print(f"       各库表数：")
    for d in dbs:
        print(f"         {d:28} {len(schema_whitelist(d)):3} 表")
    print(f"       合计 {total_tables} 表")

    # ------------------------------------------- 补充（本项目实测发现的坑，需回归）
    print("\n--- 补充 A：11 个库的文件都存在且可读 ---")
    missing, unreadable = [], []
    for d in dbs:
        p = db_path(d)
        if not p.exists():
            missing.append(d)
            continue
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            c.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            c.close()
        except Exception as e:
            unreadable.append(f"{d}: {e}")
    check("库文件齐全", not missing, f"缺失：{missing if missing else '无'}")
    check("库文件可读", not unreadable, f"不可读：{unreadable if unreadable else '无'}")

    print("\n--- 补充 B：desc_index 编码回退（A3 组依赖）---")
    # 实测：4 个 CSV 是 cp1252 而非 UTF-8，不处理会让 A3 整题失败
    n_desc, bad = 0, []
    for d in dbs:
        try:
            idx = desc_index(d)
        except Exception as e:
            bad.append(f"{d}: {type(e).__name__}: {e}")
            continue
        n_desc += len(idx)
        for (t, col) in idx:
            if "\ufeff" in str(t) or "\ufeff" in str(col):
                bad.append(f"{d}: BOM 污染 {t}.{col}")
    check("desc_index 全部库可解析（含 cp1252 回退）", not bad,
          f"错误：{bad[:3] if bad else '无'}")
    check("desc_index 未被 BOM 污染", not any("BOM" in b for b in bad))
    print(f"       database_description 条目合计：{n_desc}")

    # ------------------------------------------------- 补充 C：gold 与 JSON 对齐
    print("\n--- 补充 C：JSON[i] 与 gold 第 i 行对齐（score.py 按下标取 gold 的前提）---")
    gold = [l.rstrip("\n") for l in open(config.GOLD_FILE, encoding="utf-8") if l.strip()]
    check("gold 行数 == 题目数", len(gold) == n, f"gold {len(gold)} 行 vs 题目 {n}")

    def norm(s):
        return " ".join((s or "").split()).strip().rstrip(";").lower()

    mism_sql, mism_db = [], []
    for i, (rec, line) in enumerate(zip(qs, gold)):
        gsql, gdb = line.rsplit("\t", 1)
        if norm(gsql) != norm(rec.get("SQL")):
            mism_sql.append(i)
        if gdb != rec["db_id"]:
            mism_db.append(i)
    check("SQL 逐条对齐", not mism_sql, f"不匹配下标：{mism_sql[:5]}（共 {len(mism_sql)}）")
    check("db_id 逐条对齐", not mism_db, f"不匹配下标：{mism_db[:5]}（共 {len(mism_db)}）")

    print("\n--- 补充 D：qidx 唯一（断点续跑用）---")
    qidx = [q["qidx"] for q in qs]
    check("qidx 为 0..n-1 连续且唯一", sorted(qidx) == list(range(n)), f"n={n}")
    dup = {k: v for k, v in Counter(q["question_id"] for q in qs).items() if v > 1}
    print(f"       ⚠️ question_id 重复值：{dup if dup else '无'}")
    print(f"       → 故断点续跑必须用 qidx，不能用 question_id")

    # ---------------------------------------------------------------- 汇总
    print("\n" + "=" * 72)
    failed = [(nm, dt) for nm, ok, dt in RESULTS if not ok]
    print(f"汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        print("\n未通过项：")
        for nm, dt in failed:
            print(f"  FAIL {nm}")
    print("=" * 72)
    print("Gate 1 " + ("通过 ✅" if not failed else "未通过 ❌"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

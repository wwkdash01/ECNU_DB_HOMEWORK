# gate2_check.py —— 阶段 2 验收（Gate 2）
# 运行：~/.conda/envs/db_agent/bin/python gate2_check.py
#
# 大部分检查【离线】完成，不需要 API 密钥、不花钱。
# 唯一发真实 API 调用的是「Gate 2 端到端」一节（1 次调用 + 1 次断点续跑）。
#
# 设计：所有检查跑完再汇总，不因单条失败而中断，方便一次看全。
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from tqdm import tqdm

import config
from data import (load_questions, build_context, get_conn, db_path,
                  schema_whitelist, desc_index)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if detail:
        for line in str(detail).splitlines():
            print(f"       {line}")


def section(title):
    print("\n" + "-" * 68)
    print(title)
    print("-" * 68)


# ======================================================================
# 1. db.py
# ======================================================================
def check_db():
    section("1. db.py —— 执行层")
    from db import execute, QueryTimeout

    qs = load_questions()
    db_id = qs[0]["db_id"]
    conn = get_conn(db_id)
    tbl = next(iter(schema_whitelist(db_id)))

    # --- 1a 正常查询：行数 + 正的 QET ---
    r = execute(conn, f'SELECT * FROM "{tbl}" LIMIT 5')
    check("1a 正常查询返回行数与正的 QET",
          len(r["rows"]) <= 5 and r["qet"] > 0,
          f"行数={len(r['rows'])}  QET={r['qet']:.6f}s  truncated={r['truncated']}")

    # --- 1b truncated 判定：多取一行才能分辨「正好 N 行」与「被截断」 ---
    # 注意 SQL 的 LIMIT 必须大于 row_limit，否则永远测不出截断
    r2 = execute(conn, f'SELECT * FROM "{tbl}" LIMIT 4', row_limit=3)   # 4 行 > 3 → 截断
    r3 = execute(conn, f'SELECT * FROM "{tbl}" LIMIT 2', row_limit=3)   # 2 行 < 3 → 未截断
    check("1b truncated 判定正确",
          r2["truncated"] is True and len(r2["rows"]) == 3
          and r3["truncated"] is False and len(r3["rows"]) <= 2,
          f"LIMIT 4/row_limit 3 → truncated={r2['truncated']} 返回 {len(r2['rows'])} 行\n"
          f"       LIMIT 2/row_limit 3 → truncated={r3['truncated']} 返回 {len(r3['rows'])} 行")

    # --- 1c 笛卡尔积触发 QueryTimeout ---
    big = None
    for t in sorted(schema_whitelist(db_id)):
        n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        if big is None or n > big[1]:
            big = (t, n)
    t0 = time.perf_counter()
    try:
        execute(conn, f'SELECT count(*) FROM "{big[0]}" a, "{big[0]}" b, '
                      f'"{big[0]}" c, "{big[0]}" d', timeout=1.0)
        check("1c 笛卡尔积抛 QueryTimeout", False, "居然跑完了（表太小？）")
    except QueryTimeout as e:
        dt = time.perf_counter() - t0
        check("1c 笛卡尔积抛 QueryTimeout", True,
              f"{big[0]}（{big[1]:,} 行）  实测 {dt:.2f}s（设定 1.0s）\n{e}")
    except Exception as e:
        check("1c 笛卡尔积抛 QueryTimeout", False,
              f"抛了别的异常：{type(e).__name__}: {e}")

    # --- 1d 连续调用不被前一次的过期 deadline 污染（手册未验，本仓库补充）---
    # set_progress_handler 是连接级的；若 finally 没清干净，第二次查询会一启动就被中断
    try:
        r_first = execute(conn, f'SELECT * FROM "{tbl}" LIMIT 1')
        r_second = execute(conn, f'SELECT * FROM "{tbl}" LIMIT 1')
        r_third = execute(conn, f'SELECT * FROM "{tbl}" LIMIT 1')
        ok = all(x["qet"] > 0 for x in (r_first, r_second, r_third))
        check("1d 连续 3 次查询均正常（进度回调未污染）", ok,
              f"QET = {r_first['qet']:.6f} / {r_second['qet']:.6f} / {r_third['qet']:.6f}")
    except Exception as e:
        check("1d 连续 3 次查询均正常（进度回调未污染）", False,
              f"{type(e).__name__}: {e}")

    # --- 1e 超时后连接仍可用（finally 清理生效的强证据）---
    try:
        execute(conn, f'SELECT count(*) FROM "{big[0]}" a, "{big[0]}" b, '
                      f'"{big[0]}" c, "{big[0]}" d', timeout=0.5)
    except Exception:
        pass
    try:
        r_after = execute(conn, f'SELECT * FROM "{tbl}" LIMIT 1')
        check("1e 超时之后同一连接仍可查询", r_after["qet"] > 0,
              f"QET={r_after['qet']:.6f}s")
    except Exception as e:
        check("1e 超时之后同一连接仍可查询", False, f"{type(e).__name__}: {e}")

    # --- 1f 只读连接：模型生成的写操作必须失败 ---
    # 构造【语法合法】的写语句，否则报的是语法错而不是 readonly，测不出真东西
    first_col = next(iter(schema_whitelist(db_id)[tbl]))
    wconn = sqlite3.connect(f"file:{db_path(db_id)}?mode=ro", uri=True)
    writes = [
        f'UPDATE "{tbl}" SET "{first_col}" = "{first_col}"',
        f'DELETE FROM "{tbl}"',
        f'DROP TABLE "{tbl}"',
        f'INSERT INTO "{tbl}" ("{first_col}") VALUES (NULL)',
    ]
    blocked, other = [], []
    for w in writes:
        try:
            wconn.execute(w)
            other.append(f"未被拦截: {w}")
        except sqlite3.OperationalError as e:
            (blocked if "readonly" in str(e).lower() else other).append(
                f"{w.split()[0]}: {e}")
        except Exception as e:
            other.append(f"{w.split()[0]}: {type(e).__name__}: {e}")
    wconn.close()
    check("1f 只读连接拦截写操作", len(blocked) == len(writes) and not other,
          f"{len(blocked)}/{len(writes)} 被 readonly 拒绝\n"
          + "\n".join("       " + b for b in blocked)
          + ("\n       异常项: " + str(other) if other else ""))

    conn.close()


# ======================================================================
# 2. llm.py
# ======================================================================
def check_llm():
    section("2. llm.py —— 抽 SQL（离线）")
    from llm import extract_sql

    cases = [
        ("```sql fenced", "说明\n```sql\nSELECT a FROM t;\n```\n结尾", "SELECT a FROM t"),
        ("``` plain fenced", "```\nSELECT b FROM u\n```", "SELECT b FROM u"),
        ("bare SELECT", "Here you go: SELECT c FROM v WHERE x=1", "SELECT c FROM v WHERE x=1"),
        ("WITH clause", "```sql\nWITH w AS (SELECT 1) SELECT * FROM w\n```", "WITH w AS"),
        ("DDL only -> None", "```sql\nCREATE TABLE t (a INT);\n```", None),
        ("CREATE+SELECT -> None(加固)", "```sql\nCREATE TABLE t (a INT);\n```", None),
        ("empty -> None", "", None),
        ("None -> None", None, None),
        ("prose only -> None", "抱歉，我无法回答这个问题。", None),
    ]
    for label, text, expect in cases:
        got = extract_sql(text)
        if expect is None:
            ok = got is None
        else:
            ok = got is not None and expect in got
        check(f"2 extract_sql: {label}", ok, f"得到 {got!r}")

    # 关键加固：模型只吐 DDL 时应返回 None，而不是把 DDL 当 SQL 交出去
    check("2 加固：纯 DDL 不当作可判分 SQL",
          extract_sql("CREATE TABLE students (id INT);") is None)
    # 前导注释不应导致失败：裸文本走兜底正则从第一个 SELECT 起截取（注释被丢掉），
    # 围栏路径则保留注释；两者都能被 SQLite 正常执行
    r = extract_sql("-- note\nSELECT a FROM t")
    check("2 前导注释场景仍能抽出 SQL",
          r is not None and "SELECT" in r,
          f"{r!r}（裸文本路径丢掉注释后再截取，属预期）")

    # 硬要求：抽出物必须是一条查询，且能被 SQLite 真正执行
    probe = sqlite3.connect(":memory:")
    probe.execute("CREATE TABLE t (a INTEGER)")
    probe.execute("INSERT INTO t VALUES (1)")
    for label, text in [("围栏", "```sql\n-- c\nSELECT a FROM t\n```"),
                        ("裸 SELECT", "Here: SELECT a FROM t"),
                        ("WITH", "```sql\nWITH w AS (SELECT a FROM t) SELECT * FROM w\n```")]:
        sql = extract_sql(text)
        try:
            probe.execute(sql).fetchall()
            ok = True
        except Exception as e:
            ok = False
        check(f"2 抽出物可在 SQLite 上执行: {label}", ok, f"{sql!r}")
    probe.close()


# ======================================================================
# 3. prompts.py —— 起点冻结
# ======================================================================
def check_prompts():
    section("3. prompts.py —— 起点冻结（原则①）")
    from prompts import oneshot_prompt, agent_system

    qs = load_questions()
    bad = []
    for q in tqdm(qs, desc="prompt 起点一致性", unit="题", ncols=88):
        ctx = build_context(q["db_id"], q.get("evidence"))
        if ctx not in oneshot_prompt(ctx, q["question"]) or ctx not in agent_system(ctx):
            bad.append(q["qidx"])
    check(f"3 全部 {len(qs)} 题：context 逐字出现在 O 和 A 两个 prompt 中",
          not bad, f"不一致：{bad[:5]}（共 {len(bad)}）")

    q0 = qs[0]
    ctx = build_context(q0["db_id"], q0.get("evidence"))
    o, a = oneshot_prompt(ctx, q0["question"]), agent_system(ctx)
    check("3 O 无工具说明", "tool" not in o.lower() and "run_sql" not in o)
    check("3 A 有工具说明", "run_sql" in a and "tool" in a.lower())
    check("3 O 含问题原文", q0["question"] in o)


# ======================================================================
# 4. tools.py
# ======================================================================
def check_tools():
    section("4. tools.py —— 工具层")
    from tools import dispatch, tools_for, TOOLS, _cut_values

    qs = load_questions()
    db_id = qs[0]["db_id"]
    conn = get_conn(db_id)
    tbl, col = next((t, sorted(c)[0]) for t, c in schema_whitelist(db_id).items() if c)

    # --- 4a run_sql 成功 ---
    txt, qet = dispatch(conn, db_id, "run_sql", {"sql": f'SELECT * FROM "{tbl}" LIMIT 3'})
    check("4a run_sql 成功返回文本 + QET",
          txt.startswith("执行成功") and qet is not None and qet > 0,
          f"{txt[:90]}…")

    # --- 4b 【关键设计】run_sql 不回完整结果，只给 3 行预览 ---
    check("4b run_sql 不回完整结果（只 3 行预览）",
          "预览" in txt and "预览" in txt)
    # 更强的证据：用大表确认预览行数不超过 3
    big_tbl = max(schema_whitelist(db_id), key=lambda t: conn.execute(
        f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
    txt_big, _ = dispatch(conn, db_id, "run_sql", {"sql": f'SELECT * FROM "{big_tbl}"'})
    check("4b run_sql 预览不含完整数据", "结果被截断" in txt_big,
          f"{big_tbl}: {txt_big[:100]}…")

    # --- 4c 错误返回友好字符串而非异常 ---
    for name, args in [("run_sql", {"sql": "SELECT * FROM nope"}),
                       ("get_column_values", {"table": tbl, "column": "fake_col"}),
                       ("get_column_values", {"table": "fake_tbl", "column": "c"}),
                       ("get_column_desc", {"table": "fake_tbl", "column": "c"})]:
        try:
            out, _ = dispatch(conn, db_id, name, args)
            if name == "run_sql":
                ok = out.startswith("执行失败")
            else:
                ok = out.startswith("错误")
            check(f"4c {name} 错误入参返回友好文本", ok, f"{out[:80]!r}")
        except Exception as e:
            check(f"4c {name} 错误入参返回友好文本", False,
                  f"抛了异常：{type(e).__name__}: {e}")

    # --- 4d get_column_values 返回真实取值 ---
    out, qet = dispatch(conn, db_id, "get_column_values", {"table": tbl, "column": col})
    check("4d get_column_values 返回真实取值",
          tbl in out and "取值样例" in out, f"{out[:110]}…")

    # --- 4e 顺序确定性（本仓库加固：手册无 ORDER BY）---
    outs = {dispatch(conn, db_id, "get_column_values",
                     {"table": tbl, "column": col})[0] for _ in range(5)}
    check("4e get_column_values 输出确定（5 次一致）", len(outs) == 1,
          f"不同结果数={len(outs)}")

    # --- 4f get_column_desc ---
    out, _ = dispatch(conn, db_id, "get_column_desc", {"table": tbl, "column": col})
    check("4f get_column_desc 返回文档或明确无文档",
          ("说明" in out) or out.startswith("没有"), f"{out[:100]!r}")

    # --- 4g 未知工具名 ---
    out, _ = dispatch(conn, db_id, "no_such_tool", {})
    check("4g 未知工具名返回友好文本", out.startswith("未知工具"), f"{out!r}")

    # --- 4h tools_for：A1 ⊂ A2 ⊂ A3（submit_answer 是各 A 组共有的终止动作，不计入差异）---
    def names(g):
        return {t["function"]["name"] for t in tools_for(g)} - {"submit_answer"}
    n1, n2, n3 = names("A1"), names("A2"), names("A3")
    check("4h 工具按组递增且严格包含 A1⊂A2⊂A3",
          n1 < n2 < n3 and len(n1) == 1 and len(n3) == 3,
          f"A1={sorted(n1)}\n       A2={sorted(n2)}\n       A3={sorted(n3)}")
    check("4h submit_answer 为各 A 组共有的终止动作",
          all("submit_answer" in {t["function"]["name"] for t in tools_for(g)}
              for g in ("A1", "A2", "A3")),
          f"A1={[t['function']['name'] for t in tools_for('A1')]}")
    check("4h tools_for 对 O 组返回 None", tools_for("O1") is None)

    # --- 4i 值列表按条目截断，不把值切成半截 ---
    long_vals = ["v" * 300 for _ in range(20)]
    s = _cut_values("t.c 的取值样例（20 个）", long_vals)
    check("4i 超长值列表按条目截断（值完整）",
          len(s) <= config.TOOL_OUTPUT_MAX_CHARS + 60 and "另有" in s,
          f"长度={len(s)}（上限 {config.TOOL_OUTPUT_MAX_CHARS}）")

    conn.close()

    section("4j TOOLS 声明合法性")
    for t in TOOLS:
        fn = t["function"]
        ok = (t.get("type") == "function" and "name" in fn and "description" in fn
              and fn["parameters"].get("type") == "object"
              and len(fn["description"]) > 30)
        check(f"4j TOOLS 声明完整: {fn['name']}", ok,
              f"description {len(fn['description'])} 字，参数 {list(fn['parameters']['properties'])}")


# ======================================================================
# 5. run.py —— 断点续跑 key
# ======================================================================
def check_run_resume():
    section("5. run.py —— 断点续跑 key（本仓库修正）")
    from run import load_done

    tmp = config.RESULTS_DIR / "_gate2_resume_test.jsonl"
    tmp.parent.mkdir(exist_ok=True)
    rows = [
        {"qidx": 0, "question_id": 1471, "group": "O1"},
        {"qidx": 1, "question_id": 1472, "group": "O1"},
        {"qidx": 3, "question_id": 1476, "group": "O1"},
    ]
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    done = load_done(tmp)
    check("5 断点续跑按 qidx 去重", done == {0, 1, 3}, f"done={sorted(done)}")

    # 用 question_id 会怎样：数据集里 137/138 各出现两次 -> 漏跑 2 题
    qs = load_questions()
    dup = {}
    for q in qs:
        dup.setdefault(q["question_id"], []).append(q["qidx"])
    dup = {k: v for k, v in dup.items() if len(v) > 1}
    done_by_qid = set()
    for q in qs:
        if q["question_id"] in (1471,):
            done_by_qid.add(q["question_id"])
    check("5 确认 question_id 确有重复（故不可用作 key）", bool(dup),
          f"重复值 {dup} → 各下标 {[v for v in dup.values()]}")

    # 用真实数据集模拟：若按 question_id 去重，最终会少几条
    fake_done = set()
    kept_by_qid, kept_by_qidx = [], []
    for q in qs:
        if q["question_id"] not in fake_done:
            fake_done.add(q["question_id"]); kept_by_qid.append(q["qidx"])
    seen = set()
    for q in qs:
        if q["qidx"] not in seen:
            seen.add(q["qidx"]); kept_by_qidx.append(q["qidx"])
    check("5 question_id 作 key 会漏跑（本仓库已改用 qidx）",
          len(kept_by_qid) == len(qs) - sum(len(v) - 1 for v in dup.values()),
          f"按 question_id 得 {len(kept_by_qid)} 条，按 qidx 得 {len(kept_by_qidx)} 条 "
          f"（总数 {len(qs)}）")

    tmp.unlink(missing_ok=True)


# ======================================================================
# 6. Gate 2 端到端（唯一需要 API 的部分）
# ======================================================================
def check_gate2_e2e(limit=1, skip=False):
    section("6. Gate 2 端到端 —— 跑 A1 的 N 条并校验 jsonl 字段")
    from run import main as run_main, load_done

    if skip:
        print("已跳过（--offline）。需要真实 API 调用：1 次 LLM + 若干工具执行。")
        return

    out_path = config.RESULTS_DIR / "A1.jsonl"
    backup = None
    if out_path.exists():                      # 不破坏已有结果
        backup = out_path.read_bytes()

    try:
        out_path.unlink(missing_ok=True)
        print(f"运行 run.main('A1', limit={limit}) …（会发真实 API 调用）")
        run_main("A1", limit=limit)
        recs = [json.loads(l) for l in open(out_path, encoding="utf-8")]
        check("6a 产出记录数正确", len(recs) == limit, f"{len(recs)} 条")

        r = recs[0]
        check("6b 顶层字段齐全",
              all(k in r for k in ("qidx", "question_id", "db_id", "difficulty",
                                   "group", "latency_total", "final_sql", "turns")),
              f"字段 {sorted(r.keys())}")
        check("6c latency_total 为正", r.get("latency_total", 0) > 0,
              f"{r.get('latency_total'):.2f}s")

        turns = r.get("turns", [])
        check("6d 至少 1 轮", len(turns) >= 1, f"{len(turns)} 轮")
        need = ("call_latency", "prompt_tokens", "completion_tokens",
                "cache_hit_tokens", "cache_miss_tokens")
        missing = [k for t in turns for k in need if k not in t]
        check("6e 每轮含【每次调用单独计时】+ 4 项 token 分项", not missing,
              f"缺失：{set(missing) if missing else '无'}")
        check("6f call_latency 为正", all(t["call_latency"] > 0 for t in turns),
              f"{[round(t['call_latency'],2) for t in turns]}")
        check("6g finish_reason 已记录（本仓库新增）",
              all(t.get("finish_reason") for t in turns),
              f"{[t.get('finish_reason') for t in turns]}")

        tools_used = [tc["name"] for t in turns for tc in t.get("tool_calls", [])]
        print(f"       本轮工具调用：{tools_used or '（无）'}")
        print(f"       final_sql: {str(r.get('final_sql'))[:100]}")

        # --- 断点续跑 ---
        done = load_done(out_path)
        qs = load_questions()[:limit]
        todo = [q for q in qs if q["qidx"] not in done]
        check("6h 断点续跑：重跑待跑数为 0", len(todo) == 0,
              f"done={sorted(done)} 待跑={len(todo)}")
    except Exception as e:
        check("6 Gate 2 端到端", False, f"{type(e).__name__}: {e}")
    finally:
        if backup is not None:
            out_path.write_bytes(backup)
            print(f"       已还原原有 {out_path.name}")


# ======================================================================
def main():
    offline = "--offline" in sys.argv
    print("=" * 68)
    print("Gate 2 · 阶段 2 验收")
    print("=" * 68)
    print(f"模式：{'离线（不发 API）' if offline else '完整（含 1 次真实 API 调用）'}")

    check_db()
    check_llm()
    check_prompts()
    check_tools()
    check_run_resume()
    check_gate2_e2e(limit=1, skip=offline)

    print("\n" + "=" * 68)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        print("\n未通过项：")
        for n, d in failed:
            print(f"  FAIL {n}")
    print("=" * 68)
    print("Gate 2 " + ("通过 ✅" if not failed else "未通过 ❌"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

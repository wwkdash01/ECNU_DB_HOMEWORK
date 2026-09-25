"""analyze.py —— 阶段 8「报告产出」的离线分析（零 API 成本）

    python -m db_agent analyze                              # 全部指标，含 EX@k
    python -m db_agent analyze --results-dir results/repro3 # 指定数据目录
    python -m db_agent analyze --no-exk                     # 跳过 EX@k（快，~10s）

存在理由（手册阶段 7.2 一直要求它，但从未实现）：
    `score.py` 只判分；`paired.py` 只做配对。报告需要的另外几类产出——
    **工具调用分布**（"模型会不会用工具"）、**延迟分解**（长尾归因）、
    **QET 对比**（§1.5 要求 Latency 与 QET 分开报）、**成本**、
    **`EXPLAIN QUERY PLAN` 分桶**（§3.1 DB 基础问题）、**case study 候选**——
    此前一份都没有，全是手工临时算的。本脚本把它们固化下来。

★ 为什么 EX@k 要重判（而不是从 `_scored.jsonl` 里读）：
  `score.py` 只把【最终答案】的 `is_correct` 落盘，逐轮候选的判定没有持久化，
  所以 `_scored.jsonl` 里没有 EX@k 所需的信息，必须重放候选 SQL 重新判分。
  这是本脚本唯一"重"的部分（~1 分钟/组），`--no-exk` 可跳过。
"""
import argparse
import json
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

from tqdm import tqdm

from db_agent import config
from db_agent.core.data import get_conn
from db_agent.core.db import execute, QueryTimeout

GROUPS = ("O1", "O3", "A1")

# EX@k 的候选来源必须与 score.py 完全一致，否则曲线对不上：
#   turn.sql（模型正文作答）+ 该轮 run_sql 参数 + 该轮 submit_answer 参数
CAND_TOOLS = ("run_sql", "submit_answer")


# ---------------------------------------------------------------- 读数据
def load_raw(results_dir, g):
    p = Path(results_dir) / f"{g}.jsonl"
    if not p.exists():
        return None
    return [json.loads(l) for l in open(p, encoding="utf-8")]


def load_scored(results_dir, g):
    p = Path(results_dir) / f"{g}_scored.jsonl"
    if not p.exists():
        return None
    return [json.loads(l) for l in open(p, encoding="utf-8")]


def drop_api_error(recs):
    return [r for r in recs if r.get("failure_type") != "api_error"]


# ---------------------------------------------------------------- 各产出
def five_metrics(raw, scored, results_dir):
    """五指标（§1.5）：EX / Latency(P50,P95) / QET / 列级召回 / 幻觉率"""
    n = len(scored)
    ok = sum(r.get("is_correct") or 0 for r in scored)
    lat = sorted(r["latency_total"] for r in raw if r.get("latency_total"))
    cr = [r["column_recall"] for r in scored if r.get("column_recall") is not None]
    hl = [r["hallucination"] for r in scored if r.get("hallucination") is not None]
    return {
        "n": n,
        "EX": ok / n,
        "P50": lat[len(lat) // 2],
        "P95": lat[int(len(lat) * 0.95)],
        "列级召回": sum(cr) / len(cr) if cr else None,
        "幻觉率": sum(hl) / len(hl) if hl else None,
        "failure_type": dict(Counter(r.get("failure_type") for r in scored)),
    }


def stratified(scored):
    d = defaultdict(lambda: [0, 0])
    for r in scored:
        k = r.get("difficulty", "moderate")
        d[k][0] += r.get("is_correct") or 0
        d[k][1] += 1
    return {k: (c / t) for k, (c, t) in d.items()}, {k: t for k, (c, t) in d.items()}


def tool_distribution(raw):
    """工具调用分布 —— 回答"模型会不会用工具"（阶段 8 产出表第 5 行）"""
    names = Counter()
    turns_per_q = []
    hits_cap = 0
    src = Counter()
    subs = Counter()
    for r in raw:
        ts = r.get("turns", [])
        turns_per_q.append(len(ts))
        if len(ts) >= config.MAX_STEPS:
            hits_cap += 1
        src[r.get("final_sql_source")] += 1
        subs[r.get("submit_attempts", 0)] += 1
        for t in ts:
            for tc in t.get("tool_calls", []):
                names[tc["name"]] += 1
    return {
        "工具调用次数": dict(names.most_common()),
        "轮数均值": st.mean(turns_per_q) if turns_per_q else 0,
        "打满 MAX_STEPS 的题": hits_cap,
        "final_sql_source": dict(src),
        "submit 尝试次数分布": dict(sorted(subs.items())),
    }


def latency_decomposition(raw):
    """延迟分解 —— 长尾归因的前提（§3.3 要求：不能把 API 抖动算到 agent 头上）

    总延迟 = Σ每轮 call_latency + Σ QET + 调度/本地开销
    """
    tot = {"call": 0.0, "qet": 0.0, "total": 0.0}
    calls = []
    per_q_call = []
    for r in raw:
        tot["total"] += r.get("latency_total") or 0
        s = 0.0
        for t in r.get("turns", []):
            cl = t.get("call_latency") or 0
            s += cl
            calls.append(cl)
            tot["qet"] += t.get("qet") or 0
        per_q_call.append(s)
        tot["call"] += s
    calls.sort()
    per_q_call.sort()
    return {
        "Σ总延迟": tot["total"],
        "Σcall_latency": tot["call"],
        "ΣQET": tot["qet"],
        "Σ调度开销": tot["total"] - tot["call"] - tot["qet"],
        "单次调用 P50": calls[len(calls) // 2] if calls else 0,
        "单次调用 P95": calls[int(len(calls) * 0.95)] if calls else 0,
        "整题 call 之和 P95": per_q_call[int(len(per_q_call) * 0.95)] if per_q_call else 0,
        "调用次数": len(calls),
    }


def cost(raw):
    inp = out = hit = 0
    calls = 0
    for r in raw:
        for t in r.get("turns", []):
            calls += 1
            inp += t.get("prompt_tokens", 0) or 0
            out += t.get("completion_tokens", 0) or 0
            hit += t.get("cache_hit_tokens", 0) or 0
    return {"调用": calls, "输入": inp, "缓存命中": hit, "缓存未命中输入": inp - hit, "输出": out}


def qet_probe(scored, db_dir, timeout=2.0, desc="QET 重测"):
    """QET —— 重新执行每组最终答案，量 SQL 在库上的真实耗时。

    ★ 为什么不用 `turns[].qet`：O 组没有 `run_sql`，该字段为 None，三组不可比。
      这里对【同一个最终答案】统一重测，才是 §1.5 要求的"Latency 与 QET 分开报"。
    """
    vals = []
    for r in tqdm(scored, desc=desc, unit="题", ncols=88):
        sql = r.get("final_sql")
        if not sql:
            continue
        try:
            conn = get_conn(r["db_id"])
            try:
                vals.append(execute(conn, sql, timeout=timeout)["qet"])
            finally:
                conn.close()
        except (QueryTimeout, Exception):
            continue
    if not vals:
        return None
    vals.sort()
    return {"n": len(vals), "P50": vals[len(vals) // 2], "P95": vals[int(len(vals) * 0.95)],
            "均值": st.mean(vals)}


def explain_buckets(scored, db_dir, desc="EXPLAIN 分桶"):
    """EXPLAIN QUERY PLAN 分桶 —— §3.1「DB 基础问题」的量化依据"""
    feats = Counter()
    n = 0
    for r in tqdm(scored, desc=desc, unit="题", ncols=88):
        sql = r.get("final_sql")
        if not sql:
            continue
        try:
            conn = get_conn(r["db_id"])
            try:
                rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
            finally:
                conn.close()
        except Exception:
            continue
        n += 1
        plan = " ".join(str(x[-1]).upper() for x in rows)
        if "SCAN" in plan:
            feats["含 SCAN（全表扫）"] += 1
        if "SEARCH" in plan:
            feats["含 SEARCH（走索引）"] += 1
        if "USE TEMP B-TREE" in plan:
            feats["含 USE TEMP B-TREE（需排序/临时表）"] += 1
        if "COVERING INDEX" in plan:
            feats["含 COVERING INDEX"] += 1
        if "SUBQUERY" in plan or "CORRELATED" in plan:
            feats["含 SUBQUERY/相关子查询"] += 1
    return n, feats


def exk_curve(results_dir, g, gold, qs, judge):
    """EX@k：累计 k 轮内答对即算对。

    ★ 语义按组不同，报告里必须区分（score.py 也会打印这句）：
      A 组 = "迭代到第 k 轮"；O3 = "前 k 个并行候选"；O1 = 单次调用。

    ★ 为什么这是全脚本最慢的一节：它走**官方判分链路**
      （`func_timeout(30s, execute_sql)`，每次新开 SQLite 连接、且把 gold 也跑一遍），
      而且要对【每一轮】的每条候选都判一次 —— A1 有 1783 轮，累计约 5000 次判分。

    ★ 判分缓存：key 必须含 gold。**不同题目的 gold 不同**，只按 (sql, db_id) 缓存
      会把 A 题的判定结果套到 B 题上，是错的。同一题内模型常把同一条 SQL 重跑多轮，
      缓存正好省掉这部分。
    """
    raw = load_raw(results_dir, g)
    first_ok = {}
    cache = {}
    n_judged = n_hit = 0
    pbar = tqdm(raw, desc=f"EX@k {g}", unit="题", ncols=88)
    for r in pbar:
        qidx = r["qidx"]
        gsql, db_id = gold[qidx]
        for t in r.get("turns", []):
            k = t["turn"] + 1
            cands = []
            if t.get("sql"):
                cands.append(t["sql"])
            for tc in t.get("tool_calls", []):
                if tc["name"] in CAND_TOOLS:
                    s = (tc.get("args") or {}).get("sql")
                    if s:
                        cands.append(s.strip().rstrip(";"))
            solved = False
            seen = set()
            for s in cands:
                if s in seen:
                    continue
                seen.add(s)
                key = (s, gsql, db_id)
                if key in cache:
                    n_hit += 1
                    ok = cache[key]
                else:
                    # ★ score.judge 返回的是元组 (0/1, failure_type)，不是 bool。
                    #   写成 `if judge(...)` 会【恒为真】（非空元组恒真），必须取 [0]。
                    #   另外下面那个 break 只能跳出【候选】循环，若不靠 solved 标志
                    #   跳出【轮次】循环，first_ok 会被后续轮次反复覆盖 —— 两个错误
                    #   叠加会让每条题都记在"最后一轮"，曲线于是"每组都收敛到 100%"，
                    #   看着光滑单调，实则完全失真。
                    ok = judge(s, gsql, db_id)[0] == 1
                    cache[key] = ok
                    n_judged += 1
                if ok:
                    first_ok[qidx] = k
                    solved = True
                    break
            if solved:
                break                      # ★ 本题已判对，不再看后续轮次
        pbar.set_postfix_str(f"实判 {n_judged}  缓存命中 {n_hit}  已判对 {len(first_ok)}")
    pbar.close()
    print(f"  [{g}] 实际判分 {n_judged} 次（缓存命中 {n_hit} 次，省 "
          f"{n_hit / max(1, n_judged + n_hit) * 100:.0f}%）")
    n = len(raw)
    max_k = max((r.get("turns") and len(r["turns"])) or 0 for r in raw) or 1
    curve = []
    prev = 0
    for k in range(1, max_k + 1):
        cum = sum(1 for v in first_ok.values() if v <= k)
        curve.append((k, cum, cum / n, cum - prev))
        prev = cum
    return n, curve


# ---------------------------------------------------------------- case study
def case_study_candidates(base_dir, a_name, b_name, qs, gold, limit=3):
    """挑出 A1 相对基线【救回】和【弄坏】的题，附上题面与两侧最终 SQL。

    只做候选筛选；真正的定性分析要人读结果，脚本不替人下结论。
    """
    A = {r["qidx"]: r for r in drop_api_error(load_scored(base_dir, a_name))}
    B = {r["qidx"]: r for r in drop_api_error(load_scored(base_dir, b_name))}
    idx = sorted(set(A) & set(B))

    def pack(qidx, tag):
        r = B[qidx]
        return {
            "tag": tag, "qidx": qidx, "difficulty": r.get("difficulty"),
            "db_id": r.get("db_id"), "question": qs[qidx]["question"],
            "gold_sql": gold[qidx][0],
            f"{a_name}_correct": bool(A[qidx].get("is_correct")),
            f"{b_name}_correct": bool(B[qidx].get("is_correct")),
            f"{b_name}_sql": B[qidx].get("final_sql"),
            f"{b_name}_turns": len(B[qidx].get("turns", [])),
        }

    saved = [pack(i, "救回") for i in idx if B[i].get("is_correct") and not A[i].get("is_correct")]
    broke = [pack(i, "弄坏") for i in idx if A[i].get("is_correct") and not B[i].get("is_correct")]
    return saved[:limit], broke[:limit], len(saved), len(broke)


# ---------------------------------------------------------------- 输出
def fmt_pct(x):
    return "—" if x is None else f"{x * 100:.2f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-exk", action="store_true", help="跳过 EX@k（省 ~1 分钟/组）")
    ap.add_argument("--cases", type=int, default=3)
    a = ap.parse_args()

    rd = Path(a.results_dir) if a.results_dir else (config.RESULTS_DIR / "repro3")
    if not rd.exists():
        rd = config.RESULTS_DIR
    L = []
    P = L.append

    P(f"# 阶段 8 产出 · 离线分析\n")
    P(f"> 数据目录：`{rd}`　|　由 `analyze.py` 生成，**零 API 成本**　|　"
      f"模型 `{config.MODEL}`\n")

    raw, scored = {}, {}
    for g in GROUPS:
        raw[g] = load_raw(rd, g)
        scored[g] = load_scored(rd, g)
    have = [g for g in GROUPS if raw[g] and scored[g]]
    if not have:
        raise SystemExit(f"{rd} 下没有任何 <group>.jsonl / _scored.jsonl")
    P(f"可用组：{', '.join(have)}\n")

    # ---- 1. 五指标 ----
    P("## 1. 五指标对比（§1.5）\n")
    P("| 指标 | " + " | ".join(have) + " |")
    P("|---" * (len(have) + 1) + "|")
    m = {g: five_metrics(raw[g], scored[g], rd) for g in have}
    rows = [("EX", "EX"), ("Latency P50 (s)", "P50"), ("Latency P95 (s)", "P95"),
            ("列级召回", "列级召回"), ("幻觉率", "幻觉率")]
    for label, key in rows:
        vals = []
        for g in have:
            v = m[g][key]
            vals.append(f"{v:.2f}" if isinstance(v, float) and key in ("P50", "P95") else fmt_pct(v))
        P(f"| {label} | " + " | ".join(vals) + " |")
    P(f"| n | " + " | ".join(str(m[g]['n']) for g in have) + " |")
    P("\n**失败类型**\n")
    for g in have:
        P(f"- `{g}`：{m[g]['failure_type']}")

    # ---- 2. 分层 EX ----
    P("\n## 2. 分层 EX\n")
    P("| 难度 | n | " + " | ".join(have) + " |")
    P("|---" * (len(have) + 2) + "|")
    for d in ("simple", "moderate", "challenging"):
        ns = {g: stratified(scored[g])[1].get(d, 0) for g in have}
        if not any(ns.values()):
            continue
        exs = {g: stratified(scored[g])[0].get(d) for g in have}
        P(f"| {d} | {max(ns.values())} | "
          + " | ".join(fmt_pct(exs[g]) for g in have) + " |")

    # ---- 3. QET ----
    P("\n## 3. QET（§1.5 要求与 Latency 分开报）\n")
    P("统一重测各组【最终答案】在库上的执行耗时（2 s 超时），三组同口径可比。\n")
    P("| 组 | 可测题数 | P50 (ms) | P95 (ms) | 均值 (ms) |")
    P("|---|---|---|---|---|")
    for g in have:
        q = qet_probe(scored[g], config.DB_DIR, desc=f"QET {g}")
        if q:
            P(f"| {g} | {q['n']} | {q['P50']*1000:.2f} | {q['P95']*1000:.2f} | {q['均值']*1000:.2f} |")

    # ---- 4. 延迟分解 ----
    P("\n## 4. 延迟分解（长尾归因的前提，§3.3）\n")
    P("| 组 | Σ总延迟 (s) | Σcall_latency | ΣQET | Σ调度开销 | 单次调用 P50 | 单次调用 P95 | 调用次数 |")
    P("|---|---|---|---|---|---|---|---|")
    for g in have:
        d = latency_decomposition(raw[g])
        P(f"| {g} | {d['Σ总延迟']:.0f} | {d['Σcall_latency']:.0f} | {d['ΣQET']:.0f} | "
          f"{d['Σ调度开销']:.0f} | {d['单次调用 P50']:.2f} | {d['单次调用 P95']:.2f} | {d['调用次数']} |")
    P("\n> **读法**：若 P95 的抬升主要来自「单次调用」而非「轮数」，"
      "则结论须写成「agent 放大了 API 固有长尾」，而不是「agent 本身慢」。")

    # ---- 5. 成本 ----
    P("\n## 5. 成本（token 事实，绝对金额按当期单价换算）\n")
    P("| 组 | 调用 | 输入 | 缓存命中 | 缓存未命中输入 | 输出 | 调用/题 |")
    P("|---|---|---|---|---|---|---|")
    for g in have:
        c = cost(raw[g])
        P(f"| {g} | {c['调用']} | {c['输入']:,} | {c['缓存命中']:,} | "
          f"{c['缓存未命中输入']:,} | {c['输出']:,} | {c['调用']/len(raw[g]):.2f} |")

    # ---- 6. 工具调用分布 ----
    P("\n## 6. 工具调用分布（\"模型会不会用工具\"）\n")
    for g in have:
        t = tool_distribution(raw[g])
        P(f"**`{g}`**：工具调用 {t['工具调用次数']}；轮数均值 {t['轮数均值']:.2f}；"
          f"打满 `MAX_STEPS`({config.MAX_STEPS}) 的题 **{t['打满 MAX_STEPS 的题']}**")
        P(f"- `final_sql_source`：{t['final_sql_source']}")
        P(f"- submit 尝试次数分布：{t['submit 尝试次数分布']}\n")

    # ---- 7. EXPLAIN 分桶 ----
    P("\n## 7. `EXPLAIN QUERY PLAN` 分桶（§3.1 DB 基础问题）\n")
    P("| 组 | 可解析 | " + " | ".join(["含 SCAN", "含 SEARCH", "含 TEMP B-TREE", "含 COVERING INDEX", "含子查询"]) + " |")
    P("|---" * 7 + "|")
    keys = ["含 SCAN（全表扫）", "含 SEARCH（走索引）", "含 USE TEMP B-TREE（需排序/临时表）",
            "含 COVERING INDEX", "含 SUBQUERY/相关子查询"]
    for g in have:
        n, f = explain_buckets(scored[g], config.DB_DIR, desc=f"EXPLAIN {g}")
        P(f"| {g} | {n} | " + " | ".join(f"{f[k]} ({f[k]/n*100:.1f}%)" if n else "—" for k in keys) + " |")
    P("\n> ⚠️ SQLite 只有 B-tree、无原生 Hash 索引，清单里的「B+树 vs Hash」在 SQLite 侧做不了，"
      "作为报告局限性写明即可。")

    # ---- 8. EX@k ----
    P("\n## 8. EX@k 曲线\n")
    if a.no_exk:
        P("（本次以 `--no-exk` 跳过。EX@k 需重放每轮候选并重新判分，约 1 分钟/组。）")
    else:
        P("⚠️ **语义按组不同**：A 组 = 「迭代到第 k 轮」；O3 = 「前 k 个并行候选」；O1 = 单次调用。"
          "**不可把两条曲线画在同一张图上直接比。**\n")
        # 延迟导入：judge 依赖官方评测脚本，放在这里避免 --no-exk 时也付出 import 成本
        sys_path_insert_eval()
        from db_agent.evaluation.score import judge, load_gold, load_questions  # noqa: E402
        gold = load_gold()
        qs = load_questions()
        P("| k | " + " | ".join(f"{g} 累计 EX@{g}" for g in have) + " |")
        P("|---" * (len(have) + 1) + "|")
        curves = {g: exk_curve(rd, g, gold, qs, judge) for g in have}
        maxk = max(len(c) for _, c in curves.values())
        for i in range(maxk):
            cells = []
            for g in have:
                cur = curves[g][1]
                cells.append(f"{cur[i][2]*100:.2f}% (+{cur[i][3]})" if i < len(cur) else "—")
            P(f"| {i+1} | " + " | ".join(cells) + " |")

    # ---- 9. case study 候选 ----
    P("\n## 9. case study 候选\n")
    if "A1" in have and "O1" in have:
        import_score_helpers = None
        rd_ok = Path(rd)
        qs = json.load(open(config.QUESTIONS_FILE, encoding="utf-8"))
        gold = []
        for line in open(config.GOLD_FILE, encoding="utf-8"):
            line = line.rstrip("\n")
            if line.strip():
                gold.append(tuple(line.rsplit("\t", 1)))
        saved, broke, ns, nb = case_study_candidates(rd_ok, "O1", "A1", qs, gold, a.cases)
        P(f"A1 相对 O1：**救回 {ns} 题、弄坏 {nb} 题**（完整 qidx 见 `paired.py` 输出）。\n")
        for title, items in (("A1 救回（O1 错 → A1 对）", saved), ("A1 弄坏（O1 对 → A1 错）", broke)):
            P(f"### {title}\n")
            for it in items:
                P(f"- **q{it['qidx']}**（{it['difficulty']} / `{it['db_id']}`，A1 用了 {it['A1_turns']} 轮）")
                P(f"  - 问：{it['question']}")
                P(f"  - **A1 SQL**：`{str(it['A1_sql'])[:220]}`")
                P(f"  - gold：`{it['gold_sql'][:220]}`")
            P("")
        P("> 定性分析需人工读该题在 `<group>.jsonl` 里的完整 `turns` 与 `messages_final`；"
          "本脚本只做候选筛选，不替人下结论。")
    else:
        P("（需要同时具备 O1 与 A1 的 `_scored.jsonl`）")

    text = "\n".join(L) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"→ 已写出 {a.out}（{len(text)} 字节）")
    else:
        print(text)


def sys_path_insert_eval():
    import sys
    sys.path.insert(0, str(config.EVAL_DIR))


if __name__ == "__main__":
    main()

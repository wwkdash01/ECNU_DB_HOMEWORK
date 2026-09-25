"""score.py —— 接官方判分脚本，产出 <group>_scored.jsonl

    python -m db_agent score --group O1

判分链路唯一（手册原则③）：O 和 A 走同一个官方 execute_sql + calculate_ex，
绝不自写结果比对 —— 官方 EX 是 set(pred)==set(gold)，忽略列序与重复行。

已实测的事实（阶段 1 核实）：
  * evaluation_utils.execute_sql(pred, gold, db_path, dialect, calc_func)
  * calculate_ex = set(pred_res) == set(gold_res)
  * db_path 是【普通文件路径】，不吃 data.get_conn() 的 file:...?mode=ro URI
  * gold 文件每行 <SQL>\\t<db_id>
  * JSON[i] 与 gold 第 i 行逐条对齐 500/500（本脚本开头会再断言一次）

除官方 EX 外，本脚本同时算出报告需要的列级召回与幻觉率（metrics.py），
以及逐轮 EX@k。注意：EX@k 对 A 组是"迭代到第 k 轮"，
对 O3 是"前 k 个并行候选"——两者语义不同，见 ex_at_k_semantics 字段。
"""
import sys
import json
import argparse
from collections import defaultdict

from func_timeout import func_timeout, FunctionTimedOut
from tqdm import tqdm

from db_agent import config
from db_agent.core.data import load_questions
from db_agent.core.metrics import column_recall, hallucination

sys.path.insert(0, str(config.EVAL_DIR))
from evaluation_utils import execute_sql          # noqa: E402
from evaluation_ex import calculate_ex            # noqa: E402

JUDGE_TIMEOUT = 30.0


def load_gold():
    out = []
    for line in open(config.GOLD_FILE, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        sql, db_id = line.rsplit("\t", 1)         # 官方格式：制表符分隔
        out.append((sql, db_id))
    return out


def judge(pred_sql, gold_sql, db_id, timeout=JUDGE_TIMEOUT):
    """返回 (1/0, failure_type)。复用官方执行与比对，口径完全一致。"""
    if not pred_sql or not str(pred_sql).strip():
        return 0, "parse_error"
    db_path = str(config.DB_DIR / db_id / f"{db_id}.sqlite")
    try:
        r = func_timeout(timeout, execute_sql,
                         args=(pred_sql, gold_sql, db_path, "SQLite", calculate_ex))
        return r, ("ok" if r == 1 else "result_mismatch")
    except FunctionTimedOut:
        return 0, "timeout"
    except Exception:
        return 0, "exec_error"


def o3_mechanism_health(recs):
    """O3 机制体检 —— 验证「采样必须发散」这一前提是否成立。

    ★ 为什么必须在解读 EX 之前看（本项目唯一一个"机制死了但数字照常好看"的
      失败模式）：思考模式一旦未关闭，temperature 会被平台【静默忽略】，
      同一 prompt 的三次采样得到完全相同的 SQL —— O3 退化成 O1，
      而 EX 上看不出任何异常。所以这一步不是锦上添花，是前置排除。

    ★ 判据刻意分两层，不混：
      静态层（确定性）：直接查根因，可判 PASS/FAIL。
      数据层（经验观察）：只如实报告，不下结论 —— O3 从未跑过全量，
        任何"全同率应 < X%"的阈值都是编出来的假精度。
        把数打出来，异常一眼可见。

    只看 O3；其它组不调用本函数。
    """
    k = config.K_CANDIDATES
    n = len(recs)
    same = diff = short = no_cand = all_failed = 0
    clusters, errs = defaultdict(int), defaultdict(int)

    for r in recs:
        cands = [str(c).strip() for c in (r.get("candidates") or []) if str(c or "").strip()]
        if not cands:
            no_cand += 1
        else:
            if len(cands) < k:
                short += 1
            if len(set(cands)) == 1:        # 三次采样吐出同一条 SQL
                same += 1
            else:
                diff += 1
        groups = r.get("groups") or []
        clusters[len(groups)] += 1          # 执行结果集聚成几簇（1 簇 = 该题投票无分歧）
        if not groups:
            all_failed += 1                 # 候选全执行失败 -> 退回首个非空 SQL（采集兜底）
        for e in (r.get("exec_errors") or []):
            errs["ok" if e is None else str(e).split(":")[0]] += 1

    # ---------------- 静态层：确定性的根因检查 ----------------
    static = [
        ("思考模式已关闭（否则 temperature 静默失效）",
         config.THINKING.get("type") == "disabled", f"THINKING={config.THINKING}"),
        ("采样温度 > 0（否则三次采样必然同解）",
         config.TEMPERATURE_SAMPLING > 0, f"T={config.TEMPERATURE_SAMPLING}"),
        ("候选数 >= 2（否则投票无意义）",
         k >= 2, f"K_CANDIDATES={k}"),
    ]
    print("\n  --- O3 机制体检（解读 EX 前必看）---")
    print("    [静态层 · 判 PASS/FAIL]")
    for name, ok, detail in static:
        print(f"      [{'PASS' if ok else 'FAIL'}] {name}   {detail}")
    if not all(ok for _, ok, _ in static):
        print("      ⚠ 静态层未通过：O3 的机制前提不成立，")
        print("        本次 EX 不能当作「采样投票」的效果来解读。")

    # ---------------- 数据层：只报告，不下结论 ----------------
    print("    [数据层 · 只报告，无归档基准]")
    print(f"      记录 {n}   候选数不足 {k} 的题 {short}   无候选 {no_cand}")
    print(f"      三次候选全同 : {same:4d} ({same / n:6.1%})"
          f"   ← 越接近 100% 越可疑，结合静态层判断")
    print(f"      候选有差异   : {diff:4d} ({diff / n:6.1%})")
    print(f"      结果集簇数分布: {dict(sorted(clusters.items()))}"
          f"   （1 簇 = 该题投票无分歧）")
    print(f"      候选全失败退回: {all_failed:4d}   ← 采集兜底，报告需披露")
    print(f"      候选执行结果 : {dict(errs)}")


def main(group, out=None):
    gold = load_gold()
    qs = load_questions()

    # 下标对齐是 score.py 的命门：错位会让 EX 整体偏低且看不出原因
    assert len(gold) == len(qs), f"gold {len(gold)} 行 vs 题目 {len(qs)} 条"
    in_path = config.RESULTS_DIR / (out or f"{group}.jsonl")
    recs = [json.loads(l) for l in open(in_path, encoding="utf-8")]
    recs = [r for r in recs if r.get("failure_type") != "api_error" and "final_sql" in r]
    print(f"[{group}] 读入 {len(recs)} 条（来自 {in_path.name}，"
          f"已剔除 api_error 与无 final_sql 的记录）")

    # O3 的机制前提必须在读数字之前排掉（见 o3_mechanism_health 的 docstring）
    if group == "O3":
        o3_mechanism_health(recs)

    n_api_error = sum(1 for l in open(in_path, encoding="utf-8")
                      if json.loads(l).get("failure_type") == "api_error")

    ex_at_k = defaultdict(lambda: defaultdict(int))     # （保留：按难度分层的命中）
    first_ok = {}                    # qidx -> 第一次答对的轮次 k
    max_k = 0                        # 观察到的最大轮次
    recall_missing = defaultdict(int)
    halluc_missing = 0

    n_exec = 0                      # 累计执行的 SQL 条数
    n_ok = 0                        # 累计答对题数
    pbar = tqdm(recs, desc=f"{group} 判分", unit="题", ncols=92)
    for r in pbar:
        qidx = r.get("qidx")
        if qidx is None:                                # 兼容早期记录
            qidx = next((q["qidx"] for q in qs
                         if q["question_id"] == r["question_id"]), None)
        gsql, db_id = gold[qidx]

        ok, ftype = judge(r.get("final_sql"), gsql, db_id)
        r["is_correct"], r["failure_type"] = ok, ftype
        n_exec += 1
        n_ok += ok

        # 列级召回
        rec, reason = column_recall(r.get("final_sql"), gsql, db_id)
        r["column_recall"], r["column_recall_reason"] = rec, reason
        if rec is None:
            recall_missing[reason] += 1

        # 幻觉率
        hal, detail = hallucination(r.get("final_sql"), db_id)
        r["hallucination"], r["hallucination_detail"] = hal, detail
        if hal is None:
            halluc_missing += 1

        # 逐轮 EX@k（离线，零额外 API 成本）
        #
        # ⚠️ 候选不能只取 turn["sql"]：实测该模型把 SQL 放在【工具参数】里，
        #    turn.sql 几乎恒为 0（第1轮 0/20）。故每轮的候选取三者之和：
        #      turn.sql（若模型用正文作答）
        #    + 该轮 run_sql 的 sql 参数（模型探索/迭代时写的查询）
        #    + 该轮 submit_answer 的 sql（最终提交）
        # 这些 SQL 都真实执行过，离线重判无额外成本。
        for t in r.get("turns", []):
            k = t["turn"] + 1
            max_k = max(max_k, k)
            cands = []
            if t.get("sql"):
                cands.append(t["sql"])
            for tc in t.get("tool_calls", []):
                if tc["name"] in ("run_sql", "submit_answer"):
                    s = (tc.get("args") or {}).get("sql")
                    if s:
                        cands.append(s.strip().rstrip(";"))
            seen = set()
            for s in cands:
                if s in seen:
                    continue
                seen.add(s)
                o, _ = judge(s, gsql, db_id)
                n_exec += 1
                if o:
                    first_ok.setdefault(r["qidx"], k)
                    break
        # 进度条实时反馈
        pbar.set_postfix_str(f"正确 {n_ok}  已执行 SQL {n_exec}")

    out_path = config.RESULTS_DIR / (
        (out.rsplit(".jsonl", 1)[0] + "_scored.jsonl") if out else f"{group}_scored.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # ---------------- 汇总 ----------------
    n = len(recs)
    print(f"\n===== {group}  n={n} =====")
    if n == 0:
        print("  无有效样本")
        return

    acc = sum(r["is_correct"] for r in recs) / n * 100
    print(f"  EX = {acc:.2f}%")

    by_diff = defaultdict(lambda: [0, 0])
    for r in recs:
        d = by_diff[r.get("difficulty", "moderate")]
        d[0] += r["is_correct"]; d[1] += 1
    for d, (c, t) in sorted(by_diff.items()):
        print(f"    {d:12} {c/t*100:6.2f}%  (n={t})")

    fts = defaultdict(int)
    for r in recs:
        fts[r.get("failure_type", "?")] += 1
    print(f"  失败类型: {dict(fts)}")
    if n_api_error:
        print(f"  从 EX 分母剔除的 api_error: {n_api_error} 条（报告需披露）")

    # 列级召回（只统计能算的）
    recs_ok = [r for r in recs if r.get("column_recall") is not None]
    if recs_ok:
        avg = sum(r["column_recall"] for r in recs_ok) / len(recs_ok)
        print(f"  列级召回 = {avg*100:.2f}%  (可计算 {len(recs_ok)}/{n})")
    if recall_missing:
        print(f"    单列未计入的原因: {dict(recall_missing)}")

    # 幻觉率：只统计能算的
    hal_ok = [r for r in recs if r.get("hallucination") is not None]
    if hal_ok:
        avg = sum(r["hallucination"] for r in hal_ok) / len(hal_ok)
        nz = sum(1 for r in hal_ok if r["hallucination"] > 0)
        print(f"  幻觉率   = {avg*100:.2f}%  (可计算 {len(hal_ok)}/{n}，{nz} 条含幻觉)")

    print(f"\n  EX@k 曲线（累计：k 轮内答对即算对）:")
    semantics = ("迭代到第 k 轮" if group.startswith("A")
                 else "前 k 个并行候选" if group == "O3" else "单次调用")
    print(f"    语义: {semantics}  ← 报告里必须区分，O3 的 k 不是迭代轮次")
    print(f"    候选来源: 每轮 [正文 SQL] + [run_sql 参数 SQL] + [submit_answer SQL]")
    if max_k:
        prev_cum = 0
        for k in range(1, max_k + 1):
            cum = sum(1 for v in first_ok.values() if v <= k)
            gain = cum - prev_cum
            print(f"    EX@{k:<2} {cum/n*100:6.2f}%  (+{gain} 题, {gain/n*100:+5.2f}pt)"
                  f"   答对题数 {cum}/{n}")
            prev_cum = cum
        kstar = next((k for k in range(1, max_k + 1)
                      if sum(1 for v in first_ok.values() if v <= k) == len(first_ok)), None)
        if kstar:
            print(f"    → 全部答对所需轮数 k* = {kstar}"
                  f"  （边际收益在 k* 之后为 0，可用于 cost trade-off 分析）")

    print(f"\n→ {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True)
    ap.add_argument("--out", default=None,
                    help="输入文件名（默认 <group>.jsonl）；输出为同名 _scored.jsonl")
    a = ap.parse_args()
    main(a.group, a.out)

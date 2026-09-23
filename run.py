"""run.py —— 批量执行三组实验，结果落盘 results/<group>.jsonl

    python run.py --group A1 --limit 3        # 冒烟
    python run.py --group O1                  # 全量

相对手册（§2.6）的一处修正（本仓库实测）：
    断点续跑的 key 用 qidx 而不是 question_id —— 数据集中 question_id 有重复
    （137/138 各出现两次），用它当 key 会漏跑 2 题，最终只有 498 条。
"""
import json
import os
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

import config
from data import load_questions, build_context
from methods import run_oneshot, run_selfconsistency, run_agent
from tools import tools_for

GROUPS = {
    # 组名: (方法,               温度,                  工具)
    "O1": (run_oneshot,          config.TEMPERATURE,          None),
    # O3：T=0.7 是机制的内在要求（T=0 时三次采样会得到相同 SQL，退化成 O1）
    "O3": (run_selfconsistency,  config.TEMPERATURE_SAMPLING, None),
    # A1 = agent 循环 + 输出形态自检，工具 = run_sql + submit_answer。
    # ★ A1 ≡ 历史文档里的 A1v2（旧 A1 无形态自检，已随其余对照组移除）。
    #   prompt 见 prompts.AGENT_SYSTEM，【基线冻结】：措辞一改，63.40% 的可比性作废。
    "A1": (run_agent,            config.TEMPERATURE,          tools_for("A1")),
}


def load_done(path):
    """已完成集合。用 qidx 而非 question_id —— 后者有重复值。"""
    done = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                key = rec.get("qidx")
                if key is None:                 # 兼容早期记录
                    key = rec.get("question_id")
                if key is not None:
                    done.add(key)
    return done


def run_one(q, group):
    method, temp, tools = GROUPS[group]
    ctx = build_context(q["db_id"], q.get("evidence"))
    t0 = time.perf_counter()
    r = method(ctx, q["question"], q["db_id"], temp) if tools is None \
        else method(ctx, q["question"], q["db_id"], temp, tools)
    return {
        "qidx": q["qidx"], "question_id": q["question_id"], "db_id": q["db_id"],
        "difficulty": q.get("difficulty", "moderate"), "group": group,
        "prompt_context": ctx, "latency_total": time.perf_counter() - t0,
        "model_version": config.MODEL, **r,
    }


def main(group, limit=None, concurrency=None, out=None):
    config.RESULTS_DIR.mkdir(exist_ok=True)
    qs = load_questions()
    if limit:
        qs = qs[:limit]                                  # 冒烟：固定取前 N 条
    # --out 允许把实验写到独立文件（如重跑变体）。
    # 否则 --limit 20 会去读 <group>.jsonl 的断点记录，若该文件已是全量 500 条，
    # 前 20 题会被判为"已完成"从而一题不跑（本仓库实测踩到过）。
    out_path = config.RESULTS_DIR / (out or f"{group}.jsonl")
    done = load_done(out_path)
    todo = [q for q in qs if q["qidx"] not in done]
    print(f"[{group}] 输出 {out_path.name}  待跑 {len(todo)}（已完成 {len(done)}）")

    with open(out_path, "a", encoding="utf-8") as f, \
         ThreadPoolExecutor(concurrency or config.CONCURRENCY) as ex:
        futs = {ex.submit(run_one, q, group): q for q in todo}
        for fut in tqdm(as_completed(futs), total=len(todo), desc=group):
            q = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:                        # 单题失败不拖死整批
                rec = {"qidx": q["qidx"], "question_id": q["question_id"],
                       "db_id": q["db_id"], "group": group,
                       "failure_type": "api_error", "error": str(e)}
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            f.flush()                                     # 立即落盘，Ctrl-C 不丢数据


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True, choices=list(GROUPS))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--out", default=None,
                    help="输出文件名（默认 <group>.jsonl），避免覆盖全量结果")
    a = ap.parse_args()
    main(a.group, a.limit, a.concurrency, a.out)

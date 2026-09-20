"""run.py —— 批量执行五组实验，结果落盘 results/<group>.jsonl

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
    "O3": (run_selfconsistency,  config.TEMPERATURE_SAMPLING, None),
    "A1": (run_agent,            config.TEMPERATURE,          tools_for("A1")),
    "A2": (run_agent,            config.TEMPERATURE,          tools_for("A2")),
    "A3": (run_agent,            config.TEMPERATURE,          tools_for("A3")),
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


def main(group, limit=None, concurrency=None):
    config.RESULTS_DIR.mkdir(exist_ok=True)
    qs = load_questions()
    if limit:
        qs = qs[:limit]                                  # 冒烟：固定取前 N 条
    out_path = config.RESULTS_DIR / f"{group}.jsonl"
    done = load_done(out_path)
    todo = [q for q in qs if q["qidx"] not in done]
    print(f"[{group}] 待跑 {len(todo)}（已完成 {len(done)}）")

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
    a = ap.parse_args()
    main(a.group, a.limit, a.concurrency)

import json
import os
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

from db_agent import config
from db_agent.core.data import load_questions, build_context
from db_agent.experiment.methods import run_oneshot, run_selfconsistency, run_agent
from db_agent.experiment.tools import tools_for

GROUPS = {
    "O1": (run_oneshot,          config.TEMPERATURE,          None),
    "O3": (run_selfconsistency,  config.TEMPERATURE_SAMPLING, None),
    "A1": (run_agent,            config.TEMPERATURE,          tools_for("A1")),
}


def load_done(path):
    done = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                key = rec.get("qidx")
                if key is None:
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
        qs = qs[:limit]
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
            except Exception as e:
                rec = {"qidx": q["qidx"], "question_id": q["question_id"],
                       "db_id": q["db_id"], "group": group,
                       "failure_type": "api_error", "error": str(e)}
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            f.flush()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True, choices=list(GROUPS))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--out", default=None,
                    help="输出文件名（默认 <group>.jsonl），避免覆盖全量结果")
    a = ap.parse_args()
    main(a.group, a.limit, a.concurrency, a.out)

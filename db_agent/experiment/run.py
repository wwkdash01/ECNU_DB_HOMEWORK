import json
import os
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

from db_agent import config
from db_agent.core.data import load_questions, build_context
from db_agent.experiment.methods import (run_oneshot, run_selfconsistency,
                                         run_selfrefine, run_agent)
from db_agent.experiment.tools import tools_for

GROUPS = {
    # 2×2 因子：迭代（1 轮 / 3 轮）× 形态自检（无 / 有），外加反馈消融 A2
    "O1":  (run_oneshot,         config.TEMPERATURE,          None,              {}),
    "O1P": (run_oneshot,         config.TEMPERATURE,          None,              {"shape": True}),
    "O3":  (run_selfconsistency, config.TEMPERATURE_SAMPLING, None,              {}),
    "A0":  (run_selfrefine,      config.TEMPERATURE,          None,              {"shape": False}),
    "A0P": (run_selfrefine,      config.TEMPERATURE,          None,              {"shape": True}),
    "A2":  (run_agent,           config.TEMPERATURE,          tools_for("A2"),
            {"feedback": False, "max_steps": config.A2_MAX_STEPS}),
    "A1":  (run_agent,           config.TEMPERATURE,          tools_for("A1"),   {}),
}

# 主实验（docs/实验结论.md）使用的 provider。默认输出文件名只对它有特殊待遇，
# 其它 provider 一律带后缀 —— 否则一旦设置 DB_AGENT_MODEL，新模型会覆盖 <group>.jsonl。
CANONICAL_PROVIDER = "deepseek-flash"


def load_done(path):
    """已完成题目集合。api_error 不算完成，否则续跑永远不会重试失败的题。"""
    done = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("failure_type") == "api_error":
                    continue
                key = rec.get("qidx")
                if key is None:
                    key = rec.get("question_id")
                if key is not None:
                    done.add(key)
    return done


def ensure_dir(p):
    """mkdir -p，但对「已存在」完全静默。

    某些运行环境（含编辑器/沙箱的 fs 代理层）会把 mkdir(exist_ok=True) 的已存在
    情况抛成 FileExistsError，这里统一吞掉，避免跑批在起点就崩。
    """
    try:
        p.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        pass
    return p


def run_one(q, group):
    method, temp, tools, kw = GROUPS[group]
    ctx = build_context(q["db_id"], q.get("evidence"))
    t0 = time.perf_counter()
    r = method(ctx, q["question"], q["db_id"], temp, **kw) if tools is None \
        else method(ctx, q["question"], q["db_id"], temp, tools, **kw)
    return {
        "qidx": q["qidx"], "question_id": q["question_id"], "db_id": q["db_id"],
        "difficulty": q.get("difficulty", "moderate"), "group": group,
        "provider": config.ACTIVE_PROVIDER,
        "model": config.MODEL, "model_version": config.MODEL_VERSION,
        "run_config": {
            "temperature": temp,
            "max_tokens": config.MAX_TOKENS,
            "thinking_disabled": config.THINKING_DISABLED,
            "selfrefine_rounds": config.SELFREFINE_ROUNDS,
            "extra_body": config.EXTRA_BODY,
        },
        "prompt_context": ctx, "latency_total": time.perf_counter() - t0,
        **r,
    }


def default_out(group):
    """默认文件名带上 provider，避免不同模型互相覆盖；主实验 provider 保持旧命名。"""
    if config.ACTIVE_PROVIDER == CANONICAL_PROVIDER:
        return f"{group}.jsonl"
    safe = config.ACTIVE_PROVIDER.replace("/", "_").replace(":", "_")
    return f"{group}__{safe}.jsonl"


def main(group, limit=None, concurrency=None, out=None, model=None):
    if model:
        config.use_provider(model)
    ensure_dir(config.RESULTS_DIR)
    qs = load_questions()
    if limit:
        qs = qs[:limit]
    out_path = config.RESULTS_DIR / (out or default_out(group))
    ensure_dir(out_path.parent)
    done = load_done(out_path)
    todo = [q for q in qs if q["qidx"] not in done]
    print(f"[{group}] provider={config.ACTIVE_PROVIDER} model={config.MODEL} "
          f"输出 {out_path}  待跑 {len(todo)}（已完成 {len(done)}）")

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
                       "provider": config.ACTIVE_PROVIDER, "model": config.MODEL,
                       "failure_type": "api_error", "error": str(e)}
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            f.flush()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True, choices=list(GROUPS))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--out", default=None,
                    help="输出文件名（可含子目录，如 qwen/A1.jsonl）；"
                         "默认 <group>.jsonl，非默认模型为 <group>__<provider>.jsonl")
    ap.add_argument("--model", default=None,
                    help=f"provider 名（默认 {config.DEFAULT_PROVIDER}）；"
                         f"可选：{', '.join(config.list_providers())}")
    a = ap.parse_args()
    main(a.group, a.limit, a.concurrency, a.out, a.model)

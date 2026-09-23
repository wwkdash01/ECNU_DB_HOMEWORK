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
    # A4：替代方案 —— A1v2 基线上增加 explore_schema（表连通/桥接表检索）。
    # 打破 A1⊂A2⊂A3 的递增链，故意如此：错误结构显示瓶颈在选表(37.6%)而非取值。
    "A4": (run_agent,            config.TEMPERATURE,          tools_for("A4")),
    # A5：粒度契约（declare_shape）。A1 基线上只加一个工具，用于 pilot 验证。
    "A5": (run_agent,            config.TEMPERATURE,          tools_for("A5")),
    # A6/A7：结构化 CoT（查询计划 / 分治）。工具与 A1 相同，只换推理结构。
    "A6": (run_agent,            config.TEMPERATURE,          tools_for("A6")),
    "A7": (run_agent,            config.TEMPERATURE,          tools_for("A7")),
    # A8：值域存在性检查（第 2 层）。
    "A8": (run_agent,            config.TEMPERATURE,          tools_for("A8")),
    # A9：原生思考模式（CoT）。
    #   A9 = A1 的【逐字同一 prompt + 逐字同一工具】，唯一自变量是 thinking 开关。
    #   与 A6/A7 的区别必须写清楚：A6/A7 是"逼模型在 content 里手写推理"（外部文本，
    #   可以敷衍），A9 是模型自身的解码路径（内部推理，且会被回灌进后续轮）。
    #   老组完全不受影响：temperature 照常生效、max_tokens 仍是 2048。
    "A9": (run_agent,            config.TEMPERATURE,          tools_for("A9")),
    # A10：方向一 —— 解释阶段承诺（六要素）+ declare_intent 硬门 + run_sql 语义对质。
    #   与 A1 的唯一自变量：执行前必须先固定题目解释，且该解释会被库内真值对质。
    #   与 A5（declare_shape）的区别：A5 在 SQL 写好后校验形态（实测被忽略 49/49），
    #   A10 把承诺提前到任何执行之前，并用硬门强制（不承诺就拒绝 run_sql）。
    "A10": (run_agent,           config.TEMPERATURE,          tools_for("A10")),
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
        else method(ctx, q["question"], q["db_id"], temp, tools, group)
    return {
        "qidx": q["qidx"], "question_id": q["question_id"], "db_id": q["db_id"],
        "difficulty": q.get("difficulty", "moderate"), "group": group,
        "prompt_context": ctx, "latency_total": time.perf_counter() - t0,
        "model_version": config.MODEL, **r,
    }


def load_only(path):
    """读 {qidx:[...]} —— 定向抽样用。

    ★ 为什么需要它（实测教训）：第一版 A4 冒烟直接取前 8 题（--limit 8），
      结果 8 题全来自一个库，**且 A1v2 的表选择本来就全对** ——
      抽样完全没打到"表选错"这个靶子，得出的 +37pt 是假的。
      定向抽样必须能指定题目子集，否则 pilot 只能按顺序取前缀，无法对准假设。
    """
    import json as _json
    d = _json.loads(open(path, encoding="utf-8").read())
    return set(d["qidx"] if isinstance(d, dict) else d)


def main(group, limit=None, concurrency=None, out=None, only=None):
    config.RESULTS_DIR.mkdir(exist_ok=True)
    qs = load_questions()
    if only:
        sel = load_only(only)
        qs = [q for q in qs if q["qidx"] in sel]
        missing = sel - {q["qidx"] for q in qs}
        if missing:
            print(f"⚠ --only 里有 {len(missing)} 个 qidx 不在题库: {sorted(missing)[:10]}")
    elif limit:
        qs = qs[:limit]                                  # 冒烟：固定取前 N 条
    # --out 允许把实验写到独立文件（如 A1v2.jsonl）。
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
                    help="输出文件名（默认 <group>.jsonl）。跑变体实验时用它避免覆盖全量")
    ap.add_argument("--only", default=None,
                    help="只跑指定题号：JSON 文件，内容为 [qidx,...] 或 {\"qidx\":[...]}。"
                         "定向抽样（对准失败类型）用，比 --limit 取前缀有效")
    a = ap.parse_args()
    main(a.group, a.limit, a.concurrency, a.out, a.only)

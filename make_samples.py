"""make_samples.py —— 生成所有【题目子集清单】，并把生成方式固化下来

★ 为什么必须有这个文件（收尾时发现的真实缺口）：
  之前 5 个抽样清单直接写在 `results/` 下，而 `results/` 被 .gitignore 忽略
  （原始产物每组约 8 MB，不适合版本化）。后果是：
  **fresh clone 之后清单消失，而"到底跑了哪 100 题"是结论的一部分，无法复现。**
  更糟的是 `_strat100_qidx.json` 当时**没有任何生成脚本**，种子和分层方式
  只存在于对话里 —— 这是不可复现的隐患，已在本文件里补齐并验证可精确复现。

★ 两种抽样用途完全不同，别混用（本项目三次假信号都源于混用）：
    分层抽样（本文件）     按【难度】分层，比例与全量一致 → **才能估效果**
    make_pilot_targeted.py  按【失败类型】定向            → **只能定位机制**

用法：
    python make_samples.py            # 重新生成全部清单并校验（不发 API）
    python make_samples.py --check    # 只校验现有清单与生成逻辑是否一致
"""
import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")
import config
from data import load_questions

# 清单落在 data/ 而不是 results/ —— results/ 被 .gitignore 忽略（产物太大），
# 而"跑了哪些题"是口径的一部分：这些清单合计不到 2 KB，必须随代码版本化。
# 挪进来之前的隐患：fresh clone 后清单消失，且 _strat100 当时没有生成脚本。
OUT_DIR = config.DATA_DIR

SEED = 20260921                      # 与 _strat100 实际生成时一致（已反推验证）
# 分层顺序会影响 random 的取数轨迹，必须写死 —— 否则换个顺序就复现不出来
ORDER = ("challenging", "moderate", "simple")
# 全量 500 的真实比例：148 / 250 / 102 = 29.6% / 50.0% / 20.4%
WEIGHT = {"simple": 148 / 500, "moderate": 250 / 500, "challenging": 102 / 500}


def stratified(qs, n, seed=SEED):
    """按难度分层抽 n 题，比例与全量一致。"""
    pool = {}
    for q in qs:
        pool.setdefault(q.get("difficulty", "moderate"), []).append(q["qidx"])
    quota = {d: round(WEIGHT[d] * n) for d in WEIGHT}
    # 四舍五入可能少一题，补给 challenging（比例上它最容易被 round 掉）
    diff = n - sum(quota.values())
    if diff:
        quota["challenging"] += diff
    rng = random.Random(seed)
    picked = []
    for d in ORDER:
        cand = sorted(pool[d])
        if len(cand) < quota[d]:
            raise SystemExit(f"题库不足：{d} 只有 {len(cand)} 题，需要 {quota[d]}")
        picked += rng.sample(cand, quota[d])
    return sorted(picked), quota


def build(qs):
    out = {}
    p100, q100 = stratified(qs, 100)
    out["strat100_qidx.json"] = {"qidx": p100, "seed": SEED, "order": list(ORDER),
                                 "stratified": q100,
                                 "note": "全量比例分层抽样，供代表性评估用"}

    p30, q30 = stratified(qs, 30)
    out["strat30_qidx.json"] = {"qidx": p30, "seed": SEED, "order": list(ORDER),
                                "stratified": q30, "source": "strat100 的子集口径，重新分层",
                                "note": "n=30 只够发现 >15pt 差异，不可作为最终判断"}

    chal = sorted(q["qidx"] for q in qs if q.get("difficulty") == "challenging")
    out["chall102_qidx.json"] = {"qidx": chal,
                                 "note": "全量 500 的 challenging 层（非抽样，是全集）"}
    return out


def main(check=False):
    qs = load_questions()
    built = build(qs)

    # ---- 校验生成逻辑能精确复现归档清单（这是"可复现"的证据，不是空话）----
    bad = []
    for name, obj in built.items():
        p = OUT_DIR / name
        if p.exists():
            old = json.loads(p.read_text(encoding="utf-8"))
            if old.get("qidx") != obj["qidx"]:
                bad.append(name)
    if bad:
        print(f"❌ 生成逻辑与归档清单不一致: {bad}")
        print("   若这是有意更换抽样，请确认后再覆盖；否则说明种子/顺序/配额被改动过。")
        return 1
    print("✅ 生成逻辑与归档清单逐题一致（可精确复现）")

    if check:
        return 0

    for name, obj in built.items():
        p = OUT_DIR / name
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        c = Counter()
        for i in obj["qidx"]:
            c[next(q["difficulty"] for q in qs if q["qidx"] == i)] += 1
        n = len(obj["qidx"])
        frac = "  ".join(f"{d}:{c[d]}({c[d]/n:.1%})" for d in ORDER)
        print(f"  写出 {name:22s} {n:3d} 题   {frac}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不写出")
    a = ap.parse_args()
    sys.exit(main(a.check))

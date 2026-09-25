"""paired.py —— 两组实验的配对比较（McNemar 精确检验）

用法：
    python -m db_agent pair O1_scored.jsonl A1_scored.jsonl

★ 为什么必须配对，不能比两个绝对数：
  本仓库实测 T=0 也【不可复现】——同配置重跑，SQL 只有 ~30% 完全相同，
  30 题里会有 ~2 题 EX 翻盘。所以"历史 O1 的 58.4%"和"今天 A1 的某个数"
  直接相减，差额里混着两次运行的漂移。McNemar 只看【同一题上两组是否不一致】，
  把不翻盘的题排除掉，剩下的差值才是处理效应。

McNemar 精确检验（而非卡方近似）：
  H0: 每个不一致对，A 对/B 对的概率各 1/2。
  在 b+c 个不一致对里，b ~ Binomial(b+c, 0.5)（条件检验，与一致对数无关）。
  小样本必须用精确分布，卡方近似在 b+c < 25 时不可靠。
"""
import json
import sys
from collections import Counter
from math import comb
from pathlib import Path

from db_agent import config

# ★ RESULTS 必须走 config：本文件已迁入 db_agent/evaluation/，
#   再用 __file__ 推 "results" 会指向包内。config.ROOT 指向仓库根，两者口径统一。
RESULTS = config.RESULTS_DIR


def load(name):
    """读 _scored.jsonl -> {qidx: (is_correct, difficulty)}

    ★ 解析顺序：【先 cwd，再 RESULTS】。反过来（RESULTS 优先）会让 `results/` 下的
      同名文件【静默遮蔽】cwd 里的文件：在 `results/repro3/` 里跑
      `python -m db_agent pair O1_scored.jsonl A1_scored.jsonl` 会读到上一级归档那一批，
      数字看着完全正常，却根本不是本批次的数据。

    改序不改变任何现有用法：从仓库根目录跑裸文件名时，根目录下没有该文件，
    仍然回落到 RESULTS。
    """
    p = Path(name)
    if not p.exists():
        p = RESULTS / name
    if not p.exists():
        raise SystemExit(f"找不到 {name}（既不在 cwd 也不在 {RESULTS}）")
    d = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("failure_type") == "api_error" or "final_sql" not in r:
            continue                      # 与 score.py 同口径：采集失败不算错
        d[r["qidx"]] = (int(r.get("is_correct") or 0), r.get("difficulty", "?"))
    return d


def mcnemar_exact(b, c):
    """双侧精确 p 值。b = 甲错乙对，c = 甲对乙错。"""
    n = b + c
    if n == 0:
        return 1.0
    # 偏离期望(中位数)越远的尾部越极端；用 min(b,c) 侧的两倍尾概率
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def main(name_a, name_b, label_a=None, label_b=None):
    A, B = load(name_a), load(name_b)
    idx = sorted(set(A) & set(B))
    if not idx:
        raise SystemExit("两组没有任何共同 qidx —— 不可配对")
    la = label_a or name_a.replace("_scored.jsonl", "")
    lb = label_b or name_b.replace("_scored.jsonl", "")
    only_a, only_b = set(A) - set(B), set(B) - set(A)
    if only_a or only_b:
        print(f"⚠ 仅 {la} 有 {len(only_a)} 题、仅 {lb} 有 {len(only_b)} 题，已取交集 {len(idx)} 题")

    a_hit = sum(A[i][0] for i in idx)
    b_hit = sum(B[i][0] for i in idx)
    n = len(idx)
    ea, eb = a_hit / n, b_hit / n

    # 2x2：a_correct x b_correct
    both = [i for i in idx if A[i][0] and B[i][0]]
    a_only = [i for i in idx if A[i][0] and not B[i][0]]
    b_only = [i for i in idx if B[i][0] and not A[i][0]]
    neither = [i for i in idx if not A[i][0] and not B[i][0]]
    p = mcnemar_exact(len(b_only), len(a_only))

    # 配对差的 Wilson 区间（按不一致对数做正态近似，仅作粗参考）
    disc = len(a_only) + len(b_only)
    delta = eb - ea

    print(f"\n配对比较  n = {n} 题（同一批题、同一天跑）")
    print(f"  {la:>24s}  EX = {a_hit:3d}/{n} = {ea:7.2%}")
    print(f"  {lb:>24s}  EX = {b_hit:3d}/{n} = {eb:7.2%}")
    print(f"  {'Δ (' + lb + ' − ' + la + ')':>24s}  = {delta*100:+.2f} pt")
    print(f"\n  2x2 列联表（行={la}，列={lb}）")
    print(f"                      {lb}=对   {lb}=错")
    print(f"    {la}=对            {len(both):4d}      {len(a_only):4d}")
    print(f"    {la}=错            {len(b_only):4d}      {len(neither):4d}")
    print(f"\n  McNemar 精确检验（双侧）")
    print(f"    甲错乙对 b = {len(b_only)}   ← {lb} 救回的题")
    print(f"    甲对乙错 c = {len(a_only)}   ← {lb} 弄坏的题")
    print(f"    净翻转     = {len(b_only) - len(a_only):+d} 题"
          f"  （{delta*100:+.2f} pt = {len(b_only)-len(a_only)}/{n}）")
    print(f"    不一致对   = {disc}（只有这些题携带处理效应信息）")
    if disc == 0:
        print(f"    p = 1.0000  ← 两组逐题完全一致，无差异")
    else:
        print(f"    p = {p:.4f}" + ("  显著 (<0.05)" if p < 0.05 else "  不显著 (≥0.05)"))
    if disc and disc < 25:
        print(f"    ⚠ 不一致对只有 {disc} 对：即便 p<0.05 也很脆弱；"
              f"n=30 只够发现 ~±15pt 量级的差异")

    # 分层
    print(f"\n  按难度分层（EX）")
    print(f"    {'难度':<12s} {'n':>3s}  {la:>10s}  {lb:>10s}   {'Δpt':>7s}")
    for d in ("simple", "moderate", "challenging"):
        sub = [i for i in idx if A[i][1] == d]
        if not sub:
            continue
        ha = sum(A[i][0] for i in sub)
        hb = sum(B[i][0] for i in sub)
        print(f"    {d:<12s} {len(sub):3d}  {ha/len(sub):9.1%}  {hb/len(sub):9.1%}"
              f"   {(hb-ha)/len(sub)*100:+6.1f}")

    # 翻转题清单：离线归因的入口
    print(f"\n  翻盘题 qidx")
    print(f"    {lb} 救回 ({len(b_only)}): {b_only}")
    print(f"    {lb} 弄坏 ({len(a_only)}): {a_only}")
    return dict(n=n, a=ea, b=eb, delta=delta, p=p,
                rescued=b_only, broke=a_only, both=both, neither=neither)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    main(*sys.argv[1:3])

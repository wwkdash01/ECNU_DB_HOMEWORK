"""methods.py —— 五组对照的实验机制（技术含量最高的文件）

    O1  run_oneshot         单次调用，无工具，T=0.0
    O3  run_selfconsistency 采样 K=3，T=0.7，按【执行结果集】聚类后选出一条 SQL
    A1  run_agent           agent 循环，工具=[run_sql]
    A2  run_agent           工具=+get_column_values
    A3  run_agent           工具=+get_column_desc

统一返回结构（见 _blank），每个 turn 都埋了 call_latency + 4 个 token 分项，
因为阶段 7 的 EX@k / 延迟分解 / 成本 / 工具分布全靠 turns。

相对手册的加固：
  * O3 记录候选执行失败的原因（手册用裸 except 吞掉，导致无法解释 O3 数据）
  * 用 time.perf_counter() 包整题，与 run.py 的 latency_total 独立计算以防遗忘
"""
import json
import time

import config
from llm import chat, extract_sql
from prompts import oneshot_prompt, agent_system
from tools import dispatch
from data import get_conn
from db import execute, QueryTimeout


def _blank():
    return {"final_sql": None, "turns": [], "candidates": [], "first_exec_ok": None}


# ---------------------------------------------------------------- O1
def run_oneshot(context, question, db_id, temperature):
    r = _blank()
    msg, meta = chat([{"role": "user", "content": oneshot_prompt(context, question)}],
                     temperature=temperature)
    sql = extract_sql(msg.content)
    r["final_sql"] = sql
    r["turns"].append({"turn": 0, "sql": sql, "tool_calls": [], "tool_results": [],
                       "qet": None, **meta})
    return r


# ---------------------------------------------------------------- O3
def _exec_hash(conn, sql):
    """执行候选 SQL，返回其结果集指纹。

    用 frozenset 是因为 hash() 只接受不可变对象；frozenset 同时丢掉行序，
    与官方 EX 的 set(pred)==set(gold) 口径一致（忽略列序与重复行）。
    """
    res = set(map(tuple, execute(conn, sql)["rows"]))
    return hash(frozenset(res))


def run_selfconsistency(context, question, db_id, temperature):
    """O3：并行采样 ×3 + 按执行结果投票。

    注意命名（报告里要写准）：投的是【哪个 SQL】而不是哪个结果集——
    用结果集做聚类依据，再从胜出簇里挑一条真实可执行的 SQL 输出
    （结果集本身无法交给 execute_sql 判分）。
    """
    r = _blank()
    conn = get_conn(db_id)
    cands, hashes, exec_errors = [], [], []

    for i in range(config.K_CANDIDATES):
        msg, meta = chat([{"role": "user", "content": oneshot_prompt(context, question)}],
                         temperature=temperature)          # ← O3 用 0.7
        sql = extract_sql(msg.content)
        cands.append(sql)

        h, err = None, None
        if sql:
            try:
                h = _exec_hash(conn, sql)                   # 与官方 EX 同口径
            except QueryTimeout:
                err = "timeout"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        else:
            err = "parse_error"
        hashes.append(h)
        exec_errors.append(err)

        r["turns"].append({"turn": i, "sql": sql, "tool_calls": [], "tool_results": [],
                           "qet": None, "result_hash": h, "exec_error": err, **meta})

    r["candidates"] = cands
    r["exec_errors"] = exec_errors

    groups = {}
    for sql, h in zip(cands, hashes):
        if h is not None:                                   # 执行失败的候选不参与投票
            groups.setdefault(h, []).append(sql)
    r["groups"] = [{"size": len(v), "sqls": v} for v in groups.values()]

    r["final_sql"] = (max(groups.values(), key=len)[0] if groups
                      else next((s for s in cands if s), None))   # 全失败则退回首个非空
    conn.close()
    return r


# ---------------------------------------------------------------- A1 / A2 / A3
def run_agent(context, question, db_id, temperature, tools):
    """agent 循环。MAX_STEPS 是【上限而非固定轮数】：模型不再请求工具即停。

    "成功即停"是 EX@k 曲线成立的前提（若固定跑满 3 轮，逐轮累计正确率无从谈起）。
    """
    r = _blank()
    conn = get_conn(db_id)
    messages = [{"role": "system", "content": agent_system(context)},
                {"role": "user", "content": question}]

    for step in range(config.MAX_STEPS):
        msg, meta = chat(messages, tools=tools, temperature=temperature)
        sql = extract_sql(msg.content)
        turn = {"turn": step, "sql": sql, "tool_calls": [], "tool_results": [],
                "qet": None, "called_tools": False, **meta}

        if not msg.tool_calls:                  # 不再请求工具 = 要交答案了 -> 成功即停
            r["turns"].append(turn)
            r["final_sql"] = sql
            break

        turn["called_tools"] = True
        messages.append(msg)                    # 助手消息（含 tool_calls）进历史
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            out, qet = dispatch(conn, db_id, tc.function.name, args)
            turn["tool_calls"].append({"name": tc.function.name, "args": args})
            turn["tool_results"].append(out)
            if tc.function.name == "run_sql":
                if r["first_exec_ok"] is None:  # 只记【首次】run_sql，供自纠成功率分析
                    r["first_exec_ok"] = not out.startswith("执行失败")
                turn["qet"] = qet
            # ★ 这一行就是"回灌"：工具结果（含报错原文）进入下一轮的上下文
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})

        r["turns"].append(turn)
        r["final_sql"] = sql                    # 用尽轮数时留最后一轮（手册 §1.6）

    r["messages_final"] = messages              # 供人工核对"报错确实被回灌"
    conn.close()
    return r

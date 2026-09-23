"""methods.py —— 三组对照的实验机制（技术含量最高的文件）

    O1  run_oneshot         单次调用，无工具，T=0.0
    O3  run_selfconsistency 采样 K=3，T=0.7，按【执行结果集】聚类后选出一条 SQL
    A1  run_agent           agent 循环，工具 = [run_sql, submit_answer]，T=0.0
                            （A1 ≡ 历史文档里的 A1v2：含输出形态自检）

统一返回结构（见 _blank），每个 turn 都埋了 call_latency + 4 个 token 分项，
因为 EX@k / 延迟分解 / 成本 / 工具分布全靠 turns。

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
    return {"final_sql": None, "turns": [], "candidates": [], "first_exec_ok": None,
            "final_sql_source": None, "submitted": False, "submit_attempts": 0}


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

    ★ T=0.7 是机制的内在要求：T=0 时同一 prompt 采样三次会得到三条相同 SQL，
      投票无意义、O3 退化成 O1。且必须显式关闭思考模式，否则 temperature
      被平台静默忽略，同样退化成 O1。
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


# ---------------------------------------------------------------- A1

def _rejected(r, db_id):
    """末轮答案是否应被否决：在真实库上执行失败（语法错/表不存在/超时）。

    只用于【末轮（禁工具）】产生的答案；成败本身不泄漏给 agent。
    """
    if not config.FALLBACK_TO_LAST_EXECUTED:
        return False
    sql = r.get("final_sql")
    if not sql:
        return False
    try:
        conn = get_conn(db_id)
        try:
            execute(conn, sql)
            return False
        finally:
            conn.close()
    except Exception:
        return True


def run_agent(context, question, db_id, temperature, tools):
    """agent 循环。MAX_STEPS 是【上限而非固定轮数】：模型提交答案或不再请求工具即停。

    ★ 终止动作 submit_answer（本仓库修正，实测必需）：
      实测该模型在 3 轮里【不会】主动停止请求工具（20/20 打满上限）；而工具被禁用后，
      它会把「工具调用语法」当纯文本吐出来，导致 45% 的 final_sql 无法执行。
      根因是缺一个可靠的"交答案"动作——它擅长调工具，不擅长"最后一句写对"。
      submit_answer 让提交变成工具调用，且提交物必须能在库上执行。

    ★ 末轮只留 submit_answer：探索类工具撤走，模型只能提交或作答。
    ★ 兜底：若最终仍无可用 SQL，退回"最后一条真的执行成功过的 SQL"，
      并标记 final_sql_source 供报告披露，避免把采集失败静默归因成模型能力。
    """
    r = _blank()
    conn = get_conn(db_id)
    messages = [{"role": "system", "content": agent_system(context)},
                {"role": "user", "content": question}]
    last_ok_sql = None
    submit_tool = [t for t in (tools or []) if t["function"]["name"] == "submit_answer"]

    try:
        for step in range(config.MAX_STEPS):
            # 末轮：撤走探索类工具，只保留 submit_answer
            is_last = (step == config.MAX_STEPS - 1)
            offered = submit_tool if is_last else tools
            msg, meta = chat(messages, tools=offered, temperature=temperature)
            sql = extract_sql(msg.content)
            turn = {"turn": step, "sql": sql, "tool_calls": [], "tool_results": [],
                    "qet": None, "called_tools": False,
                    "tools_offered": [t["function"]["name"] for t in (offered or [])],
                    **meta}
            submitted = False

            if not msg.tool_calls:              # 不再请求工具 = 用文本作答
                r["turns"].append(turn)
                r["final_sql"] = sql
                if sql:
                    r["final_sql_source"] = "model"
                break

            turn["called_tools"] = True
            messages.append(msg)                # 助手消息（含 tool_calls）进历史
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                out, qet = dispatch(conn, db_id, tc.function.name, args)
                turn["tool_calls"].append({"name": tc.function.name, "args": args})
                turn["tool_results"].append(out)

                if tc.function.name == "run_sql":
                    if r["first_exec_ok"] is None:   # 只记【首次】run_sql
                        r["first_exec_ok"] = not out.startswith("执行失败")
                    turn["qet"] = qet
                    if out.startswith("执行成功"):
                        cand = (args.get("sql") or "").strip().rstrip(";")
                        if cand:
                            last_ok_sql = cand   # 兜底用：真的执行成功过的 SQL
                elif tc.function.name == "submit_answer":
                    r["submit_attempts"] += 1
                    if out.startswith("已收到最终答案"):
                        cand = (args.get("sql") or "").strip().rstrip(";")
                        r["submitted"] = True
                        r["final_sql"] = cand
                        r["final_sql_source"] = "submitted"
                        submitted = True
                    # 提交失败不终止：错误文本已进 messages，模型可修正后重提交

                # ★ 这一行就是"回灌"：工具结果（含报错原文）进入下一轮的上下文
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})

            r["turns"].append(turn)
            if submitted:
                break                           # 已提交，任务结束
            r["final_sql"] = sql                # 否则留本轮文本 SQL 作为候选

        # 兜底：最终仍无可用 SQL 时，退回最后一条真的执行成功过的 SQL
        if last_ok_sql and (not r["final_sql"] or _rejected(r, db_id)):
            if r["final_sql"]:
                r["model_answer_rejected"] = True
            r["final_sql"] = last_ok_sql
            r["final_sql_source"] = "last_executed"
    finally:
        conn.close()

    r["messages_final"] = messages              # 供人工核对"报错确实被回灌"
    return r

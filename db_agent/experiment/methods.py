import json
import time

from db_agent import config
from db_agent.core.llm import chat, extract_sql
from db_agent.experiment.prompts import (oneshot_prompt, oneshot_shape_prompt,
                                         agent_system, selfrefine_prompt)
from db_agent.experiment.tools import dispatch
from db_agent.core.data import get_conn
from db_agent.core.db import execute, QueryTimeout


def _blank():
    return {"final_sql": None, "turns": [], "candidates": [], "first_exec_ok": None,
            "final_sql_source": None, "submitted": False, "submit_attempts": 0}


def run_oneshot(context, question, db_id, temperature, shape=False):
    """O1（shape=False）/ O1+（shape=True）：单次调用，唯一差别是有无输出形态条款。"""
    r = _blank()
    prompt = (oneshot_shape_prompt(context, question) if shape
              else oneshot_prompt(context, question))
    msg, meta = chat([{"role": "user", "content": prompt}], temperature=temperature)
    sql = extract_sql(msg.content)
    r["final_sql"] = sql
    r["turns"].append({"turn": 0, "sql": sql, "tool_calls": [], "tool_results": [],
                       "qet": None, "stage": "draft_with_shape" if shape else "draft",
                       **meta})
    return r


def _exec_hash(conn, sql):
    res = set(map(tuple, execute(conn, sql)["rows"]))
    return hash(frozenset(res))


def run_selfrefine(context, question, db_id, temperature, shape=False):
    """A0 / A0+：多轮自我反思，全程不执行 SQL、不回灌任何数据。

    shape=False → A0（中性）：prompt 不含输出形态提示，只做语义/join/粒度的复查。
    shape=True  → A0+（= 旧 A0，prompt 冻结）：多一轮形态自检条款。

    与 A1 的差别因此只剩「执行反馈」，可以把「迭代」与「反馈」拆开。
    """
    r = _blank()
    prev_sql = None
    rounds = max(1, config.SELFREFINE_ROUNDS)

    for i in range(rounds):
        prompt = (oneshot_prompt(context, question) if i == 0
                  else selfrefine_prompt(context, question, prev_sql, shape=shape))
        msg, meta = chat([{"role": "user", "content": prompt}], temperature=temperature)
        sql = extract_sql(msg.content)
        r["turns"].append({"turn": i, "sql": sql, "tool_calls": [], "tool_results": [],
                           "qet": None, "stage": "draft" if i == 0 else "self_refine",
                           **meta})
        # 收敛即停：与上一轮完全一致时继续迭代没有信息增量
        if i > 0 and sql == prev_sql:
            r["converged_at"] = i
            break
        r["final_sql"] = sql
        r["final_sql_source"] = "draft" if i == 0 else "self_refine"
        prev_sql = sql

    return r


def run_selfconsistency(context, question, db_id, temperature):
    r = _blank()
    conn = get_conn(db_id)
    cands, hashes, exec_errors = [], [], []

    for i in range(config.K_CANDIDATES):
        msg, meta = chat([{"role": "user", "content": oneshot_prompt(context, question)}],
                         temperature=temperature)
        sql = extract_sql(msg.content)
        cands.append(sql)

        h, err = None, None
        if sql:
            try:
                h = _exec_hash(conn, sql)
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
        if h is not None:
            groups.setdefault(h, []).append(sql)
    r["groups"] = [{"size": len(v), "sqls": v} for v in groups.values()]

    r["final_sql"] = (max(groups.values(), key=len)[0] if groups
                      else next((s for s in cands if s), None))
    conn.close()
    return r


def _rejected(r, db_id):
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


def run_agent(context, question, db_id, temperature, tools, feedback=True, max_steps=None):
    """A1（feedback=True）/ A2（feedback=False，反馈消融）。

    两组共用同一个 system prompt、同一组工具、同一套循环；A2 只把 run_sql /
    submit_answer 的回灌内容换成不含任何执行信息的桩。因此 A1 − A2 就是
    「执行反馈」这一个变量的净效应。
    """
    r = _blank()
    conn = get_conn(db_id)
    steps = max(1, max_steps or config.MAX_STEPS)
    messages = [{"role": "system", "content": agent_system(context)},
                {"role": "user", "content": question}]
    last_ok_sql = None
    submit_tool = [t for t in (tools or []) if t["function"]["name"] == "submit_answer"]

    try:
        for step in range(steps):
            is_last = (step == steps - 1)
            offered = submit_tool if is_last else tools
            msg, meta = chat(messages, tools=offered, temperature=temperature)
            sql = extract_sql(msg.content)
            turn = {"turn": step, "sql": sql, "tool_calls": [], "tool_results": [],
                    "qet": None, "called_tools": False,
                    "tools_offered": [t["function"]["name"] for t in (offered or [])],
                    **meta}
            submitted = False

            if not msg.tool_calls:
                r["turns"].append(turn)
                r["final_sql"] = sql
                if sql:
                    r["final_sql_source"] = "model"
                break

            turn["called_tools"] = True
            messages.append(msg)
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                out, qet = dispatch(conn, db_id, tc.function.name, args, feedback=feedback)
                turn["tool_calls"].append({"name": tc.function.name, "args": args})
                turn["tool_results"].append(out)

                if tc.function.name == "run_sql":
                    if r["first_exec_ok"] is None:
                        r["first_exec_ok"] = not out.startswith("执行失败")
                    turn["qet"] = qet
                    if out.startswith("执行成功"):
                        cand = (args.get("sql") or "").strip().rstrip(";")
                        if cand:
                            last_ok_sql = cand
                elif tc.function.name == "submit_answer":
                    r["submit_attempts"] += 1
                    if out.startswith("已收到最终答案"):
                        cand = (args.get("sql") or "").strip().rstrip(";")
                        r["submitted"] = True
                        r["final_sql"] = cand
                        r["final_sql_source"] = "submitted"
                        submitted = True

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})

            r["turns"].append(turn)
            if submitted:
                break
            r["final_sql"] = sql

        # 采集兜底只对 A1 有意义：A2 全程拿不到执行成功，不存在 last_ok_sql
        if feedback and last_ok_sql and (not r["final_sql"] or _rejected(r, db_id)):
            if r["final_sql"]:
                r["model_answer_rejected"] = True
            r["final_sql"] = last_ok_sql
            r["final_sql_source"] = "last_executed"
    finally:
        conn.close()

    r["messages_final"] = messages
    return r

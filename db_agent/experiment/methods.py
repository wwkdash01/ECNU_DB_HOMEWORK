import json
import time

from db_agent import config
from db_agent.core.llm import chat, extract_sql
from db_agent.experiment.prompts import oneshot_prompt, agent_system
from db_agent.experiment.tools import dispatch
from db_agent.core.data import get_conn
from db_agent.core.db import execute, QueryTimeout


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
    res = set(map(tuple, execute(conn, sql)["rows"]))
    return hash(frozenset(res))


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


# ---------------------------------------------------------------- A1

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


def run_agent(context, question, db_id, temperature, tools):
    r = _blank()
    conn = get_conn(db_id)
    messages = [{"role": "system", "content": agent_system(context)},
                {"role": "user", "content": question}]
    last_ok_sql = None
    submit_tool = [t for t in (tools or []) if t["function"]["name"] == "submit_answer"]

    try:
        for step in range(config.MAX_STEPS):
            is_last = (step == config.MAX_STEPS - 1)
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
                out, qet = dispatch(conn, db_id, tc.function.name, args)
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

        if last_ok_sql and (not r["final_sql"] or _rejected(r, db_id)):
            if r["final_sql"]:
                r["model_answer_rejected"] = True
            r["final_sql"] = last_ok_sql
            r["final_sql_source"] = "last_executed"
    finally:
        conn.close()

    r["messages_final"] = messages
    return r

"""db.py —— SQL 执行层：唯一入口

职责三件：跑 SQL、管超时、记 QET。
不负责：改数据（连接是只读的）、判断对错（score.py）、回灌错误（tools.py）。

关键约束（实测）：
  * set_progress_handler 是【连接级】的，而 agent 一个连接会连续跑多次查询，
    因此必须在 finally 里清掉；否则下一次查询会带着已过期的 deadline，
    一启动就被立刻中断。这个 bug 只在【连续调用】时暴露，单次测试看不出来。
  * SQLite 没有原生查询超时；connect(timeout=) 是等锁超时，不是执行超时。
"""
import time
import sqlite3

import config


class QueryTimeout(Exception):
    """查询超过时限被中断。

    单独成类是因为它与"SQL 写错"的实验含义完全不同：
      QueryTimeout  -> 方法缺陷，计 EX=0 但【不剔除】（手册 §1.6）
      其他 OperationalError -> 有效信号，要回灌给模型让它自修
    """


def execute(conn, sql, row_limit=None, timeout=None):
    """执行一条 SQL，返回 {rows, qet, truncated}。

    失败时抛原始异常；超时抛 QueryTimeout（单列）。
    """
    row_limit = config.RESULT_ROW_LIMIT if row_limit is None else row_limit
    timeout   = config.QUERY_TIMEOUT_SEC if timeout is None else timeout

    # 用闭包标志 + 单一 deadline，避免在回调里反复调 time.time()
    state = {"expired": False}
    deadline = time.monotonic() + timeout

    def _on_progress():
        # 返回非 0 让 SQLite 中断当前查询
        if not state["expired"] and time.monotonic() > deadline:
            state["expired"] = True
        return 1 if state["expired"] else 0

    conn.set_progress_handler(_on_progress, 10_000)

    t0 = time.perf_counter()
    try:
        # 多取一行用于判断是否被截断
        rows = conn.execute(sql).fetchmany(row_limit + 1)
    except sqlite3.OperationalError as e:
        if state["expired"] or "interrupt" in str(e).lower():
            raise QueryTimeout(f"查询超过 {timeout}s 被中断") from e
        raise
    finally:
        # 无论成功、失败、超时都必须清掉，否则污染同连接的后续查询
        conn.set_progress_handler(None, 0)

    qet = time.perf_counter() - t0
    truncated = len(rows) > row_limit
    return {"rows": rows[:row_limit], "qet": qet, "truncated": truncated}

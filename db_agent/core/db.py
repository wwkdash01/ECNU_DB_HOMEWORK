import time
import sqlite3

from db_agent import config


class QueryTimeout(Exception):
    pass


def execute(conn, sql, row_limit=None, timeout=None):
    row_limit = config.RESULT_ROW_LIMIT if row_limit is None else row_limit
    timeout   = config.QUERY_TIMEOUT_SEC if timeout is None else timeout

    state = {"expired": False}
    deadline = time.monotonic() + timeout

    def _on_progress():
        if not state["expired"] and time.monotonic() > deadline:
            state["expired"] = True
        return 1 if state["expired"] else 0

    conn.set_progress_handler(_on_progress, 10_000)

    t0 = time.perf_counter()
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(row_limit + 1)
    except sqlite3.OperationalError as e:
        if state["expired"] or "interrupt" in str(e).lower():
            raise QueryTimeout(f"查询超过 {timeout}s 被中断") from e
        raise
    finally:
        conn.set_progress_handler(None, 0)

    qet = time.perf_counter() - t0
    truncated = len(rows) > row_limit
    return {"rows": rows[:row_limit], "cols": cols, "qet": qet, "truncated": truncated}

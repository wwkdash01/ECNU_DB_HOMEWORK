from db_agent import config
from db_agent.core.db import execute, QueryTimeout

TOOLS = [
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "在数据库上执行一条 SQL 查询并返回结果状态。"
                       "写好的 SQL 必须先用这个工具执行验证，不要直接给出未经执行的答案。"
                       "如果返回错误信息，请仔细阅读并修正查询后重新执行。",
        "parameters": {"type": "object",
            "properties": {"sql": {"type": "string", "description": "要执行的 SQLite 查询语句"}},
            "required": ["sql"]}}},
]

SUBMIT_TOOL = {"type": "function", "function": {
    "name": "submit_answer",
    "description": "提交最终答案并结束任务。当你确认 SQL 已经正确回答了用户问题时调用它。"
                   "参数必须是那条完整的、可直接执行的 SQL 查询。"
                   "调用后任务立即结束，所以不要在还没确定答案时调用。",
    "parameters": {"type": "object",
        "properties": {"sql": {"type": "string",
                               "description": "最终答案 SQL（完整的单条查询）"}},
        "required": ["sql"]}}}


def _cut(s):
    return s[:config.TOOL_OUTPUT_MAX_CHARS]


# A2 的反馈桩：与 A1 唯一不同的就是这句话的内容 —— 它不携带任何行列/报错信息。
NO_FEEDBACK_MSG = ("执行不可用：当前会话没有数据库访问权限，无法返回行数、列名或数据预览。"
                   "请只依据 schema 与问题本身推理，确认后调用 submit_answer 提交最终 SQL。")


def shape_of(cols):
    return f"{len(cols)} 列（{', '.join(str(c) for c in cols)}）"


def _run_sql(conn, sql):
    try:
        r = execute(conn, sql)
    except QueryTimeout as e:
        return _cut(f"执行失败：{e}"), None
    except Exception as e:
        return _cut(f"执行失败：{e}"), None
    tail = "（结果被截断）" if r["truncated"] else ""
    return _cut(f"执行成功，返回 {len(r['rows'])} 行，{shape_of(r['cols'])}{tail}。"
                f"预览：{r['rows'][:3]}"), r["qet"]


def _submit(conn, db_id, sql, feedback=True):
    sql = (sql or "").strip().rstrip(";")
    if not sql:
        return "提交失败：sql 参数为空，请给出完整的单条查询", None
    from db_agent.core.llm import _QUERY_RE
    if not _QUERY_RE.match(sql):
        return f"提交失败：不是一条查询语句。请提交 SELECT/WITH 查询。收到：{sql[:120]}", None
    if not feedback:
        # 反馈消融组：只做语法形态校验，不执行、不回任何行列信息
        return (f"已收到最终答案：{_cut(sql)}\n"
                f"（该答案未在数据库上执行，因此不返回行列信息。）"), None
    try:
        r = execute(conn, sql)
    except Exception as e:
        return f"提交失败：该 SQL 无法执行（{e}）。请修正后重新提交。", None
    return (f"已收到最终答案：{_cut(sql)}\n"
            f"该答案的结果形态：{len(r['rows'])} 行，{shape_of(r['cols'])}。\n"
            f"请自行确认：这个列数与列名正是问题所要求的吗？若多带了佐证列，"
            f"请修正后重新提交。"), None


def dispatch(conn, db_id, name, args, feedback=True):
    """feedback=False 时走 A2 的反馈桩：工具名与参数照旧，回灌内容不含任何执行信息。"""
    if name == "run_sql":
        if not feedback:
            return NO_FEEDBACK_MSG, None
        return _run_sql(conn, args.get("sql", ""))
    if name == "submit_answer":
        return _submit(conn, db_id, args.get("sql", ""), feedback=feedback)
    return f"未知工具：{name}", None


def tools_for(group):
    # A1：真实执行反馈；A2：同工具、同 prompt，只把回灌内容替换成桩（反馈消融）
    if group in ("A1", "A2"):
        return [TOOLS[0], SUBMIT_TOOL]
    return None

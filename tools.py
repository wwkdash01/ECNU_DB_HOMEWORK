"""tools.py —— 模型唯一的"动手"入口

三件事：工具声明（发给 API 的说明书）、参数校验与执行、按组裁剪。

关键设计（手册 §2.4，报告必须披露）：
    run_sql 成功时【只回行数 + 3 行预览，不回完整结果】。
    这样 A1 拿到的仅是「验证信号」，想拿数据内容必须调 get_column_values，
    从而保证 A1−O3（验证）与 A2−A1（值域获取）两层差异是干净的。

相对手册的两处加固：
    * get_column_values 加 ORDER BY —— 手册无 ORDER BY，SQLite 返回顺序
      取决于查询计划，同一题两次运行可能给出不同的取值样例，破坏可复现性。
    * _cut 按【条目】截断而不是把字符串硬切一半，避免把某个取值切成残缺片段。
"""
import config
from db import execute, QueryTimeout
from data import schema_whitelist, desc_index

TOOLS = [
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "在数据库上执行一条 SQL 查询并返回结果状态。"
                       "写好的 SQL 必须先用这个工具执行验证，不要直接给出未经执行的答案。"
                       "如果返回错误信息，请仔细阅读并修正查询后重新执行。",
        "parameters": {"type": "object",
            "properties": {"sql": {"type": "string", "description": "要执行的 SQLite 查询语句"}},
            "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "get_column_values",
        "description": "查看某一列在数据库中实际存储的取值（去重后的前若干个）。"
                       "当你不确定用户提到的业务名词（地区名、状态、类别、产品名等）"
                       "在数据库里具体存成什么字符串或编码时，用这个工具先确认。"
                       "这可以避免 WHERE 条件里写错值，导致查询返回 0 行却看不出问题。",
        "parameters": {"type": "object",
            "properties": {
                "table":  {"type": "string", "description": "表名，必须来自上面给出的 schema"},
                "column": {"type": "string", "description": "列名，必须来自上面给出的 schema"}},
            "required": ["table", "column"]}}},
    {"type": "function", "function": {
        "name": "get_column_desc",
        "description": "查看某个表某一列的官方说明文档，包含该列的业务含义与取值编码解释。"
                       "当列名是缩写、含义不明确（例如 Tot_liab、crt_dt 这类），"
                       "或你需要确认某字段代表什么业务指标时使用。",
        "parameters": {"type": "object",
            "properties": {
                "table":  {"type": "string", "description": "表名"},
                "column": {"type": "string", "description": "列名"}},
            "required": ["table", "column"]}}},
]

# 终止动作：模型用它提交最终答案并结束循环。
# 刻意【不放进 TOOLS】—— tools_for() 用 TOOLS[0:n] 切片，放进去会破坏 A1⊂A2⊂A3 与
# 各组的工具计数。它按组统一附加，不属于任何一组的"能力差异"。
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
    """按字符上限截断（_col_values 另有按条目的安全截断）"""
    return s[:config.TOOL_OUTPUT_MAX_CHARS]


def _cut_values(prefix, vals):
    """按【条目】截断：宁可不列某个值，也不把值切成半截。

    手册的 _cut 是 str[:2000]，遇到长值列表会把某个字符串切一半，
    模型看到的是损坏数据（如 'Calaver' 少了引号），可能据此写出错误的 WHERE。
    """
    kept, total = [], len(prefix)
    for v in vals:
        piece = repr(v)
        if total + len(piece) + 2 > config.TOOL_OUTPUT_MAX_CHARS:
            break
        kept.append(v)
        total += len(piece) + 2
    omitted = len(vals) - len(kept)
    tail = f"（另有 {omitted} 个未显示）" if omitted > 0 else ""
    return f"{prefix}：{kept}{tail}"


def _run_sql(conn, sql):
    try:
        r = execute(conn, sql)
    except QueryTimeout as e:
        return _cut(f"执行失败：{e}"), None
    except Exception as e:
        return _cut(f"执行失败：{e}"), None     # 报错原样回灌，模型多半能自己修
    tail = "（结果被截断）" if r["truncated"] else ""
    # 刻意只给 3 行预览：完整结果不从这里出去，见模块 docstring
    return _cut(f"执行成功，返回 {len(r['rows'])} 行{tail}。预览：{r['rows'][:3]}"), r["qet"]


def _col_values(conn, db_id, table, column):
    wl = schema_whitelist(db_id)
    if table not in wl or column not in wl[table]:
        return _cut(f"错误：{table}.{column} 不存在，请从 schema 里选择"), None
    # ORDER BY 保证输出确定（手册无 ORDER BY，同一题两次运行顺序可能不同）
    r = execute(conn, f'SELECT DISTINCT "{column}" FROM "{table}" '
                      f'ORDER BY "{column}" LIMIT {config.VALUE_SAMPLE_LIMIT}')
    vals = [row[0] for row in r["rows"]]
    return _cut_values(f"{table}.{column} 的取值样例（{len(vals)} 个）", vals), r["qet"]


def _col_desc(conn, db_id, table, column):
    wl = schema_whitelist(db_id)
    if table not in wl or column not in wl[table]:
        return _cut(f"错误：{table}.{column} 不存在，请从 schema 里选择"), None
    d, v = desc_index(db_id).get((table, column), ("", ""))
    if not d and not v:
        return f"没有 {table}.{column} 的说明文档", None
    return _cut(f"{table}.{column} 说明：{d}\n取值说明：{v}"), None


def _submit(conn, db_id, sql):
    """提交最终答案：只有在库上真的能执行才接受。

    失败时返回提示并让循环继续——模型可以读错误、改完再提交。
    这样"最终答案"必定是可执行 SQL，从根上消灭"畸形/散文当答案"的情形。
    """
    sql = (sql or "").strip().rstrip(";")
    if not sql:
        return "提交失败：sql 参数为空，请给出完整的单条查询", None
    from llm import _QUERY_RE
    if not _QUERY_RE.match(sql):
        return f"提交失败：不是一条查询语句。请提交 SELECT/WITH 查询。收到：{sql[:120]}", None
    try:
        execute(conn, sql)
    except Exception as e:
        return f"提交失败：该 SQL 无法执行（{e}）。请修正后重新提交。", None
    return f"已收到最终答案：{_cut(sql)}", None


def dispatch(conn, db_id, name, args):
    """返回 (文本, qet)。未知工具名返回友好错误，而不是抛异常打断 agent 循环。"""
    if name == "run_sql":
        return _run_sql(conn, args.get("sql", ""))
    if name == "get_column_values":
        return _col_values(conn, db_id, args.get("table", ""), args.get("column", ""))
    if name == "get_column_desc":
        return _col_desc(conn, db_id, args.get("table", ""), args.get("column", ""))
    if name == "submit_answer":
        return _submit(conn, db_id, args.get("sql", ""))
    return f"未知工具：{name}", None


def tools_for(group):
    """按组裁剪工具。必须按顺序切片，保证 A1 ⊂ A2 ⊂ A3，差值才有意义。

    submit_answer 作为 agent 的【终止动作】统一附加在所有 A 组上（O 组不走这条路）。
    它不是"能力差异"，故不参与 A1/A2/A3 的切片计数。
    """
    base = {"A1": TOOLS[0:1], "A2": TOOLS[0:2], "A3": TOOLS[0:3]}.get(group)
    return None if base is None else base + [SUBMIT_TOOL]

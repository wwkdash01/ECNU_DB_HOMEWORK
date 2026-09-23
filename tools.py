"""tools.py —— 模型唯一的"动手"入口

三件事：工具声明（发给 API 的说明书）、参数校验与执行、按组裁剪。

本仓库最终只保留三组（O1 / O3 / A1），故工具层只剩两个：

    run_sql        唯一的数据入口。写好的 SQL 必须先执行验证。
    submit_answer  agent 的【终止动作】——提交物必须能在库上真正执行。

关键设计（手册 §2.4，报告必须披露）：
    run_sql 成功时【只回行数 + 列名 + 3 行预览，不回完整结果】，但【形态必须给全】。
    列数与列名是模型自查"输出形态"的唯一依据，而输出形态自检正是 A1 相对 O1
    的增益来源（+5.0pt，McNemar p=0.0059）。

相对手册的加固（均为实测）：
    * submit_answer 让"交答案"变成一次必须可执行的工具调用。该模型不会主动停止
      请求工具（旧 A1 无此工具时 20/20 打满上限），且在工具被禁用后会把工具调用
      语法当纯文本输出，导致 45% 的 final_sql 无法执行。
    * run_sql 的报错【原样回灌】进 messages —— 这是 agent 闭环唯一的在线信号。
      注意它只能区分"执行成功/报错/几行"，无法判断语义对错（原则②：在线信号与
      离线判分严格分离）。
"""
import config
from db import execute, QueryTimeout

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

# 终止动作：模型用它提交最终答案并结束循环。
# 刻意【不放进 TOOLS】—— 它按组统一附加，不属于任何一组的"能力差异"，
# 否则"工具数"会把终止机制误记成能力增量。
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
    """按字符上限截断工具输出，避免一次工具结果吃光上下文。"""
    return s[:config.TOOL_OUTPUT_MAX_CHARS]


def shape_of(cols):
    """把列名列表格式化成给模型看的形态串。

    ★ 为什么必须回报形态（本仓库实测）：
      旧 A1 的 220 条 result_mismatch 中，107 条（48.6%）的错误结果【列数】与
      gold 不符，而模型此前只收到"返回 N 行"，完全不知道自己的结果有几列。
      典型错法：问 "who had the least" 却返回 (CustomerID, total) 两列——
      核心值对，但多带了"佐证列"；官方 EX 是 set(pred)==set(gold)，多一列即判错。
      让模型看见列数，它就能在不看 gold 的情况下自查"问题只问了 who，我给了 2 列"。
    """
    return f"{len(cols)} 列（{', '.join(str(c) for c in cols)}）"


def _run_sql(conn, sql):
    try:
        r = execute(conn, sql)          # execute 返回 cols
    except QueryTimeout as e:
        return _cut(f"执行失败：{e}"), None
    except Exception as e:
        return _cut(f"执行失败：{e}"), None     # 报错原样回灌，模型多半能自己修
    tail = "（结果被截断）" if r["truncated"] else ""
    # 刻意只给 3 行预览：完整结果不从这里出去，见模块 docstring。
    # 但【形态】必须给全 —— 列数与列名是模型唯一的自查依据。
    return _cut(f"执行成功，返回 {len(r['rows'])} 行，{shape_of(r['cols'])}{tail}。"
                f"预览：{r['rows'][:3]}"), r["qet"]


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
        r = execute(conn, sql)
    except Exception as e:
        return f"提交失败：该 SQL 无法执行（{e}）。请修正后重新提交。", None
    return (f"已收到最终答案：{_cut(sql)}\n"
            f"该答案的结果形态：{len(r['rows'])} 行，{shape_of(r['cols'])}。\n"
            f"请自行确认：这个列数与列名正是问题所要求的吗？若多带了佐证列，"
            f"请修正后重新提交。"), None


def dispatch(conn, db_id, name, args):
    """返回 (文本, qet)。未知工具名返回友好错误，而不是抛异常打断 agent 循环。"""
    if name == "run_sql":
        return _run_sql(conn, args.get("sql", ""))
    if name == "submit_answer":
        return _submit(conn, db_id, args.get("sql", ""))
    return f"未知工具：{name}", None


def tools_for(group):
    """按组裁剪工具。O 组不走工具路径，返回 None。

    A1 = run_sql + submit_answer。submit_answer 是 agent 的【终止动作】，
    统一附加在所有 A 组上，不是"能力差异"。
    """
    if group == "A1":
        return [TOOLS[0], SUBMIT_TOOL]
    return None

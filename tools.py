"""tools.py —— 模型唯一的"动手"入口

三件事：工具声明（发给 API 的说明书）、参数校验与执行、按组裁剪。

关键设计（手册 §2.4，报告必须披露）：
    run_sql 成功时【只回行数 + 3 行预览，不回完整结果】。
    这样 A1 拿到的仅是「验证信号」，想拿数据内容必须调 get_column_values，
    从而保证 A1−O3（验证）与 A2−A1（值域获取）两层差异是干净的。

★ A4（替代方案，非 A3 的延伸）：把 A2 的 get_column_values 换成 explore_schema。
    A1v2 剩余 181 条错误中「表选错」68 条（37.6%）居首，而 get_column_values
    解决的是"列里存了什么值"，与"该用哪些表"正交 —— 故按错误结构换掉工具。
    两组不构成包含链（A1⊂A4 不成立），报告里分开叙述。见 schema_link.py。

相对手册的两处加固：
    * get_column_values 加 ORDER BY —— 手册无 ORDER BY，SQLite 返回顺序
      取决于查询计划，同一题两次运行可能给出不同的取值样例，破坏可复现性。
    * _cut 按【条目】截断而不是把字符串硬切一半，避免把某个取值切成残缺片段。
"""
import config
from db import execute, QueryTimeout
from data import schema_whitelist, desc_index
from schema_link import explore_schema
from semantics import join_cardinality, declared_vs_actual   # A10 方向一

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

# 行数自检工具（A2 = A1 + 本工具）。
#
# ★ 存在理由（本仓库实测）：
#   run_sql 成功时只回"返回 N 行 + 3 行预览"。对 gold>3 行的 116 道题，
#   模型**看不到自己结果的行数偏差**：其实测 EX 仅 51.7%（整体 63.4%），
#   56 条错题里 10 条是"行数错"、19 条是"列数错" —— 两者都是【模型自己
#   能判断、但从未被要求判断】的东西（与已验证有效的"形态自检"同源）。
#
# ★ 刻意设计成"模型先声明、工具再核对"而不是"工具直接告诉它对不对"：
#   数据库里有正确答案，工具不能替模型做判断（手册原则②：在线信号与离线判定
#   严格分离）。工具只回"实际是什么"，对不对仍由模型对照自己的声明判断。
CHECK_TOOL = {"type": "function", "function": {
    "name": "check_result",
    "description": "在提交答案前核对结果的【形态】：先说出你认为这道题应该返回"
                   "多少行、哪些列，工具会执行你的 SQL 并回报实际的行数与列名，"
                   "把你声明的和实际的两者并排列出来，供你自己判断是否一致。"
                   "当你已经有一条能执行成功的 SQL、准备提交但不确定结果形态是否正确时使用。"
                   "注意：行数不是能够执行就算对 —— 少了行往往意味着 join 写成了"
                   "INNER JOIN 而丢了行，多了行往往意味着 join 产生了重复。",
    "parameters": {"type": "object",
        "properties": {
            "sql": {"type": "string", "description": "你已经写好的那条 SQL"},
            "expected_columns": {"type": "array", "items": {"type": "string"},
                                 "description": "你预期结果应当包含的列名（按问题要求，不多不少）"},
            "expected_rows": {"type": "integer",
                              "description": "你预期结果应有多少行；不确定就填 -1"}},
        "required": ["sql"]}}}

# 粒度契约工具（A5 = A1 + 本工具；A2 的第三版候选）。
#
# ★ 与前两版的区别（为什么这版可能成立）：
#   v1 check_result（行数自检）：模型报的行数就是它自己 SQL 的输出行数 —— 没有第三方
#      依据，实测 47 次调用 0 次发现不一致，净增益 0。**同义反复，已证伪。**
#   v2 本工具：验的是【声明的分组粒度】与【SQL 实际的 GROUP BY / 行重复模式】是否吻合。
#      这是数据库能机械判定、而模型无法从自己 SQL 反推的东西 ——
#      它的 SQL 可能压根没写 GROUP BY，但问题里写着 "monthly"。
#      实测依据：表选对但答案错的 62 题里，44 题【行数相同、值不同】，
#      相对误差 <1 倍占 21 题 —— 典型症状就是"该按月聚合却按单条算"这类粒度错。
#
# ★ 判定"哪几列是分组键"用的是【数据模式】而非解析 SQL：
#      一个分组键在各分组内的取值必须唯一；事实列（聚合结果）在分组内会变化。
#   这样即使模型用了 CTE/子查询/窗口函数，判定依然成立。
DECLARE_TOOL = {"type": "function", "function": {
    "name": "declare_shape",
    "description": "在提交前核对结果的【粒度契约】：先声明你认为这道题的结果应当"
                   "按什么维度分组（例如按月、按人、按课程），再让工具回报你这条 SQL "
                   "实际的分组结构 —— 实际的列、行数，以及数据本身显示哪几列是分组键、"
                   "哪几列是聚合值，并列出每组出现的次数。两者不一致就说明粒度错了，"
                   "需要改 SQL。典型错误：问题问 'monthly'（按月），SQL 却对每条记录"
                   "直接聚合，没写 GROUP BY。准备提交前调用它。",
    "parameters": {"type": "object",
        "properties": {
            "sql": {"type": "string", "description": "你已经写好的那条 SQL"},
            "group_by": {"type": "array", "items": {"type": "string"},
                         "description": "你声明的分组维度：结果【每一行】唯一标识的列。"
                                        "例如按月汇总就填 ['month']，按客户汇总就填 "
                                        "['CustomerID']。如果结果只有一行（整体汇总）就填 []"}},
        "required": ["sql"]}}}


# 值域存在性检查（A8 = A1 + 本工具；对应"第 2 层：拿数据库当语义参照"）。
#
# ★ 与已弃用的 get_column_values 的区别（这是它能成立的原因）：
#   get_column_values 把整列的值域【端给】模型 —— 等于把候选答案列表摆出来，
#     模型可以直接照抄，存在答案泄漏（实测 gold 字面量有 44% 会出现在样例里）。
#   本工具反过来：模型【先声明】它打算用的字面量，工具只回报"存在/不存在"+
#     该列的格式样例。模型必须先有自己的判断，工具只做裁定 —— 不提供候选答案。
CHECK_VALUES_TOOL = {"type": "function", "function": {
    "name": "check_values",
    "description": "在提交前核对你在 WHERE/HAVING 里写的【字面量】是否真的存在于该列中。"
                   "传入 表名、列名 和你写的字面量列表，工具会逐个回报该值在列中"
                   "是否存在，并给出该列真实的格式样例。"
                   "典型能抓到的错误：列存的是 '201208' 而你写成 '2012-08-05'；"
                   "把类别名记成 'SME' 而库里是 'Small and Medium Enterprise'。"
                   "这类错误执行不会报错、往往返回 0 行或错值，你自己看不出来。"
                   "只在你已经写了含字面量的过滤条件、准备提交前调用。",
    "parameters": {"type": "object",
        "properties": {
            "checks": {"type": "array",
                "description": "要核对的 (表, 列, 字面量) 列表",
                "items": {"type": "object",
                    "properties": {
                        "table": {"type": "string"},
                        "column": {"type": "string"},
                        "value": {"type": "string", "description": "你写的字面量（不含引号）"}},
                    "required": ["table", "column", "value"]}}},
        "required": ["checks"]}}}


# A4 组专用：replace-A2 的表连通工具。见 schema_link.py 的模块 docstring ——
# A1v2 剩余错误里表选错占 37.6%（漏表 40/多表 8/并存 17），根因是 DDL 里
# 看不到"两张表靠哪个共享列连起来"（yearmonth 没有任何 FK 指向 transactions_1k）。
EXPLORE_TOOL = {"type": "function", "function": {
    "name": "explore_schema",
    "description": "查找问题涉及哪些表、以及这些表能靠哪些列连起来（连接关系）。"
                   "当你【不确定该用哪些表】、题目跨多个业务概念、需要按日期/时间/状态等"
                   "维度过滤、或几张表看起来无法连接时，先调用它。它返回："
                   "(1) 与问题词语匹配的表和列（按相关度排序）；"
                   "(2) 每张表的行数规模，用来区分事实表与维度表；"
                   "(3) 各表之间共享的列名 —— 这是 SQLite 隐式连接的真实依据，"
                   "外键声明往往缺失，DDL 里看不出来；"
                   "(4) 桥接表：与候选表共享主键/外键列、但表名没在问题里出现的表。"
                   "漏掉这类桥接表是常见的错误来源。"
                   "多表题尤其要先调用它。",
    "parameters": {"type": "object",
        "properties": {
            "keywords": {"type": "array", "items": {"type": "string"},
                         "description": "问题里的业务名词，一次给 2~6 个。"
                                        "例如 [\"gas station\", \"transaction\", \"June 2013\"]"},
            "tables": {"type": "array", "items": {"type": "string"},
                       "description": "你已确定会用到的表名，可留空。"
                                      "给出后工具会列出它们的列、规模与可达的其他表"}},
        "required": []}}}

# ---------------------------------------------------------------- A10 方向一
# 硬门：任何 run_sql 之前必须先提交"题目解释承诺"（六要素）。
# 与 A5 的 declare_shape 的关键区别（两条，都有实测依据）：
#   1) 时机：A5 是写完 SQL 之后再校验形态 —— 那时模型已经和自己的 SQL 绑定了，
#      实测 49/49 次拒绝修改。A10 把承诺提前到【任何执行之前】，此时还没有
#      可辩护的产出，模型没有锚点。
#   2) 强度：A5 是顾问式工具（报告出来，模型可以选择不理）。A10 是硬门 ——
#      没承诺就【拒绝执行 run_sql】，模型想跑数据就必须先把自己的解释写下来。
INTENT_TOOL = {"type": "function", "function": {
    "name": "declare_intent",
    "description": "【强制】在用 run_sql 查任何数据之前，必须先用本工具写下你对题目的"
                   "理解（六要素）。没有提交本承诺时，run_sql 会被直接拒绝执行。"
                   "write down 之后工具会复述你的承诺，后续每条 SQL 的执行结果里"
                   "都会附带「你的承诺 vs 数据库真值」的对质。",
    "parameters": {"type": "object",
        "properties": {
            "entities": {"type": "string",
                         "description": "问题问的是【什么实体】（行/记录的主体），"
                                        "如 'customer'、'gas station'"},
            "filters": {"type": "string",
                        "description": "过滤条件，逐条列出，如 'Country = CZE 且 Segment = Discount'"},
            "dimensions": {"type": "string",
                           "description": "分组/分类所依据的维度列，如 'Segment'"},
            "measures": {"type": "string",
                         "description": "被计算的度量，如 'COUNT(*)'、'SUM(Consumption)'"},
            "group_by": {"type": "array", "items": {"type": "string"},
                         "description": "GROUP BY 的列名列表；不需要分组时给空数组 []"},
            "row_grain": {"type": "string",
                          "enum": ["row", "per_entity", "aggregate", "unknown"],
                          "description": "结果粒度：row=每行一条记录；per_entity=每个实体一行；"
                                         "aggregate=单个聚合值（1 行 1 列）"}},
        "required": ["entities", "row_grain"]}}}

def _declare_intent(state, args):
    """记录承诺并复述。返回给模型的是它【自己刚写下的话】——这是后面证伪的基准。

    ★ 复述必须逐字，不能改写成我们的措辞：证伪的依据要是模型自己的承诺，
      否则它可以说"我没这么讲过"。
    """
    ents = (args.get("entities") or "").strip()
    grain = (args.get("row_grain") or "").strip()
    if not ents or not grain:
        return ("拒绝：entities 与 row_grain 为必填。你必须先说清问题问的是什么实体、"
                "结果是什么粒度，才能开始查数据。"), None
    state["intent"] = {
        "entities": ents,
        "filters": (args.get("filters") or "").strip(),
        "dimensions": (args.get("dimensions") or "").strip(),
        "measures": (args.get("measures") or "").strip(),
        "group_by": [str(g).strip() for g in (args.get("group_by") or []) if str(g).strip()],
        "row_grain": grain,
    }
    it = state["intent"]
    lines = [f"  - 实体：{it['entities']}",
             f"  - 过滤：{it['filters'] or '(未填写)'}",
             f"  - 维度：{it['dimensions'] or '(未填写)'}",
             f"  - 度量：{it['measures'] or '(未填写)'}",
             f"  - GROUP BY：{it['group_by'] or '(无分组)'}",
             f"  - 结果粒度：{it['row_grain']}"]
    return ("已记录你的题目解释承诺，后续每条 SQL 的执行结果都会附上它与数据库真值的对质：\n"
            + "\n".join(lines)
            + "\n现在可以调用 run_sql 了。"), None


def _run_sql_gated(conn, db_id, sql, state):
    """A10 专用 run_sql：硬门 + 语义对质。

    硬门：没有 declare_intent 就拒绝执行。错误文本本身必须是【可行动的】
    （告诉模型下一步做什么），否则模型会卡在原地重试同一条 SQL。
    """
    if not state.get("intent"):
        return ("拒绝执行：你还没有调用 declare_intent。"
                "在查询任何数据之前，你必须先写下对题目的理解（六要素）——"
                "这是为了让你在执行前把解释固定下来。请先调用 declare_intent。"), None
    txt, qet = _run_sql(conn, sql)
    extra = []
    if not txt.startswith("执行失败"):
        try:
            rep = join_cardinality(conn, sql)
            if rep:
                extra.append(rep)
        except Exception:
            pass
        try:
            ds = declared_vs_actual(conn, sql, state["intent"])
            if ds:
                extra.append("[与你承诺的对质]\n  - " + "\n  - ".join(ds))
        except Exception:
            pass
    if extra:
        txt = txt + "\n\n" + "\n\n".join(extra)
    return _cut(txt), qet


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


def report_shape(conn, sql, max_rows=20):
    """（独立执行版，保留给需要单独跑一次的场景）见 _run_sql 内的 shape_of 用法。"""
    cur = conn.execute(sql)
    cols = [d[0] for d in (cur.description or [])]
    rows = cur.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    return cols, rows[:max_rows], truncated


def shape_of(cols):
    """把列名列表格式化成给模型看的形态串。

    ★ 为什么必须回报形态（本仓库实测）：
      A1 的 220 条 result_mismatch 中，45% 的错误结果【列数】与 gold 不符
      （99/220），而模型此前只收到"返回 N 行"，完全不知道自己的结果有几列。
      典型错法：问 "who had the least" 却返回 (CustomerID, total) 两列——
      核心值对，但多带了"佐证列"；官方 EX 是 set(pred)==set(gold)，多一列即判错。
      让模型看见列数，它就能在不看 gold 的情况下自查"问题只问了 who，我给了 2 列"。
    """
    return f"{len(cols)} 列（{', '.join(str(c) for c in cols)}）"


def _run_sql(conn, sql):
    try:
        r = execute(conn, sql)          # execute 现已返回 cols
    except QueryTimeout as e:
        return _cut(f"执行失败：{e}"), None
    except Exception as e:
        return _cut(f"执行失败：{e}"), None     # 报错原样回灌，模型多半能自己修
    tail = "（结果被截断）" if r["truncated"] else ""
    # 刻意只给 3 行预览：完整结果不从这里出去，见模块 docstring。
    # 但【形态】必须给全 —— 列数与列名是模型唯一的自查依据。
    return _cut(f"执行成功，返回 {len(r['rows'])} 行，{shape_of(r['cols'])}{tail}。"
                f"预览：{r['rows'][:3]}"), r["qet"]


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
        r = execute(conn, sql)
    except Exception as e:
        return f"提交失败：该 SQL 无法执行（{e}）。请修正后重新提交。", None
    return (f"已收到最终答案：{_cut(sql)}\n"
            f"该答案的结果形态：{len(r['rows'])} 行，{shape_of(r['cols'])}。\n"
            f"请自行确认：这个列数与列名正是问题所要求的吗？若多带了佐证列，"
            f"请修正后重新提交。"), None


def _grain(cols, rows):
    """从数据模式判定【极小组件键】：能唯一决定每一行的最小列集合。

    ★ 第一版判据是错的（实测踩到）：原先按"其余列构成分组、该列组内唯一"逐列判定，
      结果把聚合列 SUM(Consumption) 也判成分组键 —— 因为"其余列=Date"确实唯一
      决定了它。判据太弱，会把事实列误报为分组键，模型据此反而被误导。

    改为标准的【极小键】定义：
      列集 K 是键  <=>  按 K 投影后没有重复行
      极小        <=>  K 的任何真子集都不是键
    这样 SUM(Consumption) 单独不是键（它不唯一决定 Date），只有 Date 是。
    枚举顺序按 |K| 从 1 递增，保证拿到的是极小的那个。
    不解析 SQL，故 CTE / 子查询 / 窗口函数一视同仁。
    """
    if not rows or not cols:
        return set()
    from itertools import combinations
    idx = {c: i for i, c in enumerate(cols)}

    def is_key(sub):
        seen = set()
        for r in rows:
            t = tuple(r[idx[c]] for c in sub)
            if t in seen:
                return False
            seen.add(t)
        return True

    n = len(cols)
    for size in range(1, n + 1):
        best = None
        for sub in combinations(cols, size):
            if is_key(sub):
                # 极小性：真子集必须都不是键
                if any(is_key(s2) for k in range(1, size)
                       for s2 in combinations(sub, k)):
                    continue
                best = set(sub)
                break
        if best is not None:
            return best
    return set(cols)


def _declare_shape(conn, sql, group_by):
    """执行 sql，回报实际粒度结构，并与模型的声明并排比对。

    只回报"实际是什么"，对不对由模型对照自己的声明判断（手册原则②）。
    """
    sql = (sql or "").strip().rstrip(";")
    if not sql:
        return "错误：sql 参数为空", None
    from llm import _QUERY_RE
    if not _QUERY_RE.match(sql):
        return f"错误：不是一条查询语句。收到：{sql[:120]}", None
    try:
        r = execute(conn, sql)
    except QueryTimeout as e:
        return f"执行失败：{e}", None
    except Exception as e:
        return f"执行失败：{e}", None

    cols = [str(c) for c in r["cols"]]
    rows = r["rows"]
    declared = [str(c) for c in (group_by or [])]
    lines = [f"实际结果：{len(rows)} 行，{len(cols)} 列（{', '.join(cols)}）"]

    if not rows:
        lines.append("结果为空 —— 无法判定分组结构。先确认过滤条件是否写错。")
        return _cut("\n".join(lines)), r["qet"]

    keys = _grain(cols, rows)

    def _distinct(c):
        return len({r[cols.index(c)] for r in rows})

    # ★ 防御（实测踩到）：键若落在"度量列"上，多半是【漏了 GROUP BY】的假象。
    #   例：SELECT CustomerID, Consumption FROM yearmonth —— Consumption 数值恰好
    #   各不相同，于是被当成键，而模型真正该做的是按月聚合。此时必须报警，
    #   否则工具会把模型引到更错的方向。
    def _looks_like_measure(c):
        cl = c.lower()
        if cl in ("date", "time", "year", "month", "day"):
            return False
        if cl.endswith("_id") or cl == "id" or cl.startswith("link_to"):
            return False
        # 数值型且不同值数接近行数 -> 更像度量而非分组键
        vals = [r[cols.index(c)] for r in rows]
        if all(isinstance(v, (int, float)) for v in vals):
            return True
        return False

    if len(cols) == 1:
        lines.append("只有一列，看不出分组结构。")
    elif keys:
        lines.append(f"数据模式显示【分组键】：{', '.join(sorted(keys, key=cols.index))}"
                     f"（这些列的组合在结果里唯一确定每一行）")
        bad = [c for c in keys if _looks_like_measure(c)]
        if bad and len(keys) == len(cols):
            lines.append(f"⚠ 但分组键落在数值列 {bad} 上、且没有任何列被当作聚合值 —— "
                         f"这通常是【漏写 GROUP BY】的假象（数值恰好各不相同而已）。"
                         f"如果你的问题要求按某个维度汇总，请补上 GROUP BY。")
        elif bad:
            lines.append(f"⚠ 分组键里含数值列 {bad}，请确认它真的是维度而不是被聚合的度量。")
    else:
        lines.append("数据模式显示【没有稳定的分组键】：没有任何列能唯一确定行 —— "
                     "说明 join 可能产生了重复行（同一实体出现在多行）。")
    if len(keys) < len(cols):
        lines.append(f"列表中没有出现在分组键里的："
                     f"{', '.join(c for c in cols if c not in keys)}"
                     f"（这些当作聚合值处理）")

    # 分组重复度：直接暴露 fan-out
    if keys:
        cnt = {}
        order = [c for c in cols if c in keys]
        for row in rows:
            k = tuple(row[cols.index(c)] for c in order)
            cnt[k] = cnt.get(k, 0) + 1
        dup = sum(1 for v in cnt.values() if v > 1)
        lines.append(f"按分组键去重后 {len(cnt)} 组；出现重复行的组 {dup} 个"
                     + ("（>0 说明有 join 放大）" if dup else ""))

    # 并排比对声明与实际
    if declared:
        d = {x.strip().lower() for x in declared if x and x.strip()}
        actual = {c.lower() for c in (keys if len(cols) > 1 else cols)}
        if not d:
            lines.append("你声明的是整体汇总（只有一行）。"
                         + ("实际也无可辨识分组键。" if not keys else
                            f"但实际数据有分组键：{', '.join(sorted(keys))} —— 形状不一致。"))
        elif d <= actual:
            lines.append(f"你声明的分组维度 {sorted(d)} 与实际分组结构一致。")
        else:
            lines.append(f"你声明的分组维度 {sorted(d)}，但实际数据里 "
                         f"{sorted(d - actual)} 并不构成分组键 —— 粒度与声明不符，"
                         f"检查是否漏了 GROUP BY 或分组键写错。")
    else:
        lines.append("（你没有声明 group_by，故只回报实际分组结构）")

    lines.append("请自行判断：这个粒度正是问题要求的吗（按月？按人？整体汇总？）？"
                 "不一致就改 SQL 后重新核对或提交。")
    # 给一个小样本，便于模型核对聚合值是否合理
    lines.append(f"预览（前 {min(3, len(rows))} 行）：{rows[:3]}")
    return _cut("\n".join(lines)), r["qet"]


def _check_values(conn, db_id, checks):
    """逐个裁定"模型声明的字面量是否真的存在于该列"，并给出该列真实格式样例。

    ★ 只回报"存在/不存在"+ 格式样例，【不列全部取值】—— 避免变成候选答案清单
      （get_column_values 就是因为端出全值域而弃用，实测 gold 字面量 44% 会撞上）。
    ★ 不受 LIKE 影响：只查精确相等，避免把 'SJS' 判成存在于 'SJS susp'。
    """
    wl = schema_whitelist(db_id)
    out, n_ok, n_bad = [], 0, 0
    for ck in (checks or [])[:12]:
        if not isinstance(ck, dict):
            continue
        t = str(ck.get("table", "")).strip()
        col = str(ck.get("column", "")).strip()
        val = ck.get("value", "")
        real = next((x for x in wl if x.lower() == t.lower()), None)
        if real is None:
            out.append(f'  {t}.{col} = {val!r}: 表不存在')
            continue
        rcol = next((x for x in wl[real] if x.lower() == col.lower()), None)
        if rcol is None:
            out.append(f'  {t}.{col} = {val!r}: 列不存在（该表有: '
                       f'{", ".join(sorted(wl[real], key=str.lower))}）')
            continue
        try:
            hit = conn.execute(
                f'SELECT 1 FROM "{real}" WHERE "{rcol}" = ? LIMIT 1', (val,)).fetchone()
            samples = [r[0] for r in conn.execute(
                f'SELECT DISTINCT "{rcol}" FROM "{real}" WHERE "{rcol}" IS NOT NULL '
                f'ORDER BY "{rcol}" LIMIT 4').fetchall()]
        except Exception as e:
            out.append(f'  {t}.{col} = {val!r}: 查询失败（{e}）')
            continue
        if hit:
            n_ok += 1
            out.append(f'  {real}.{rcol} = {val!r}: ✅ 存在')
        else:
            n_bad += 1
            tail = (f"  该列真实取值如: {[str(s)[:24] for s in samples]}" if samples else "")
            out.append(f'  {real}.{rcol} = {val!r}: ❌ 不存在（可能写错/格式不符）。{tail}')
    if not out:
        return _cut("没有可核对的字面量（checks 为空或格式不对）"), None
    head = f"字面量核对：{n_ok} 个存在，{n_bad} 个不存在"
    if n_bad:
        head += "  ← 含不存在的值，若不修正会返回 0 行或错值"
    out.append(head + "。请据此修正后再提交。")
    return _cut("\n".join(out)), None


def _check_result(conn, sql, exp_cols, exp_rows):
    """执行 sql，回报实际形态，并与模型的声明并排比对。

    只回"实际是什么"，不回"对不对" —— 判定权留给模型（手册原则②：
    在线信号与离线判定严格分离，数据库里有正确答案，工具不能替模型判断）。
    """
    sql = (sql or "").strip().rstrip(";")
    if not sql:
        return "错误：sql 参数为空，请给出你要核对的查询", None
    from llm import _QUERY_RE
    if not _QUERY_RE.match(sql):
        return f"错误：不是一条查询语句。收到：{sql[:120]}", None
    try:
        r = execute(conn, sql)
    except QueryTimeout as e:
        return f"执行失败：{e}", None
    except Exception as e:
        return f"执行失败：{e}", None

    got_cols = [str(c) for c in r["cols"]]
    got_rows = len(r["rows"])
    lines = [f"实际结果：{got_rows} 行，{len(got_cols)} 列（{', '.join(got_cols)}）"]
    lines.append(f"预览（前 {min(3, got_rows)} 行）：{r['rows'][:3]}")

    if exp_cols:
        want = [str(c) for c in exp_cols]
        g = {c.lower() for c in got_cols}
        w = {c.lower() for c in want}
        extra, missing = sorted(g - w), sorted(w - g)
        cmp_txt = f"你声明的列：{', '.join(want)}\n"
        if not extra and not missing:
            cmp_txt += "→ 列名与声明完全一致。"
        else:
            if extra:
                cmp_txt += f"→ 你声明之外的列：{', '.join(extra)}"
            if missing:
                cmp_txt += f"\n→ 你声明了但结果里没有的列：{', '.join(missing)}"
        lines.append(cmp_txt)
    else:
        lines.append("（你没有声明 expected_columns，故无从比对列名）")

    if exp_rows is not None and int(exp_rows) >= 0:
        exp_rows = int(exp_rows)
        if got_rows == exp_rows:
            lines.append(f"→ 行数与声明一致（{got_rows}）。")
        else:
            direction = "多于" if got_rows > exp_rows else "少于"
            lines.append(f"→ 实际 {got_rows} 行，{direction}你声明的 {exp_rows} 行。"
                         f"若少了：检查是不是用了 INNER JOIN 把行丢了；"
                         f"若多了：检查 join 是否产生了重复行。")
    else:
        lines.append("（你没有声明 expected_rows，故只回报实际行数）")

    lines.append("请自行判断：这与问题要求一致吗？不一致就改 SQL 后重新核对或提交。")
    return _cut("\n".join(lines)), r["qet"]


def _explore(conn, db_id, args):
    """注意：【不】把 conn 传进 explore_schema —— 它自己按需开只读连接并做缓存。"""
    kw = args.get("keywords") or []
    tb = args.get("tables") or []
    if isinstance(kw, str):
        kw = [kw]
    if isinstance(tb, str):
        tb = [tb]
    return _cut(explore_schema(db_id, keywords=kw, tables=tb)), None


def dispatch(conn, db_id, name, args, state=None):
    """返回 (文本, qet)。未知工具名返回友好错误，而不是抛异常打断 agent 循环。

    `state` 是 A10 的每题可变状态（当前只有 {"intent": {...}}）。默认 None 时
    按 A1~A9 的原有语义走 —— 这样 gate2_check 等旧调用点不必改。
    """
    if name == "declare_intent":
        return _declare_intent(state if state is not None else {}, args)
    if name == "run_sql":
        if state is not None:
            return _run_sql_gated(conn, db_id, args.get("sql", ""), state)
        return _run_sql(conn, args.get("sql", ""))
    if name == "get_column_values":
        return _col_values(conn, db_id, args.get("table", ""), args.get("column", ""))
    if name == "get_column_desc":
        return _col_desc(conn, db_id, args.get("table", ""), args.get("column", ""))
    if name == "check_values":
        return _check_values(conn, db_id, args.get("checks"))
    if name == "declare_shape":
        return _declare_shape(conn, args.get("sql", ""), args.get("group_by"))
    if name == "check_result":
        return _check_result(conn, args.get("sql", ""),
                             args.get("expected_columns"), args.get("expected_rows"))
    if name == "explore_schema":
        return _explore(conn, db_id, args)
    if name == "submit_answer":
        return _submit(conn, db_id, args.get("sql", ""))
    return f"未知工具：{name}", None


def tools_for(group):
    """按组裁剪工具。必须按顺序切片，保证 A1 ⊂ A2 ⊂ A3，差值才有意义。

    ★ A4 是【替代方案】而非 A3 的延伸，它**打破** A1⊂A2⊂A3 这条链：
        A4 = run_sql + explore_schema + submit_answer
      即 A1v2 基线上【增加】explore_schema。这样 A4 − A1v2 的差值只有一个自变量：
      "把选表从'凭 DDL 猜'换成'先查证再写'"。
      之所以不与 A2/A3 构成包含链，是因为错误结构表明瓶颈在选表（37.6%）而非取值，
      get_column_values 作用正交，故弃用而非叠加。报告里必须分开叙述两族对照。

    submit_answer 作为 agent 的【终止动作】统一附加在所有 A 组上（O 组不走这条路）。
    它不是"能力差异"，故不参与各组的切片计数。
    """
    if group in ("A6", "A7", "A9"):
        # A6/A7：结构化 CoT（prompt 里手写推理步骤）；A9：原生思考模式。
        # 三者【工具与 A1 完全相同】，只换推理机制。
        # 这样 A6−A1 / A7−A1 / A9−A1 的单一自变量就是"推理机制"，不含任何工具差异。
        return [TOOLS[0], SUBMIT_TOOL]
    if group == "A10":
        # 方向一：declare_intent 硬门 + run_sql 语义对质。
        # 相对 A1 的单一自变量 = "执行前必须先固定题目解释，且解释会被库内真值对质"。
        # 顺序即语义：declare_intent 在前（硬门要求），run_sql 仍是唯一的数据入口。
        return [INTENT_TOOL, TOOLS[0], SUBMIT_TOOL]
    if group == "A8":
        # A1 基线上【只加】check_values（值域存在性检查）
        return [TOOLS[0], CHECK_VALUES_TOOL, SUBMIT_TOOL]
    if group == "A5":
        # A1 基线上【只加】declare_shape（粒度契约），用于 pilot 验证
        return [TOOLS[0], DECLARE_TOOL, SUBMIT_TOOL]
    if group == "A2":
        # A1 基线上【只加】check_result（A1 ⊂ A2 成立）。原 get_column_values 方案已弃用。
        return [TOOLS[0], CHECK_TOOL, SUBMIT_TOOL]
    if group == "A4":
        # 顺序即 tools_for 的语义：run_sql 仍是验证手段，explore_schema 是新增能力
        return [TOOLS[0], EXPLORE_TOOL, SUBMIT_TOOL]
    base = {"A1": TOOLS[0:1], "A2": TOOLS[0:2], "A3": TOOLS[0:3]}.get(group)
    return None if base is None else base + [SUBMIT_TOOL]

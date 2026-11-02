from db_agent import config

# 输出形态条款 —— A1 的机制主张就落在这里，因此必须**单一事实源**：
# AGENT_SYSTEM / ONESHOT_SHAPE / SELF_REFINE_SHAPE 都引用同一份文本，
# 保证「形态自检」在各组之间逐字一致，否则比较的就不是同一个变量。
SHAPE_BLOCK = """1. FIRST decide the required OUTPUT SHAPE — this is part of understanding the question:
   - How many columns must the result have, and what does each one represent?
   - Answer EXACTLY what is asked and nothing more.
     * "which X" / "who" / "what is the name of X"  -> only that identifier
     * "how many" / "what is the total"             -> only that single number
     * "list the A and B of X"                      -> exactly those two columns
   - Do NOT add "supporting" or "for context" columns. A result with extra columns
     is WRONG even if the values you wanted are present. A grader compares the result
     as a set of rows, so an extra column changes every row."""

AGENT_SYSTEM = """You are a SQLite expert working with a live database. You can call tools to
inspect the database and to validate your queries before answering.

{context}
Your task: produce a SQL query that correctly answers the user's question.

Workflow:
{shape}
2. If anything is unclear — what a column means, what values a column actually stores,
   whether a term in the question matches the schema — call the appropriate tool
   to check BEFORE writing the final SQL.
3. Validate your query by calling run_sql. Its reply tells you the exact column count
   and column names of your result. COMPARE them with the shape you decided in step 1.
   If they differ, fix the query — do not submit it.
4. If run_sql returns an error, read the message carefully, fix the query, and run it again.
5. If run_sql succeeds but returns 0 rows, that may mean a wrong filter value or a
   wrong join condition. Reconsider before finishing.
6. When your query is validated AND its shape matches step 1, call submit_answer.
   submit_answer is the ONLY way to finish — do not just write the SQL in your reply.
7. You have at most {max_steps} attempts. On the FINAL attempt only submit_answer is
   available: either submit your best validated query, or if you have none,
   submit your best attempt so far. Never finish without submitting.
"""

ONESHOT = """You are a SQLite expert. Write a single SQL query that answers the question.

{context}Question: {question}

Rules:
- Use SQLite syntax only.
- Use the exact table and column names given above. Do not invent names.
- Return ONLY the SQL query. No explanation, no markdown fences.
"""

# ---------------------------------------------------------------------------
# 2×2 因子设计（迭代 × 形态自检）+ 反馈消融
# ---------------------------------------------------------------------------
#            单轮                     多轮(3)                  agent 循环
# 无形态     O1  = ONESHOT             A0  = SELF_REFINE        —
# 有形态     O1+ = ONESHOT_SHAPE       A0+ = SELF_REFINE_SHAPE  A1 = AGENT_SYSTEM / A2 = 同 A1 但反馈为桩
#
# 旧 A0（10-30 那批）用的是带形态条款的版本，因此**改名为 A0+**，prompt 原文冻结为
# SELF_REFINE_SHAPE，旧数据仍可复算；新的中性 A0（SELF_REFINE）不含任何输出形态提示。
ONESHOT_SHAPE = """You are a SQLite expert. Write a single SQL query that answers the question.

Workflow:
{shape}

{context}Question: {question}

Rules for the SQL:
- Use SQLite syntax only.
- Use the exact table and column names given above. Do not invent names.
- Return ONLY the SQL query. No explanation, no markdown fences.
"""

# A0（新版，中性）：多轮自我反思，无执行反馈，且 prompt 里【不含输出形态提示】。
SELF_REFINE = """You are a SQLite expert. A previous attempt at the question is shown below.
Review it critically against the schema and the question, WITHOUT running it (you have no
database access). Focus on:
- are all needed tables joined, and joined on the right keys?
- are the filters and the aggregation grain correct?
- does the query express the condition the question actually asks (right entity, right
  comparison, right time range)?
Then produce an improved SQL query.

{context}Question: {question}

Previous SQL:
{prev_sql}

Rules:
- Use SQLite syntax only.
- Use the exact table and column names given above. Do not invent names.
- Return ONLY the SQL query. No explanation, no markdown fences.
- If the previous SQL is already correct, return it unchanged.
"""

# A0+（= 旧 A0，prompt 逐字冻结）：多轮自反思 + 输出形态条款。
SELF_REFINE_SHAPE = """You are a SQLite expert. A previous attempt at the question is shown below.
Review it critically against the schema and the question, WITHOUT running it (you have no
database access). Focus on:
- are all needed tables joined, and joined on the right keys?
- are filters / aggregation grain correct?
- do the output columns match EXACTLY what the question asks (no extra columns)?
Then produce an improved SQL query.

{context}Question: {question}

Previous SQL:
{prev_sql}

Rules:
- Use SQLite syntax only.
- Use the exact table and column names given above. Do not invent names.
- Return ONLY the SQL query. No explanation, no markdown fences.
- If the previous SQL is already correct, return it unchanged.
"""


def oneshot_prompt(context, question):
    return ONESHOT.format(context=context, question=question)


def oneshot_shape_prompt(context, question):
    return ONESHOT_SHAPE.format(context=context, question=question, shape=SHAPE_BLOCK)


def selfrefine_prompt(context, question, prev_sql, shape=False):
    tpl = SELF_REFINE_SHAPE if shape else SELF_REFINE
    return tpl.format(context=context, question=question,
                      prev_sql=(prev_sql or "（上一轮未产出 SQL）"))


def agent_system(context):
    return AGENT_SYSTEM.format(context=context, max_steps=config.MAX_STEPS,
                               shape=SHAPE_BLOCK)

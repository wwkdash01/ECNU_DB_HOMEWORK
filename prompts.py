"""prompts.py —— O 与 A 的公共起点

原则①（手册 §1.3）：O 和 A 必须共享同一个 build_context() 产出，逐字相同。
本文件是这条原则的唯一落点——两个模板都以 {context} 原样嵌入，
差异【只】在 §1.2 那张表里：温度、工具、是否循环。
"""
import config

AGENT_SYSTEM = """You are a SQLite expert working with a live database. You can call tools to
inspect the database and to validate your queries before answering.

{context}
Your task: produce a SQL query that correctly answers the user's question.

Workflow:
1. FIRST decide the required OUTPUT SHAPE — this is part of understanding the question:
   - How many columns must the result have, and what does each one represent?
   - Answer EXACTLY what is asked and nothing more.
     * "which X" / "who" / "what is the name of X"  -> only that identifier
     * "how many" / "what is the total"             -> only that single number
     * "list the A and B of X"                      -> exactly those two columns
   - Do NOT add "supporting" or "for context" columns. A result with extra columns
     is WRONG even if the values you wanted are present. A grader compares the result
     as a set of rows, so an extra column changes every row.
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


def oneshot_prompt(context, question):
    return ONESHOT.format(context=context, question=question)


def agent_system(context):
    return AGENT_SYSTEM.format(context=context, max_steps=config.MAX_STEPS)

"""prompts.py —— O 与 A 的公共起点

原则①（手册 §1.3）：O 和 A 必须共享同一个 build_context() 产出，逐字相同。
本文件是这条原则的唯一落点——两个模板都以 {context} 原样嵌入，
差异【只】在实验设计那张表里：温度、工具、是否循环。

本仓库最终只保留三组：

    O1  one-shot ×1，T=0.0，无工具                       —— 基准
    O3  one-shot 采样 ×3，T=0.7，按执行结果聚类          —— 采样投票值不值
    A1  agent 循环 + 输出形态自检，T=0.0，run_sql        —— 回灌迭代的净贡献

★ A1 ≡ 历史文档里的 "A1v2"（旧 A1 无形态自检，已随其余对照组一并移除）。
  AGENT_SYSTEM 第 1 步的【输出形态自检】是 A1 唯一的机制改进，也是全项目唯一
  统计显著的增益来源：EX 58.40% → 63.40%，配 McNemar 精确检验 p = 0.0059。
  因此本模板【基线冻结】—— 任何措辞改动都会毁掉与归档结果的可比性。
"""
from db_agent import config

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

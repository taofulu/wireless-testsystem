# 参数化用例进化：血缘落库，追溯链升五环

批量用例通过种子用例的参数槽位赋值生成（`POST /text-cases/{id}/evolve`），继承种子的结构化步骤与映射，不重新走 LLM 映射。用例为单实体，以 `origin enum[seed|evolved]` + `parent_case_id` + `variable_slots jsonb` 区分血缘；追溯链从四环扩展为五环（种子→进化→结构化→代码→结果）。

**Considered Options**: LLM 自由变异生成新文本用例——幻觉风险与 ADR-0002"禁止猜测"原则冲突，且每例都付出 LLM 映射成本；留待二期评估。

**Consequences**: 进化操作零 LLM 成本、确定性可复现；种子质量成为全系统的质量杠杆，上游的种子打磨流程（术语库+grill 协同）是关键依赖；槽位标注错误的种子会批量传染，种子的"认证"需谨慎。

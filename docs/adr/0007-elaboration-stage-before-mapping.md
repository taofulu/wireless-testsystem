# 扩写环节前置：用例质量内建于管线

文本用例质量是当前主要矛盾——隐含的专家知识写不清晰。管线在 `draft` 与 `mapped` 之间插入 `elaborating` 态：GLM 对文本用例做充分性评估，输出缺失点与针对性问题，前端以问答表单呈现，用户逐条回答后合并回原文；可迭代多轮，允许强制跳过（接受映射质量风险）。

**Considered Options**: 直接映射低质量输入——垃圾进垃圾出，下游确认态负担爆炸；完全依赖人工打磨——种子用例流程（术语库+grill）只能覆盖少量基准，覆盖不了日常新增用例。

**Consequences**: 用例状态机扩展为 `draft → elaborating → mapped → confirmed → generated → queued → running → done`；扩写与映射复用同一 CLI 子进程接入（ADR-0006），仅加载的 skill 不同。

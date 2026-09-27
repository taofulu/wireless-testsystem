# Spec: Wireless Test System MVP

## Problem Statement

无线测试工程师用自然语言编写文本用例（预知条件/测试步骤/预期结果），但 testbed（LASS + MBB + BBU/UE/仪表）只能执行 pytest 代码。手工把文本翻译成代码耗时、易错、不可追溯；而 LLM 直接生成代码又有向真实射频硬件下发幻觉指令的风险。

testbed 环境存在三种复杂场景：已有环境可直接执行、需新建环境、或需修改已有环境。环境中台（LASS/MBB）承担"规建维优"全生命周期职责，用例转化系统必须与这一业务全景对齐，把环境差异作为执行前置条件纳入工作流。

## Solution

一个 Web 系统：工程师三段分栏录入文本用例 → 本地 GLM 5.2（agent 模式）将其映射为结构化步骤（动作-参数-断言，对齐操作目录）→ 确认态人工审核（未映射步骤高亮、手选兜底，禁止猜测）→ 模板渲染为 AW pytest 代码 → 声明所需拓扑 → **LASS 环境校验，返回三值结果（已满足/需新建/需修改）** → 依结果分支：已满足则直接入队执行；需新建/需修改则阻断并附带差异说明，由环境中台处理 → testbed 侧拉取式 Worker 执行 → Allure 步骤级报告映射回原始文本步骤。原始文本→结构化步骤→可执行用例→执行结果四环全落库，双向可溯。

## User Stories

1. 作为测试工程师，我想分三栏录入预知条件、测试步骤、预期结果，以便用自然语言描述用例而无需写代码。
2. 作为测试工程师，我想保存文本用例草稿并稍后继续编辑，以便跨时段完成用例编写。
3. 作为测试工程师，我想一键触发 LLM 映射得到结构化步骤，以便快速进入审核环节。
4. 作为测试工程师，我想看到每个步骤映射到的 AW 操作及参数，以便核对系统理解是否正确。
5. 作为测试工程师，我想未映射步骤被显著高亮，以便集中处理系统不认识的操作。
6. 作为测试工程师，我想从操作目录下拉框为未映射步骤手动选择操作，以便不依赖 LLM 猜测。
7. 作为测试工程师，我想在确认态编辑任意步骤的操作与参数，以便修正 LLM 的映射错误。
8. 作为测试工程师，我想仅当全部步骤均已映射时才能生成可执行用例，以便保证下发代码完整可追溯。
9. 作为测试工程师，我想查看生成的 pytest 代码全文，以便必要时做最终人工审查。
10. 作为测试工程师，我想修改文本后重新触发映射，以便迭代打磨用例。
11. 作为测试工程师，我想每次生成产生新的代码版本记录，以便回溯任意历史版本。
12. 作为测试工程师，我想在用例中声明所需拓扑（BBU/UE/仪表组合），以便系统理解执行前提。
13. 作为测试工程师，我想提交执行时系统自动调用 LASS 校验拓扑可用性，以便避免无效排队。
14. 作为测试工程师，我想拓扑不可用时得到明确原因提示，以便调整拓扑或改期。
15. 作为测试工程师，我想环境校验返回"已满足/需新建/需修改"三种结论，以便明确下一步动作。
16. 作为测试工程师，我想环境校验结果为"需新建"时看到缺失资源的明确清单，以便提交环境中台建设。
17. 作为测试工程师，我想环境校验结果为"需修改"时看到差异说明，以便提交环境中台维护优化。
18. 作为测试工程师，我想环境中台完成环境处理后再次触发校验，以便闭环推进。
19. 作为执行 Worker，我想主动轮询拉取待执行任务，以便 testbed 侧无需暴露入站端口。
20. 作为执行 Worker，我想任务领取具备幂等性，以便重试或多 Worker 时同一任务不被重复执行。
21. 作为执行 Worker，我想执行时从后端拉取代码临时落盘、执行后清理，以便本地无需持久存储。
22. 作为执行 Worker，我想执行完成后自动回传 Allure 结果，以便报告及时入库。
23. 作为执行 Worker，我想断网或崩溃后任务可被恢复或重新领取，以便任务不丢失。
24. 作为测试工程师，我想看到步骤级 pass/fail 报告且每步对应原始文本步骤序号，以便秒级定位失败点。
25. 作为测试工程师，我想查看完整追溯链（原文→结构化→代码→结果），以便审计与复盘。
26. 作为测试工程师，我想操作目录支持按名称/描述检索，以便快速找到所需操作。
27. 作为 AW 库维护者，我想操作目录以机器可读 JSON/YAML 维护，以便系统索引与前端下拉复用同一数据源。
28. 作为测试工程师，我想查看同一用例的多次执行历史，以便对比结果、识别 flaky。

## Implementation Decisions

**模块划分**（backend）：
- `catalog`——操作目录加载、JSON Schema 校验、检索查询；操作目录是唯一数据源，LLM 工具与前端下拉共用
- `mapping`——GLM 5.2 agent 客户端封装（工具：操作目录检索、参数 schema 查询）；LLM client 为可注入接口，无 key/离线时注入 fake
- `confirmation`——用例状态机：`draft → mapped → confirmed → generated → queued → running → done`；确认态编辑仅允许在 `mapped` 态进行
- `rendering`——纯函数：结构化步骤列表 + 模板 → pytest 代码字符串；每个测试步骤渲染为一个 Allure step，step 序号与结构化步骤一一对应
- `execution`——任务队列（DB 实现）、LASS 拓扑校验客户端、Worker REST API（claim / heartbeat / result）
- `reporting`——Allure 结果解析（纯函数）、步骤级映射、四环追溯查询
- `persistence`——SQLAlchemy 模型层

**数据模型**（PostgreSQL，遵循 ADR-0005 唯一主存）：
- `text_case(id, title, precondition, steps_text, expected_text, required_topology jsonb, status, created_at)`
- `structured_step(id, text_case_id, seq, action_text, aw_operation_id nullable, params jsonb, assertion_text, mapping_status enum[mapped|unmapped|manual])`
- `executable_case(id, text_case_id, version, code text, created_at)`——每次生成新增一行，版本递增
- `execution_task(id, executable_case_id, status enum[pending|claimed|running|succeeded|failed], worker_id nullable, claimed_at, finished_at, env_check_result enum[ready|needs_create|needs_modify], env_check_detail jsonb nullable)`——claim 用条件更新保证幂等
- `execution_result(id, task_id, verdict, allure_report jsonb, step_results jsonb, logs text)`

**环境校验分支决策**：
`env_check_result` 三值设计来自 LASS API 响应：
- `ready`：已有环境完全匹配所需拓扑，直接入队，Worker 可领取执行
- `needs_create`：无匹配环境，返回缺失资源清单；任务状态为 `blocked`，前端提示"需新建"，不生成 worker 任务，等待环境中台处理
- `needs_modify`：已有环境部分匹配但存在差异（如 UE 数量不足、仪表型号不符），返回差异说明；任务状态为 `blocked`，前端提示"需修改"，不生成 worker 任务，等待环境中台处理

环境中台（LASS）完成建设/修改后，用例可重新触发校验，闭环推进。本系统**不直接参与**规建维优，只消费校验 API 的返回结果。

**API 契约**（REST）：
- 前端：`POST /text-cases`、`POST /text-cases/{id}/map`、`GET /operations?q=`、`PATCH /text-cases/{id}/steps`、`POST /text-cases/{id}/generate`（有未映射步骤时 409）、`POST /executable-cases/{id}/execute`（先 LASS 校验，依结果分支）、`GET /executions/{id}/report`、POST /executable-cases/{id}/recheck`（重新环境校验）`
- Worker：`POST /worker/tasks/claim`、`POST /worker/tasks/{id}/heartbeat`、`POST /worker/tasks/{id}/result`
- LASS（对方提供）：环境可用性校验 API，返回三值结果 + 详细说明

**关键约束**（来自 ADR）：LLM 不直接产出代码（0001）；未映射步骤禁止猜测（0002）；Web 不直连仪表（0003）；step 标记与结构化步骤一一对应（0004）；DB 唯一主存、代码临时落盘（0005）。

**技术栈**：后端 FastAPI + SQLAlchemy + PostgreSQL；前端 React；Worker 为独立 Python 包；GLM 5.2 本地部署。

## Testing Decisions

- **好测试的标准**：只测外部行为（HTTP 响应、落库数据、生成的代码字符串、报告映射），不测内部实现；fake 只注在系统边界（GLM、LASS、Worker 执行器）。
- **主接缝**：FastAPI TestClient 全链路——录入 → fake GLM 映射 → 确认态编辑（含未映射手选）→ 生成（断言代码含 Allure step 标记）→ 入队执行。
- **环境分支测试**：fake LASS 返回三种结果，断言各分支的 task 状态和前端可见性。
- **纯函数单测**：模板渲染（快照断言）、Allure 解析（预置 allure-results JSON → 步骤级结果）。
- **Worker**：fake 后端（httpx mock）测轮询/幂等领取/回传；另设 1 个真实 pytest 子进程守护测试，用样例可执行用例验证"落盘→执行→产出 allure-results"真实闭环。
- **Prior art**：ai-hero-cli 的命令层接缝 + fake LLM + 临时目录，及 `cli.pilot.test.ts` 的真实进程管道守护模式。

## Out of Scope

- LASS 环境**自动申请**与**变更编排**（MVP 只做可用性校验 + 返回差异，不直接触达规建维优流程）
- 多用户认证与权限体系
- Word/Excel 用例批量导入
- MBB 场景编排的深度自动化
- 消息队列、任务优先级调度、多 Worker 负载均衡
- 执行报告的趋势分析与 Dashboard

## Further Notes

- 操作目录的首批条目需 AW 侧按 JSON Schema 提供，这是 LLM 映射质量的前提。
- LASS 环境校验 API 由对方按需开发，契约需在联调前冻结。三值返回（ready/needs_create/needs_modify）及详细字段结构是关键设计输入。
- 未映射步骤的统计将作为操作目录补全的需求输入（ADR-0002 的衍生收益）。
- 环境校验结果为 blocked 时，任务不入 worker 队列，避免占用 Worker 资源。

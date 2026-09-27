# Spec: Wireless Test System MVP

## Problem Statement

无线测试工程师用自然语言编写文本用例（预知条件/测试步骤/预期结果），但 testbed（LASS + MBB + BBU/UE/仪表）只能执行 pytest 代码。手工把文本翻译成代码耗时、易错、不可追溯；而 LLM 直接生成代码又有向真实射频硬件下发幻觉指令的风险。

**更深层的矛盾**：隐含的专家知识写不清晰——文本用例质量是管线的前置瓶颈。种子用例需经术语库与 grill 协同打磨定稿，日常新增用例也需要质量内建机制。

**调试资源矛盾**：系统生产的 pytest 代码需要反复调试（生成后自验、失败后改了再跑），而真实 testbed 拓扑被多人共享、一次调试会话独占整套环境且常持续数小时；错误类型中占多数的映射错误、参数错误、用例语义错误其实不需要射频硬件就能发现，让它们占用真实环境是资源错配。

**约束**：本地 GLM 5.2 仅提供 CLI（可加载 skill），无 HTTP API。

testbed 环境存在三种复杂场景：已有环境可直接执行、需新建环境、或需修改已有环境。环境中台（LASS/MBB）承担"规建维优"全生命周期职责，用例转化系统必须与这一业务全景对齐，把环境差异作为执行前置条件纳入工作流。

## Solution

一个 Web 系统，管线如下：

工程师三段分栏录入文本用例 → **扩写评估**（GLM 评估充分性、输出缺失点与追问，用户逐条回答合并回原文；可跳过） → 本地 GLM 5.2 CLI（加载映射 skill）将文本映射为结构化步骤 → 确认态人工审核（未映射步骤高亮、手选兜底，禁止猜测） → 模板渲染为 AW pytest 代码 → **在沙盒仿真中高频调试**（虚拟 BBU/UE/仪表，桩校验+有状态仿真两级保真，三态判决；不碰射频硬件、不做 LASS 校验；失败只许回上游修改后重新生成，禁止直接改代码） → 声明所需拓扑 → **LASS 环境校验，返回三值结果（已满足/需新建/需修改）** → 依结果分支：已满足则直接入队；需新建/需修改则阻断并附带差异说明，由环境中台处理 → testbed 侧拉取式 Worker 真实执行（一次性验证而非全程独占） → Allure 步骤级报告映射回原始文本步骤。

批量用例通过**参数化进化**（`POST /text-cases/{id}/evolve`）从种子用例生成，继承种子映射，追溯链升五环（种子→进化→结构化→代码→结果）。

## User Stories

1. 作为测试工程师，我想分三栏录入预知条件、测试步骤、预期结果，以便用自然语言描述用例而无需写代码。
2. 作为测试工程师，我想保存文本用例草稿并稍后继续编辑，以便跨时段完成用例编写。
3. 作为测试工程师，我想提交用例后系统自动评估内容充分性，以便识别遗漏。
4. 作为测试工程师，我想看到系统生成的针对性追问列表，以便补全不清晰或缺失的信息。
5. 作为测试工程师，我想逐条回答追问后原文自动合并，以便持续打磨用例质量。
6. 作为测试工程师，我想跳过扩写直接进入映射，以便紧急场景快速推进（接受质量风险）。
7. 作为测试工程师，我想一键触发 GLM 映射得到结构化步骤，以便快速进入审核环节。
8. 作为测试工程师，我想看到每个步骤映射到的 AW 操作及参数，以便核对系统理解是否正确。
9. 作为测试工程师，我想未映射步骤被显著高亮，以便集中处理系统不认识的操作。
10. 作为测试工程师，我想从操作目录下拉框为未映射步骤手动选择操作，以便不依赖 LLM 猜测。
11. 作为测试工程师，我想在确认态编辑任意步骤的操作与参数，以便修正 LLM 的映射错误。
12. 作为测试工程师，我想仅当全部步骤均已映射时才能生成可执行用例，以便保证下发代码完整可追溯。
13. 作为测试工程师，我想查看生成的 pytest 代码全文，以便必要时做最终人工审查（只读，不可在线编辑）。
14. 作为测试工程师，我想修改文本后重新触发映射，以便迭代打磨用例。
15. 作为测试工程师，我想每次生成产生新的代码版本记录，以便回溯任意历史版本。
16. 作为测试工程师，我想在用例中声明所需拓扑（BBU/UE/仪表组合），以便系统理解执行前提。
17. 作为测试工程师，我想提交执行时系统自动调用 LASS 校验拓扑可用性，以便避免无效排队。
18. 作为测试工程师，我想拓扑不可用时得到明确原因提示，以便调整拓扑或改期。
19. 作为测试工程师，我想环境校验返回"已满足/需新建/需修改"三种结论，以便明确下一步动作。
20. 作为测试工程师，我想环境校验结果为"需新建"时看到缺失资源的明确清单，以便提交环境中台建设。
21. 作为测试工程师，我想环境校验结果为"需修改"时看到差异说明，以便提交环境中台维护优化。
22. 作为测试工程师，我想环境中台完成环境处理后再次触发校验，以便闭环推进。
23. 作为种子用例负责人，我想在系统中将用例标记为种子（认证），以便作为进化基准。
24. 作为测试工程师，我想在种子用例中标注可变参数槽位（频段/功率/UE 数等），以便批量生成进化用例。
25. 作为测试工程师，我想通过选择种子并填写槽位值快速生成进化用例，以便批量场景的高效覆盖。
26. 作为测试工程师，我想进化用例自动继承种子的结构化步骤与映射，以便零 LLM 成本、零幻觉批量生产。
27. 作为测试工程师，我想查看进化用例的血缘关系（种子→进化），以便追溯与审计。
28. 作为执行 Worker，我想主动轮询拉取待执行任务，以便 testbed 侧无需暴露入站端口。
29. 作为执行 Worker，我想任务领取具备幂等性，以便重试或多 Worker 时同一任务不被重复执行。
30. 作为执行 Worker，我想执行时从后端拉取代码临时落盘、执行后清理，以便本地无需持久存储。
31. 作为执行 Worker，我想执行完成后自动回传 Allure 结果，以便报告及时入库。
32. 作为执行 Worker，我想断网或崩溃后任务可被恢复或重新领取，以便任务不丢失。
33. 作为测试工程师，我想看到步骤级 pass/fail 报告且每步对应原始文本步骤序号，以便秒级定位失败点。
34. 作为测试工程师，我想查看完整追溯链（种子→进化→结构化→代码→结果），以便审计与复盘。
35. 作为测试工程师，我想操作目录支持按名称/描述检索，以便快速找到所需操作。
36. 作为 AW 库维护者，我想操作目录以机器可读 JSON/YAML 维护，以便系统索引与前端下拉复用同一数据源。
37. 作为测试工程师，我想查看同一用例的多次执行历史，以便对比结果、识别 flaky。
38. 作为测试工程师，我想在提交真实执行前于沙盒仿真中运行生成的代码，以便不占用 testbed 就能发现映射错误、参数错误与用例语义错误。
39. 作为测试工程师，我想沙盒按用例声明的所需拓扑自动实例化虚拟设备（标准默认态），以便零搭建成本开始调试。
40. 作为测试工程师，我想在调试面板设定虚拟设备的初始状态并保存为调试预设，以便复现特定前置场景且不污染用例正式数据。
41. 作为测试工程师，我想在沙盒报告中逐步骤看到"仿真通过/仅桩校验/未仿真"标注与仿真包版本，以便判断每一步结论的可信度。
42. 作为测试工程师，我想沙盒判决明确区分为通过/失败/不可判定三态，以便存在未仿真步骤时不会被误导为验证通过。
43. 作为测试工程师，我想沙盒失败后只能回到确认态改映射或回到文本修改、再重新生成代码版本，以便代码始终保持 100% 模板渲染血缘。
44. 作为测试工程师，我想沙盒结果为不可判定时仍可提交真实执行，但需看到未覆盖步骤清单并二次确认，以便在仿真覆盖不足时知情决策。
45. 作为测试工程师，我想在本机运行一个沙盒 Worker，以便调试任务不出本机、不与任何人争抢共享资源。
46. 作为测试工程师，我想查看同一代码版本的最近若干次调试会话，以便对比迭代效果。
47. 作为测试工程师，我想沙盒报告显著标注"仿真执行、未进行环境校验"，以便与真实执行报告区分、不被误用为环境可用性证据。

## Implementation Decisions

**用例状态机**（8 态，沙盒调试不加新状态）：
`draft → elaborating → mapped → confirmed → generated → queued → running → done`

- `draft`：初始录入态
- `elaborating`：扩写中，GLM 评估充分性，用户回答追问
- `mapped`：结构化步骤已生成，进入确认态
- `confirmed`：用户完成确认态编辑（全部步骤已映射）
- `generated`：可执行用例已生成；**沙盒调试在此态内闭环**（generate → 发起调试会话 → 失败则回 mapped 改映射或改文本重新映射 → 重新 generate 新版本 → 再调试），可高频反复，不产生状态迁移
- `queued`：用户正式提交真实执行（`generated → queued`）
- `running`：real Worker 已领取并执行中
- `done`：执行完成（含 `blocked`——环境校验不通过时转 done，结果体带 `env_check_result`）

**模块划分**（backend）：
- `catalog`——操作目录加载、JSON Schema 校验、检索查询；校验每个操作的仿真供给声明
- `elaboration`——GLM CLI 调用（加载扩写 skill），产出缺失点与追问列表；临时工作目录注入（`input.md` + `terms.json` + 输出 schema），子进程异步，轮询取结果
- `mapping`——GLM CLI 调用（加载映射 skill），产出结构化步骤；同样子进程异步轮询；候选集预筛注入（操作目录子集 + 术语子集 + 输出 schema）
- `confirmation`——确认态编辑，处理未映射步骤与手选
- `rendering`——纯函数：结构化步骤 → pytest 代码（Allure step 标记）
- `evolution`——参数化进化：槽位值替换 + 复制种子结构化步骤
- `sandbox`——仿真供给加载（自动桩/声明式效果描述符/Python 仿真包）、虚拟拓扑实例化、调试预设管理、逐步骤仿真级别聚合与三态判决、调试会话记录与保留策略
- `execution`——任务队列（DB）、LASS 拓扑校验客户端、Worker 注册/心跳/领取/回传 REST API、按执行目标与 Worker 能力路由
- `reporting`——Allure 解析、步骤级映射、五环追溯查询（仅真实执行）
- `persistence`——SQLAlchemy 模型层

**数据模型**（PostgreSQL，ADR-0005）：
- `text_case(id, title, origin enum[seed|evolved], parent_case_id nullable, variable_slots jsonb, precondition, steps_text, expected_text, required_topology jsonb, status, elaboration_qa jsonb, created_at)`
- `structured_step(id, text_case_id, seq, action_text, aw_operation_id nullable, params jsonb, assertion_text, mapping_status enum[mapped|unmapped|manual])`
- `executable_case(id, text_case_id, version, code text, created_at)`
- `execution_task(id, executable_case_id, execution_target enum[sandbox|real], status, worker_id nullable, claimed_at, finished_at, heartbeat_at, env_check_result enum[ready|needs_create|needs_modify] nullable, env_check_detail jsonb)`——`execution_target` 区分沙盒传输任务与真实任务；正式执行历史与五环追溯只查 `real`
- `execution_result(id, task_id, verdict, allure_report jsonb, step_results jsonb, logs text)`
- `debug_run(id, executable_case_id, task_id, verdict enum[passed|failed|inconclusive], step_results jsonb（逐步骤含仿真级别：simulated|schema_stub|unsimulated 与 pass/fail）, preset jsonb, sim_package_version, created_at)`——调试会话业务记录，仅保留最近 N 次（应用层清理），不进五环追溯

**操作目录仿真供给声明**（catalog 每个操作条目新增）：

```
simulatable: "schema_stub" | "declarative" | "python" | "none"
sim_ref: string?                 # declarative 效果描述符引用 / python 仿真实现引用
sim_package_version: string?     # python 类仿真的包版本
```

- `schema_stub`：由参数 schema 自动生成桩，仅签名/类型校验
- `declarative`：效果描述符声明写/读哪些虚拟设备状态路径、预置返回值或错误码（schema 预留 fault 字段，MVP 不启用失败注入面板）
- `python`：AW 团队提供的纯 Python 仿真（独立包、无硬件 SDK 依赖），用于复杂操作
- `none`：沙盒中该步骤标"未仿真"
- **仿真实现所有权归 AW 团队，随 catalog 一起交付与版本管理；本系统团队不手写仿真**

**沙盒判决三态**（ADR-0009）：
- `passed`：全部步骤有仿真实现（declarative/python）且断言全过
- `failed`：有仿真实现的步骤中断言失败或 AW 报错——真实失败，必须修
- `inconclusive`：存在仅桩校验或未仿真步骤；前端禁止展示为绿色通过，提交真实执行需二次确认并展示未覆盖步骤清单

**沙盒调试修复路径（只许上游修复）**：
- 映射/参数错误 → 回确认态编辑（generated → mapped），重新 generate
- 用例文本/语义错误 → 改文本重新映射，旧结构化步骤作废，重新 generate
- 任何情况下代码只读，系统不提供代码在线编辑入口；代码永远 100% 由模板渲染产出（ADR-0001 原则延伸）

**虚拟拓扑与调试预设**：沙盒按 `required_topology` 自动实例化虚拟 BBU/UE/仪表为标准默认态；调试预设（如"UE 已注册"）仅存于 debug_run 上下文快照，不写入 text_case。沙盒执行不做 LASS 校验，报告固定标注"仿真执行未进行环境校验"。

**Worker 能力路由**（ADR-0003 延伸）：
- Worker 启动注册：`POST /worker/register`，body `{worker_id, capabilities: ["sandbox"|"real"], sim_package_version?, topology_tags?}`；周期性心跳，超时摘除
- claim 按任务 `execution_target` + Worker 能力过滤，先到先得；sandbox Worker 可多实例（含工程师本机），real Worker 仍只部署 testbed 侧
- 沙盒报告记录 `sim_package_version`，用于仿真与 catalog 漂移排查

**GLM CLI 接入契约**（ADR-0006）：
- 后端为每次映射/扩写创建临时工作目录
- 目录内容：`input.md`（用例文本）、`catalog_subset.json`（预筛候选操作）、`terms.json`（相关术语子集）、`output_schema.json`（期望输出 JSON schema）
- 子进程调用：`glm-cli --skill <skill_path> --workdir <dir>`
- CLI 内部自主完成 agent 循环（检索+推理），产出 `result.json`
- 后端轮询文件存在性直至完成；超时/失败回退为任务失败态

**扩写 skill 输出 schema**：`{ sufficient: bool, missing_points: [{field, question}], elaborated_text?: string }`
- `sufficient=false` 时进入 `elaborating` 态，前端展示 `missing_points`
- 用户回答后合并为新版 `input.md`，再次提交扩写（可迭代）
- 用户强制跳过时直接进入 `mapped` 态（跳过扩写评估）

**映射 skill 输出 schema**：`{ steps: [{ seq, action_text, aw_operation_id?, params, assertion_text, mapping_status }] }`
- `mapping_status=unmapped` 时 `aw_operation_id=null`，确认态高亮

**进化接口**：`POST /text-cases/{id}/evolve`（种子用例 id，body `{slot_values}`）
- 复制种子结构化步骤到新用例
- 替换 params 中对应槽位值
- 追溯链五环自动生成

**API 契约**（REST）：
- 前端：`POST /text-cases`（创建）、`POST /text-cases/{id}/elaborate`（触发扩写）、`POST /text-cases/{id}/map`（触发映射）、`GET /operations?q=`、`PATCH /text-cases/{id}/steps`（确认态编辑）、`POST /text-cases/{id}/generate`、`POST /executable-cases/{id}/debug`（发起沙盒调试，body 可选 preset）、`GET /executable-cases/{id}/debug-runs`（最近 N 次调试会话）、`POST /executable-cases/{id}/execute`（正式真实执行；沙盒 inconclusive 后调用须带确认标记）、`POST /executable-cases/{id}/recheck`、`POST /text-cases/{id}/evolve`
- Worker：`POST /worker/register`、`POST /worker/tasks/claim`（按能力过滤）、`POST /worker/tasks/{id}/heartbeat`、`POST /worker/tasks/{id}/result`
- LASS：环境校验 API（三值 + 详细说明）

**关键约束**（来自 ADR）：
- LLM 不直接产出代码（0001）；未映射步骤禁止猜测（0002）
- Web 不直连仪表（0003）；Allure 步骤级映射（0004）
- DB 唯一主存（0005）；GLM CLI 子进程异步（0006）
- 扩写前置（0007）；参数化进化血缘落库（0008）
- 沙盒仿真先于真实 testbed，三态判决、禁止假绿、只许上游修复（0009）

**技术栈**：后端 FastAPI + SQLAlchemy + PostgreSQL；前端 React；Worker 独立 Python 包（sandbox/real 双能力模式）；GLM 5.2 本地 CLI。

## Testing Decisions

- **好测试的标准**：只测外部行为（HTTP 响应、落库数据、生成代码、报告映射、判决结果），不测内部实现；fake 只注在系统边界（GLM CLI、LASS、仿真包供给、Worker 执行器）。
- **主接缝**：FastAPI TestClient 全链路——录入 → fake CLI 扩写 → fake CLI 映射 → 确认态编辑 → 生成 → 沙盒调试（三态分支）→ 正式入队 → 真实执行回传。
- **GLM CLI 边界 fake**：一个 shell 脚本可执行文件，读工作目录、按输入产出固定 JSON（参数化，支持扩写/映射两种输出 schema），守护全流程。
- **环境分支测试**：fake LASS 返回三种结果，断言 task 状态与前端可见性。
- **沙盒分支测试**：用最小仿真包夹具（declarative + python + none 三类操作各一）驱动三种判决——全 simulated 通过判 passed、断言失败判 failed、含 stub/none 步骤判 inconclusive；断言前端提交真实执行时 inconclusive 需确认标记、passed 不需要。
- **能力路由测试**：注册 sandbox/real 两类 Worker，断言 sandbox 任务不被 real Worker 领取、反之亦然；心跳超时后任务可被重新领取。
- **上游修复闭环测试**：沙盒 failed 后断言系统不存在代码编辑入口（API 层 404/405 守护），只能经 PATCH steps → 重新 generate 产生新版本。
- **纯函数单测**：模板渲染（快照断言）、Allure 解析、声明式效果描述符解释器、三态判决聚合。
- **Worker**：fake 后端（httpx mock）测注册/轮询/幂等领取/心跳摘除/回传；sandbox 模式以真实 pytest 子进程 + 桩 AW 包守护端到端（沿用 ai-hero-cli `cli.pilot.test.ts` 真实进程管道模式）。
- **Prior art**：ai-hero-cli 命令层接缝 + fake LLM + 临时目录，及 `cli.pilot.test.ts` 真实进程管道守护模式。

## Out of Scope

- LASS 环境自动申请与变更编排（MVP 只做校验 + 差异说明）
- 多用户认证与权限体系
- Word/Excel 用例批量导入
- MBB 场景编排的深度自动化
- 消息队列、任务优先级调度、多 Worker 负载均衡
- 执行报告趋势分析与 Dashboard
- LLM 自由变异生成新用例（进化仅限参数化槽位赋值）
- **射频级数字孪生（L3）：信道仿真、信号生成、真实测量值仿真**
- **失败注入 UI 与编排（效果描述符 schema 预留 fault 字段，二期实现）**
- **从真实 LASS 环境快照生成虚拟拓扑（LASS 无快照能力）**
- **在沙盒中扮演 LASS 三值校验（三值分支由 fake LASS 测试与真实联调覆盖）**
- **直接编辑生成代码（架构性禁止，非延后功能）**

## Further Notes

- 操作目录首批条目需 AW 侧按 JSON Schema 提供，并随附仿真供给（至少 schema_stub；核心操作应有 declarative/python 实现）；仿真操作覆盖率是沙盒实际价值的运营指标，覆盖率低则 inconclusive 比例高、真实 testbed 压力缓解有限。
- 仿真包与 catalog 须一起版本管理；沙盒报告展示 sim_package_version，漂移排查以此为证据。
- LASS 环境校验 API 需在联调前冻结契约（三值 + 差异字段）。
- 扩写 skill 与映射 skill 的输出 schema 是 CLI 与本系统的接口契约，需版本管理。
- 种子用例的"认证"（标记 origin=seed）是质量杠杆，应设审批机制（MVP 可手动标记）。
- 术语库 repo 地址需提供，系统启动时 pin commit 加载。
- debug_run 保留次数 N 为配置项，初值建议 20。

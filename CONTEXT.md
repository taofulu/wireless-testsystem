# Wireless Test System

将测试工程师编写的文本用例转化为 testbed 中可执行的 pytest 用例，并远程触发执行、回传步骤级报告的 Web 系统。

## Language

### 用例（输入侧）

**文本用例 (Text Case)**:
用户编写的原始测试用例，固定由预知条件、测试步骤、预期结果三段组成，三段分栏填写。
_Avoid_: 自然语言用例、手工用例

**预知条件 (Precondition)**:
执行测试步骤前必须满足的环境与设备状态描述。

**测试步骤 (Test Step)**:
对 testbed 中设备或仪表的一次有序操作。

**预期结果 (Expected Result)**:
测试步骤执行后可观测、可断言的判定标准。

### 转化

**结构化步骤 (Structured Step)**:
从文本用例中提取出的"动作-参数-断言"三元组序列，是代码生成的直接输入。

**AW 操作 (AW Operation)**:
AW 代码库中封装的高阶测试操作，在 pytest 框架内可直接调用。
_Avoid_: 关键字、指令、API

**操作目录 (Operation Catalog)**:
机器可读（JSON/YAML）的 AW 操作清单，每项含名称、描述、参数 schema、前置条件。LLM 映射与确认态下拉框共用此唯一数据源。

**未映射步骤 (Unmapped Step)**:
LLM 无法在操作目录中找到匹配项的结构化步骤。必须人工从操作目录手选，禁止 LLM 猜测生成代码。

**确认态 (Confirmation State)**:
结构化提取完成后、代码生成前的人工审核编辑状态。未映射步骤在此高亮处理。

**可执行用例 (Executable Case)**:
模板渲染产出的、符合 AW pytest 框架约定的 `.py` 测试文件。

### Testbed 与执行

**Testbed**:
执行可执行用例的无线测试环境，由 LASS、MBB 及 BBU/UE/仪表/服务器资源组成。

**LASS**:
环境管理软件平台，负责环境申请与可用性校验。

**MBB**:
场景平台，提供无线场景配置能力。

**所需拓扑 (Required Topology)**:
可执行用例声明的执行环境需求（BBU/UE/仪表组合），执行前须通过 LASS 校验可用性。

**执行 Worker (Execution Worker)**:
部署在 testbed 侧的轻量进程，轮询 Web 后端拉取任务、执行 pytest、回传报告。Web 后端不直连仪表。

### 报告与追溯

**步骤级报告 (Step-level Report)**:
基于 Allure 的执行报告，每个测试步骤对应独立的 pass/fail 断言点。

**追溯链 (Traceability Chain)**:
原始文本 → 结构化步骤 → 可执行用例 → 执行结果的四环关联，四环全部落库、可双向回查。

# GLM 5.2 仅 CLI 接入：子进程 + 工作目录文件契约，映射异步化

本地 GLM 5.2 不提供 HTTP API，仅提供 Claude 式 CLI（可单独加载 skill）。后端以子进程方式调用：为每次映射准备临时工作目录（用例文本、操作目录子集、术语子集、输出 schema），加载映射 skill，CLI 内部自主完成检索与推理（agent 循环在 CLI 内部），产出结构化 JSON。因 CLI 延迟不可控，映射设计为异步任务（提交 → 轮询状态 → 取结果）。

**Considered Options**: 要求 GLM 侧提供 HTTP API——不可控的外部依赖；HTTP 同步等待 CLI 完成——延迟超时时用户体验不可接受。

**Consequences**: 原 Q6"agent 模式"的工具循环由 CLI 内部承担，后端职责收缩为"预筛 + 注入"；测试接缝位于 CLI 子进程边界，可用 fake CLI 可执行脚本（读工作目录、写固定 JSON）守护全链路；扩写环节复用同一接入方式（加载不同 skill）。

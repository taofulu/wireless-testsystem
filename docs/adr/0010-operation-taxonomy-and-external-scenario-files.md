# 操作分类法与外部场景文件：五类 AW 操作、命令字典服务端校验、场景引用版本冻结

真实 testbed 上的 AW 操作不是同质的一类：BBU 侧以成千上万条 MML 命令为主，辅以升级、起跟踪、导出日志等长时操作；仪器仪表侧除原始接口操作外，大量使用数据模板——按模板在 MBB 场景库生成的场景文件，经上传、播放送入真实仪表。为支撑这种异构性，操作目录每个条目声明两个正交维度：`kind`（`mml_family | mml_generic | long_running | instrument_primitive | composite`）与 `device_target`（`bbu | ue | instrument | mbb`），作为渲染器选模板、沙盒选仿真策略、映射 skill 组织候选集的共同依据。

**MML 建模（分层混合）**：高频命令族收为独立操作（`mml_family`，参数 schema 化）；长尾命令收为一个通用 MML 操作（`mml_generic`），其命令名与参数必须通过机器可读的**命令字典**校验。命令字典由 BBU/网元团队随版本供给、系统导入转换；字典缺失期间，通用 MML 操作对所有命令判"未仿真"且映射转 unmapped，系统不凭空编造字典。映射 skill 输出的长尾命令在落库前由后端做**服务端字典二次校验**，不认即转 unmapped 进确认态手选，不信任 LLM 自报合法（ADR-0002 边界加固）。

**场景文件（外部实体，引用而非内嵌）**：结构化步骤以 `{scenario_id, scenario_version}` 引用 MBB 场景库产出的场景文件，版本在映射/确认时冻结落库，保证历史用例追溯不失真。"上传文件→播放→等待就绪"封装为一个 `composite` 操作（如 `play_scenario`），对外保持"一步骤↔一操作↔一报告映射"，内部声明有序子操作序列；步骤模型不引入子操作数组。文件优先经 MBB 与仪表间既有直通通道传递（Worker 只传引用），无直通则 Worker 凭 ID 从 MBB 拉取后上传；下载/不可达归类为环境类失败。后端同步场景库索引供确认态下拉检索（id/version/名称/模板类型/元信息），MBB 无查询 API 时降级为手工填写 ID+版本、播放时校验存在性。

**Considered Options**:
- MML 逐条枚举为独立操作：映射最精准但目录随 BBU 版本爆炸，维护不可行。
- MML 只保留单一通用操作、不做字典校验：LLM 可填任意命令，ADR-0002"禁止猜测"被架空，沙盒无从校验。
- "上传+播放"拆成多个结构化步骤：破坏与原始文本步骤的一一对应，Allure 步骤序号映射错乱。
- 步骤支持任意子操作数组：把组合复杂度泄漏给映射 skill 与确认态 UI。
- 场景文件内容内嵌进用例：二进制文件体积大、与 MBB 单一数据源冲突、版本无法独立管理。

**Consequences**:
- catalog schema 增加 `kind`、`device_target`；`mml_generic` 条目携带命令字典引用，`long_running` 条目声明阶段状态与 `produces_artifacts`，composite 条目声明有序子操作序列与各子操作参数映射。
- 沙盒中 composite 操作的仿真级别取子操作最差者：任一子操作为 none 则整步 inconclusive。场景播放的仿真可行性取决于场景文件是否携带机器可读元信息（模板参数、信号特征、预期仪表状态）；有元信息则 play_scenario 可做声明式仿真（读元信息改写虚拟仪表状态），无元信息则诚实标记"含场景播放步骤，必须真实 testbed 验证"。元信息由 MBB 侧为数据模板配套供给（先覆盖核心模板），与 catalog 仿真供给同源。
- 升级/起跟踪/导出日志标记 `kind: long_running`，沙盒中声明式仿真为立即完成并产出占位制品元数据；真实执行产出的**制品**（日志包、跟踪文件）MVP 不入库，结果中只记录 testbed 侧可访问路径与校验和，二期再谈制品存储。
- 映射候选集 `catalog_subset.json` 按 kind/device_target 组织，mml_generic 仅注入相关命令族的字典片段而非整本字典。
- 新增前端 API：`GET /scenarios?q=`（场景库索引检索；无 API 时降级手工录入）。

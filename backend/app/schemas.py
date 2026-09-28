"""API 输入输出 schema（Pydantic）。字段命名与 CONTEXT.md / spec 数据模型对齐。"""
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class TextCaseCreate(BaseModel):
    """新建文本用例：三栏分栏录入，落库为 draft 态。

    三栏允许留空——草稿常是半成品，稍后从列表回来补全（用户故事 2）。
    required_topology（故事 16）：所需拓扑声明（BBU/UE/仪表组合），可后补；
    执行前由 LASS 三值校验消费。
    """

    title: str = Field(min_length=1, max_length=200)
    precondition: str = ""
    steps_text: str = ""
    expected_text: str = ""
    required_topology: Optional[dict[str, Any]] = None


class TextCasePatch(BaseModel):
    """草稿继续编辑：仅允许修改三栏内容、标题与所需拓扑，不回退状态。

    required_topology 是执行元数据而非扩写评估输入：改它不动扩写闸门
    （apply_text_case_patch 的闸门字段集不含拓扑）。
    """

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    precondition: Optional[str] = None
    steps_text: Optional[str] = None
    expected_text: Optional[str] = None
    required_topology: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# 扩写域（T4）：充分性评估、缺失点追问、多轮问答
# ---------------------------------------------------------------------------

# missing_points.field 只能指向三栏（追问必须能合并回原文某个栏目）
ElaborationField = Literal["precondition", "steps_text", "expected_text"]

# 扩写作业细粒度状态（存于 text_case.elaboration_qa，不新增用例状态）
ElaborationState = Literal[
    "running",            # CLI 子进程执行中
    "awaiting_answers",   # sufficient=false，等待工程师逐条回答
    "answered",           # 答案已合并为新版本，可再次扩写或跳过
    "sufficient",         # CLI 判定充分，扩写闸门通过
    "skipped",            # 工程师强制跳过，扩写闸门通过
    "failed",             # CLI 超时/失败/坏输出，允许重试或跳过
]
ElaborationErrorCode = Literal[
    "timeout",       # CLI 在超时窗口内未产出 result.json
    "cli_failed",    # 无法启动 / 非零退出
    "bad_result",    # result.json 缺失或不符合输出 schema
    "interrupted",   # 服务重启导致在途作业中断（启动时回收）
]


class MissingPoint(BaseModel):
    """扩写 skill 输出 schema 中的单条追问（spec 168 行）。"""

    model_config = {"extra": "forbid"}

    field: ElaborationField
    question: str = Field(min_length=1, max_length=500)


class ElaborationCLIResult(BaseModel):
    """GLM CLI result.json 的边界校验；不信任子进程输出的形状。"""

    model_config = {"extra": "forbid"}

    sufficient: bool
    missing_points: list[MissingPoint] = Field(default_factory=list)
    elaborated_text: Optional[str] = None


class ElaborationAnswerIn(BaseModel):
    """逐条回答：field+question 必须与当轮 missing_points 对应，防止过期表单。"""

    model_config = {"extra": "forbid"}

    field: ElaborationField
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=5000)


class ElaborationAnswersIn(BaseModel):
    answers: list[ElaborationAnswerIn] = Field(min_length=1)


class MissingPointOut(BaseModel):
    field: str
    question: str


class ElaborationAnswerOut(BaseModel):
    field: str
    question: str
    answer: str


class ElaborationRoundOut(BaseModel):
    """单轮评估及其问答，多轮迭代全程留痕。"""

    round: int
    missing_points: list[MissingPointOut] = Field(default_factory=list)
    answers: list[ElaborationAnswerOut] = Field(default_factory=list)


class ElaborationErrorOut(BaseModel):
    code: ElaborationErrorCode
    detail: Optional[str] = None


class ElaborationOut(BaseModel):
    """扩写状态对外视图：前端据此渲染轮询进度、问答表单与失败提示。"""

    state: ElaborationState
    round: int
    missing_points: list[MissingPointOut] = Field(default_factory=list)
    rounds: list[ElaborationRoundOut] = Field(default_factory=list)
    error: Optional[ElaborationErrorOut] = None


# ---------------------------------------------------------------------------
# 映射域（T5）：GLM 映射结构化步骤、异步作业轮询（ADR-0001/0002/0006/0010）
# ---------------------------------------------------------------------------

# 映射作业细粒度状态（存于 text_case.mapping_job，不新增用例状态）
MappingState = Literal[
    "running",      # CLI 子进程执行中
    "succeeded",    # 结构化步骤已落库（可能含被服务端对账降级的 unmapped 步骤）
    "failed",       # CLI 超时/失败/坏输出，允许重试
]
MappingErrorCode = Literal[
    "timeout",       # CLI 在超时窗口内未产出 result.json
    "cli_failed",    # 无法启动 / 非零退出
    "bad_result",    # result.json 缺失、不符合输出 schema 或步骤序列非法
    "interrupted",   # 服务重启导致在途作业中断（启动时回收）
    "internal_error",  # 作业线程内部错误兜底，避免作业永久卡在 running
]

# 映射 skill 只能自报 mapped/unmapped；manual 是确认态人工手选产物（T6）
CLIMappingStatus = Literal["mapped", "unmapped"]
StepMappingStatus = Literal["mapped", "unmapped", "manual"]


class MappingCLIStepIn(BaseModel):
    """映射 skill 输出 schema 中的单条结构化步骤（spec 173 行契约）。"""

    model_config = {"extra": "forbid"}

    seq: int = Field(ge=1)
    action_text: str = Field(min_length=1, max_length=2000)
    aw_operation_id: Optional[int] = None
    params: dict[str, Any] = Field(default_factory=dict)
    assertion_text: str = Field(default="", max_length=2000)
    mapping_status: CLIMappingStatus


class MappingCLIResult(BaseModel):
    """GLM CLI result.json 的边界校验；不信任子进程输出的形状。"""

    model_config = {"extra": "forbid"}

    steps: list[MappingCLIStepIn] = Field(min_length=1)


class MappingErrorOut(BaseModel):
    code: MappingErrorCode
    detail: Optional[str] = None


class ReclassificationOut(BaseModel):
    """服务端对账把 LLM 自报 mapped 降级为 unmapped 的留痕（确认态提示分类）。"""

    seq: int
    code: str
    detail: Optional[str] = None


class MappingJobOut(BaseModel):
    """映射作业对外视图：前端据此轮询进度、展示步骤统计与失败提示。"""

    state: MappingState
    error: Optional[MappingErrorOut] = None
    step_count: Optional[int] = None
    mapped_count: Optional[int] = None
    unmapped_count: Optional[int] = None
    reclassifications: list[ReclassificationOut] = Field(default_factory=list)


class StructuredStepOut(BaseModel):
    """结构化步骤对外视图：确认态（T6）与渲染（T7）消费的落库数据。"""

    id: int
    seq: int
    action_text: str
    aw_operation_id: Optional[int] = None
    params: dict[str, Any]
    assertion_text: str
    mapping_status: StepMappingStatus

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# 确认态域（T6）：人工审核、未映射手选、参数编辑、确认闸门
# ---------------------------------------------------------------------------


class StepPatchIn(BaseModel):
    """单步编辑：可改操作引用与参数（故事 10/11）。

    - aw_operation_id 为 null 表示清空操作（回退 unmapped，允许撤销误选）
    - 仅提供 params 且不提供 aw_operation_id 时，在原操作上改参数
    - 提供 aw_operation_id 时按目标操作校验 params（mml_generic 过字典、
      composite 场景类冻结 scenario_id/scenario_version）
    """

    model_config = {"extra": "forbid"}

    id: int
    aw_operation_id: Optional[int] = None
    params: Optional[dict[str, Any]] = None


class StepsPatchIn(BaseModel):
    """确认态批量编辑：一次提交多个步骤的操作/参数修改。"""

    model_config = {"extra": "forbid"}

    steps: list[StepPatchIn] = Field(min_length=1)


class ConfirmationOut(BaseModel):
    """确认结果：确认后用例进入 confirmed 态。"""

    status: str
    step_count: int
    manual_count: int


# ---------------------------------------------------------------------------
# 生成域（T7）：可执行用例版本、只读代码查看
# ---------------------------------------------------------------------------


class ExecutableCaseOut(BaseModel):
    """可执行用例版本视图（版本历史列表用；代码全文经 /code 获取）。"""

    id: int
    text_case_id: int
    version: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ExecutableCodeOut(ExecutableCaseOut):
    """代码全文视图（故事 13 只读查看）。"""

    code: str


class TextCaseOut(BaseModel):
    """文本用例对外视图。

    origin/variable_slots/parent_case_id 三字段承载 T14 参数化进化血缘
    （ADR-0008）：origin=seed 是手工认证的种子，origin=evolved 由种子进化
    产生、parent_case_id 指向种子；非进化路径下三者均为 None。
    """

    id: int
    title: str
    precondition: str
    steps_text: str
    expected_text: str
    status: str
    required_topology: Optional[dict[str, Any]] = None
    elaboration: Optional[ElaborationOut] = None
    mapping: Optional[MappingJobOut] = None
    origin: Optional[str] = None
    variable_slots: Optional[dict[str, str]] = None
    parent_case_id: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# 进化域（T14）：种子认证、参数槽位、用例进化（ADR-0008，故事 23–27）
# ---------------------------------------------------------------------------

# 用例血缘来源（与 TextCaseOrigin 同源；Literal 让非法取值在 422 被拦）
TextCaseOriginValue = Literal["seed", "evolved"]


class MarkSeedIn(BaseModel):
    """标记用例为种子（MVP 手工认证，spec Further Notes 故事 23）并声明
    可变参数槽位定义（频段/功率/UE 数等）。

    槽位定义为 名称→描述 的映射；进化时按槽位名提供值，缺一不可。重提交
    时整体替换槽位定义（不增量合并），允许调整槽位集。允许空字典——某些
    种子参数化分量为零（如全静态用例），仍可作进化基准，slot_values 须为空。
    """

    model_config = {"extra": "forbid"}

    variable_slots: dict[str, str] = Field(default_factory=dict)


class EvolveIn(BaseModel):
    """进化用例：按槽位名提供值（故事 24）。

    slot_values 必须覆盖种子声明的全部槽位（多出/缺失一律 422 拒绝，
    不允许部分进化）；后端复制种子的结构化步骤与文本并把
    ``{{slot_name}}`` 占位符替换为值。进化过程零 LLM 成本、确定性可复现
    （ADR-0008：不重新走扩写+映射）。
    """

    model_config = {"extra": "forbid"}

    slot_values: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 目录域（T3）：操作目录、命令字典、场景库索引
# ---------------------------------------------------------------------------

OperationKind = Literal[
    "mml_family",
    "mml_generic",
    "long_running",
    "instrument_primitive",
    "composite",
]
DeviceTarget = Literal["bbu", "ue", "instrument", "mbb"]
Simulatable = Literal["schema_stub", "declarative", "python", "none"]


class OperationIn(BaseModel):
    """操作目录条目（AW 库维护者机器可读供给，文件与导入 API 同构）。

    extra="forbid" 拦截拼写错误的字段；kind 专属声明按 ADR-0010 校验。
    """

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1)
    kind: OperationKind
    device_target: DeviceTarget
    params_schema: dict[str, Any]
    simulatable: Simulatable
    sim_ref: Optional[str] = None
    sim_package_version: Optional[str] = None
    # composite 专属：有序子操作序列（对外仍是一步骤↔一操作↔一报告映射）
    suboperations: Optional[list[str]] = None
    # long_running 专属：阶段状态与制品声明
    stages: Optional[list[str]] = None
    produces_artifacts: Optional[bool] = None
    # mml_generic 专属：命令字典引用
    dictionary_ref: Optional[str] = None

    @model_validator(mode="after")
    def _validate_kind_specific(self) -> "OperationIn":
        if self.simulatable in ("declarative", "python") and not self.sim_ref:
            raise ValueError(f"simulatable={self.simulatable} requires sim_ref")
        if self.simulatable == "python" and not self.sim_package_version:
            raise ValueError("simulatable=python requires sim_package_version")

        if self.kind == "composite":
            if not self.suboperations:
                raise ValueError("composite requires non-empty suboperations")
        elif self.suboperations is not None:
            raise ValueError("suboperations only allowed for composite kind")

        if self.kind == "long_running":
            if not self.stages:
                raise ValueError("long_running requires non-empty stages")
            if self.produces_artifacts is None:
                raise ValueError("long_running requires produces_artifacts")
        else:
            if self.stages is not None or self.produces_artifacts is not None:
                raise ValueError("stages/produces_artifacts only allowed for long_running kind")

        if self.kind == "mml_generic":
            if not self.dictionary_ref:
                raise ValueError("mml_generic requires dictionary_ref")
        elif self.dictionary_ref is not None:
            raise ValueError("dictionary_ref only allowed for mml_generic kind")
        return self


class OperationOut(BaseModel):
    """操作目录对外视图：核心声明列 + kind 专属字段展平。"""

    id: int
    name: str
    description: str
    kind: str
    device_target: str
    params_schema: dict[str, Any]
    simulatable: str
    sim_ref: Optional[str] = None
    sim_package_version: Optional[str] = None
    suboperations: Optional[list[str]] = None
    stages: Optional[list[str]] = None
    produces_artifacts: Optional[bool] = None
    dictionary_ref: Optional[str] = None


class ParamSpecIn(BaseModel):
    """字典参数声明：参数名/类型/取值范围。"""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1)
    type: Literal["string", "int", "float", "bool"]
    min: Optional[float] = None
    max: Optional[float] = None
    allowed_values: Optional[list[Any]] = None
    description: Optional[str] = None

    @model_validator(mode="after")
    def _validate_ranges(self) -> "ParamSpecIn":
        if self.type in ("int", "float"):
            if self.min is not None and self.max is not None and self.min > self.max:
                raise ValueError(f"param {self.name}: min > max")
        elif self.min is not None or self.max is not None:
            raise ValueError(f"param {self.name}: min/max only allowed for int/float")
        return self


class CommandIn(BaseModel):
    """命令字典中的单条命令。"""

    model_config = {"extra": "forbid"}

    command: str = Field(min_length=1, max_length=100)
    description: Optional[str] = None
    params: list[ParamSpecIn]


class DictionaryImportIn(BaseModel):
    """命令字典导入：随 BBU 版本供给，版本号对齐字典来源。"""

    model_config = {"extra": "forbid"}

    version: str = Field(min_length=1, max_length=50)
    commands: list[CommandIn] = Field(min_length=1)


class ScenarioIn(BaseModel):
    """场景库索引条目（MBB 同步与手工录入同构）。"""

    model_config = {"extra": "forbid"}

    scenario_id: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    template_type: Optional[str] = Field(default=None, max_length=100)
    has_meta: bool = False


class ScenarioOut(BaseModel):
    """场景库索引对外视图（id/version/名称/模板类型/元信息可用性）。"""

    id: int
    scenario_id: str
    version: str
    name: str
    template_type: Optional[str] = None
    has_meta: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# 执行域（T8）：Worker 注册、能力路由、沙盒调试、结果回传
# ---------------------------------------------------------------------------


# 执行目标二值（CONTEXT.md）：Literal 约束让非法能力值直接被 422 拦截
Capability = Literal["sandbox", "real"]


class WorkerRegisterIn(BaseModel):
    """Worker 启动注册与信息更新（spec 157 行契约）；重复注册视为续约更新。"""

    model_config = {"extra": "forbid"}

    worker_id: str = Field(min_length=1, max_length=100)
    capabilities: list[Capability] = Field(min_length=1)
    sim_package_version: Optional[str] = Field(default=None, max_length=50)
    topology_tags: Optional[list[str]] = Field(default=None)


class WorkerHeartbeatIn(BaseModel):
    """Worker 保活心跳（独立于任务心跳：空闲 Worker 也需保活不被摘除）。"""

    model_config = {"extra": "forbid"}

    worker_id: str = Field(min_length=1, max_length=100)


class WorkerClaimIn(BaseModel):
    """Worker 领取任务的身份声明；能力过滤以注册表为准（防自报绕过隔离）。"""

    model_config = {"extra": "forbid"}

    worker_id: str = Field(min_length=1, max_length=100)
    # capabilities 供客户端自报一致性校验；实际过滤以 worker.capabilities 为准
    capabilities: list[Capability] = Field(default_factory=list)


class WorkerOut(BaseModel):
    """Worker 注册响应。"""

    worker_id: str
    capabilities: list[str]
    sim_package_version: Optional[str] = None

    model_config = {"from_attributes": True}


class TaskClaimOut(BaseModel):
    """任务领取响应：含可执行用例引用，Worker 凭此拉取代码。"""

    task_id: int
    executable_case_id: int
    execution_target: str

    model_config = {"from_attributes": True}


class TaskResultIn(BaseModel):
    """Worker 执行完成后的结果回传。

    verdict 取值（ADR-0009 + T12）：
    - passed/failed/inconclusive：沙盒三态（inconclusive 由沙盒内核在存在
      仅桩校验或未仿真步骤时产出，禁止假绿）；real 通路断言失败为 failed
    - env_failed：real 通路环境类失败（场景文件不可达/无权限等），与断言
      失败 failed 在判决上区分（故事 53）
    allure_report：real 通路回传的 Allure 结果原文（T13 做步骤级解析）
    """

    model_config = {"extra": "forbid"}

    worker_id: str = Field(min_length=1, max_length=100)
    verdict: Literal["passed", "failed", "inconclusive", "env_failed"]
    logs: str
    step_results: list[Any] = Field(default_factory=list)
    artifacts: list[Any] = Field(default_factory=list)
    allure_report: Optional[dict[str, Any]] = None


class TaskResultOut(BaseModel):
    """结果回传落库响应。"""

    task_id: int
    verdict: str

    model_config = {"from_attributes": True}


class ExecutionTaskOut(BaseModel):
    """任务简要视图（含 T11 环境校验结论：阻断任务的阻断原因随任务呈现）。"""

    id: int
    executable_case_id: int
    execution_target: str
    status: str
    worker_id: Optional[str] = None
    env_check_result: Optional[str] = None
    env_check_detail: Optional[dict[str, Any]] = None

    model_config = {"from_attributes": True}


class ExecutionResultOut(BaseModel):
    """执行结果视图（T12）：判决、日志、逐步骤、制品与 Allure 原文。"""

    task_id: int
    verdict: str
    logs: str
    step_results: list[Any]
    artifacts: list[Any]
    allure_report: Optional[dict[str, Any]] = None
    sim_package_version: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ExecutionRecordOut(BaseModel):
    """一次真实执行的任务 + 结果合成视图（结果页/追溯列表用）。"""

    id: int
    executable_case_id: int
    execution_target: str
    status: str
    worker_id: Optional[str] = None
    env_check_result: Optional[str] = None
    env_check_detail: Optional[dict[str, Any]] = None
    created_at: datetime
    finished_at: Optional[datetime] = None
    result: Optional[ExecutionResultOut] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# 沙盒调试域（T9/T10）：调试预设、沙盒上下文、调试会话、真实执行闸门
# ---------------------------------------------------------------------------

# 沙盒报告的固定声明（ADR-0009：与真实执行报告区分，不得误用为环境可用性证据）
SANDBOX_ENVIRONMENT_DISCLAIMER = "仿真执行、未进行环境校验"

# 逐步骤仿真级别（CONTEXT.md：simulated|schema_stub|unsimulated）
SimLevel = Literal["simulated", "schema_stub", "unsimulated"]


class DebugIn(BaseModel):
    """发起沙盒调试的可选参数：调试预设（虚拟设备初始状态）。

    预设仅存于 debug_run 上下文，不写入用例正式数据（故事 40，ADR-0009）。
    """

    model_config = {"extra": "forbid"}

    preset: Optional[dict[str, Any]] = None


class SandboxSubStepOut(BaseModel):
    """组合操作的子操作仿真供给视图（仿真级别取子操作最差者的输入）。

    descriptor/sim_ref 与主步骤同语义：Worker 侧组合解释器据此执行子操作。
    """

    name: str
    simulatable: SimLevel
    descriptor: Optional[dict[str, Any]] = None
    sim_ref: Optional[str] = None


class SandboxStepOut(BaseModel):
    """沙盒上下文中的单步仿真供给：Worker 内核据此分发桩/声明式/Python 仿真。"""

    seq: int
    op_name: str
    kind: str
    device_target: str
    # 有效仿真级别（已按 composite 最差子操作、场景元信息门聚合）
    simulatable: SimLevel
    # L1 桩校验依据（schema_stub 级别必需；其余级别附带供调试面板展示）
    params_schema: dict[str, Any] = Field(default_factory=dict)
    # declarative 供给的效果描述符内容（后端从 catalog_dir 内联；缺失即 None
    # 并在 effective 级别上已降级为 unsimulated）
    descriptor: Optional[dict[str, Any]] = None
    # python 供给的仿真实现引用（module:function）
    sim_ref: Optional[str] = None
    produces_artifacts: bool = False
    suboperations: list[SandboxSubStepOut] = Field(default_factory=list)


class SandboxContextOut(BaseModel):
    """沙盒上下文：Worker 内核执行用例所需的全部仿真供给 + 调试预设。"""

    executable_case_id: int
    steps: list[SandboxStepOut]
    preset: Optional[dict[str, Any]] = None


class SandboxStepMetaOut(BaseModel):
    """确认态/调试面板的逐步骤仿真覆盖视图（故事 41/55：提前知情）。

    simulatable 字段承载的是聚合后的有效级别（SimLevel），不是目录声明值。
    """

    seq: int
    op_name: str
    simulatable: SimLevel


class SandboxMetaOut(BaseModel):
    """用例级仿真覆盖概览：任一 unsimulated 步骤即预告沙盒必然 inconclusive。"""

    executable_case_id: int
    steps: list[SandboxStepMetaOut]
    has_uncovered: bool


class DebugRunOut(BaseModel):
    """调试会话对外视图（故事 41/42/46/47）。"""

    id: int
    executable_case_id: int
    task_id: int
    verdict: str
    step_results: list[Any]
    preset: Optional[dict[str, Any]] = None
    sim_package_version: Optional[str] = None
    # 固定声明：仿真执行未进行环境校验（ADR-0009）
    environment_disclaimer: str = SANDBOX_ENVIRONMENT_DISCLAIMER
    created_at: datetime

    model_config = {"from_attributes": True}


class DebugRunListOut(BaseModel):
    """最近 N 次调试会话（N 为配置项 debug_run_keep_latest）。"""

    keep_latest: int
    runs: list[DebugRunOut]


class ExecuteIn(BaseModel):
    """提交真实执行（T10 骨架；LASS 三值校验在 T11 插入）。

    沙盒判决为 inconclusive（或无调试结论）时必须携带 confirm_inconclusive
    二次确认标记才放行（故事 44）；passed 不需要确认；failed 一律拒绝——
    只许回上游修复（故事 43）。
    """

    model_config = {"extra": "forbid"}

    confirm_inconclusive: bool = False


# ---------------------------------------------------------------------------
# 报告与追溯域（T13）：步骤级报告、五环追溯、flaky 识别（故事 33/34/37/47）
# ---------------------------------------------------------------------------


class AllureStepOut(BaseModel):
    """Allure 单步映射回原始文本步骤序号的视图（故事 33）。

    seq 为 None 表示该 Allure 步骤不对应原始文本步骤（setup/teardown 附属步骤），
    仍展示但不参与步骤级失败定位。action_text 由结构化步骤按 seq 回填。
    """

    seq: Optional[int] = None
    title: str
    status: str
    action_text: Optional[str] = None


class StepReportOut(BaseModel):
    """步骤级报告视图（故事 33/47）。

    real 任务：steps 为 Allure 解析结果，映射回原始文本步骤序号。
    sandbox 任务：steps 取调试会话 step_results（仿真标注），并携带
    environment_disclaimer（ADR-0009：显著区分于真实执行报告，不被误用为
    环境可用性证据）。is_sandbox 供前端视觉分流。
    """

    task_id: int
    execution_target: str
    is_sandbox: bool
    verdict: str
    # 仅沙盒任务携带固定声明；real 任务为 None（与沙盒报告显著区分，故事 47）
    environment_disclaimer: Optional[str] = None
    steps: list[AllureStepOut] = Field(default_factory=list)
    logs: str = ""
    artifacts: list[Any] = Field(default_factory=list)
    sim_package_version: Optional[str] = None
    created_at: Optional[datetime] = None


class FlakySummaryOut(BaseModel):
    """同一用例多次真实执行的 flaky 识别（故事 37）。

    is_flaky：≥2 次执行且同时出现 passed 与 failed（断言失败）——同代码版本
    不同轮次结果不一致即代码 flaky。env_failed 是环境类失败（故事 53），不计为
    flaky（环境不稳 ≠ 代码 flaky）。仅统计真实执行，沙盒调试会话不计入。
    """

    total_runs: int
    passed_count: int
    failed_count: int
    is_flaky: bool


class ExecutionRingOut(BaseModel):
    """五环追溯的"执行结果"环：真实执行历史 + flaky 识别（故事 34/37）。

    仅统计真实执行（spec：沙盒调试会话不进五环追溯）。records 新的在前。
    """

    records: list[ExecutionRecordOut]
    flaky: FlakySummaryOut


class TextCaseSummaryOut(BaseModel):
    """追溯链中用例环节的摘要视图（种子/进化环）。

    非进化路径下种子/进化环为空（parent_case_id 为 null，故事 34）。
    """

    id: int
    title: str
    status: str
    parent_case_id: Optional[int] = None

    model_config = {"from_attributes": True}


class TraceChainOut(BaseModel):
    """五环追溯视图（故事 34）：种子→进化→结构化→代码→结果，双向回查。

    非进化路径下 seed_case/evolution_case 为 None（parent_case_id 为 null）；
    进化路径（T14 落地后）seed_case 为父用例、evolution_case 为当前用例本身。
    executions 环仅含真实执行（沙盒调试会话不进追溯链，ADR-0009）。
    """

    executable_case_id: int
    seed_case: Optional[TextCaseSummaryOut] = None
    evolution_case: Optional[TextCaseSummaryOut] = None
    structured_steps: list[StructuredStepOut] = Field(default_factory=list)
    executable_case: ExecutableCaseOut
    executions: ExecutionRingOut

"""API 输入输出 schema（Pydantic）。字段命名与 CONTEXT.md / spec 数据模型对齐。"""
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class TextCaseCreate(BaseModel):
    """新建文本用例：三栏分栏录入，落库为 draft 态。

    三栏允许留空——草稿常是半成品，稍后从列表回来补全（用户故事 2）。
    """

    title: str = Field(min_length=1, max_length=200)
    precondition: str = ""
    steps_text: str = ""
    expected_text: str = ""


class TextCasePatch(BaseModel):
    """草稿继续编辑：仅允许修改三栏内容与标题，不回退状态。"""

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    precondition: Optional[str] = None
    steps_text: Optional[str] = None
    expected_text: Optional[str] = None


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


class TextCaseOut(BaseModel):
    """文本用例对外视图（追溯/拓扑字段后续票补充）。"""

    id: int
    title: str
    precondition: str
    steps_text: str
    expected_text: str
    status: str
    elaboration: Optional[ElaborationOut] = None
    created_at: datetime

    model_config = {"from_attributes": True}


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

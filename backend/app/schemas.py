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


class TextCaseOut(BaseModel):
    """文本用例对外视图（录入阶段字段子集，追溯/拓扑字段后续票补充）。"""

    id: int
    title: str
    precondition: str
    steps_text: str
    expected_text: str
    status: str
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

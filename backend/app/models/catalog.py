"""目录域 ORM 模型：操作目录、命令字典、场景库索引（T3，ADR-0010）。

操作目录是映射与确认态共用的唯一数据源；kind/device_target/simulatable
为每个条目的声明维度（取值见 app.schemas 的 Literal 定义），kind 专属声明
（子操作序列、阶段/制品、字典引用）收敛在 extra JSON 列。
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Operation(Base):
    """操作目录条目；按 name 幂等导入（AW 库维护者机器可读供给）。"""

    __tablename__ = "operation"
    __table_args__ = (UniqueConstraint("name", name="uq_operation_name"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    device_target: Mapped[str] = mapped_column(String(20), nullable=False)
    params_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    simulatable: Mapped[str] = mapped_column(String(20), nullable=False)
    sim_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sim_package_version: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )
    # kind 专属声明：composite→suboperations；long_running→stages/produces_artifacts；
    # mml_generic→dictionary_ref
    extra: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class DictionaryState(Base):
    """命令字典生效版本（单行）：最新导入的字典为当前校验依据。"""

    __tablename__ = "dictionary_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    active_version: Mapped[str] = mapped_column(String(50), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class CommandDictionaryEntry(Base):
    """命令字典条目：命令名 + 参数名/类型/取值范围（BBU 团队随版本供给）。"""

    __tablename__ = "command_dictionary_entry"
    __table_args__ = (
        UniqueConstraint("version", "command", name="uq_dict_version_command"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    command: Mapped[str] = mapped_column(String(100), nullable=False)
    # [{name, type, min?, max?, allowed_values?, description?}]
    params: Mapped[list] = mapped_column(JSON, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Scenario(Base):
    """MBB 场景库索引：确认态下拉检索用；步骤引用冻结 (scenario_id, version)。"""

    __tablename__ = "scenario"
    __table_args__ = (
        UniqueConstraint("scenario_id", "version", name="uq_scenario_id_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scenario_id: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    template_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # 机器可读元信息（模板参数/信号特征/预期仪表状态）是否可用——决定
    # play_scenario 能否声明式仿真
    has_meta: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

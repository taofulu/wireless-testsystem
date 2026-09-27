"""映射域 ORM 模型：结构化步骤（T5，ADR-0001/0002/0010）。

结构化步骤是"动作-参数-断言"三元组序列，模板渲染（T7）的直接输入。
mml_generic 步骤落库的 params 为 {command, args}，且已在映射落库前通过
命令字典服务端二次校验；字典不认的命令一律 unmapped（不信任 LLM 自报）。

manual 状态由确认态人工手选产生（T6）；映射 skill 只产出 mapped/unmapped，
服务端对账只可能把 mapped 降级为 unmapped，从不反向提升。
"""
import enum
from typing import Optional

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MappingStatus(str, enum.Enum):
    """结构化步骤映射状态（spec 数据模型 mapping_status 枚举）。"""

    MAPPED = "mapped"      # 命中操作目录（mml_generic 已过字典校验）
    UNMAPPED = "unmapped"  # 未命中/被字典拦截，确认态高亮手选（ADR-0002）
    MANUAL = "manual"      # 确认态人工手选（T6）


class StructuredStep(Base):
    __tablename__ = "structured_step"
    __table_args__ = (
        UniqueConstraint("text_case_id", "seq", name="uq_step_case_seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    text_case_id: Mapped[int] = mapped_column(
        ForeignKey("text_case.id"), nullable=False
    )
    # 原始文本步骤序号，同一用例内从 1 连续；与原始步骤一一对应（ADR-0010）
    seq: Mapped[int] = mapped_column(nullable=False)
    action_text: Mapped[str] = mapped_column(Text, nullable=False)
    # unmapped/manual 兜底前可为 None；mapped 必指向操作目录中的真实条目
    aw_operation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("operation.id"), nullable=True
    )
    # mml_generic 存 {command, args}（已过字典校验）；场景类在确认态冻结
    # {scenario_id, scenario_version}
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    assertion_text: Mapped[str] = mapped_column(Text, nullable=False)
    mapping_status: Mapped[MappingStatus] = mapped_column(
        String(20), nullable=False
    )

"""文本用例（Text Case）ORM 模型。

字段与状态机遵循 spec 数据模型与 8 态用例状态机
（draft → elaborating → mapped → confirmed → generated → queued → running → done）。
扩写阶段（T4）不新增状态：用例进入 elaborating 后，细粒度作业状态存于
elaboration_qa（running/awaiting_answers/answered/sufficient/skipped/failed），
映射（T5）仅在闸门 sufficient/skipped 后允许触发。
"""
import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TextCaseStatus(str, enum.Enum):
    """用例状态机（spec 83-93 行）；str 混合便于 JSON 序列化与 DB 取值一致。"""

    DRAFT = "draft"
    ELABORATING = "elaborating"
    MAPPED = "mapped"
    CONFIRMED = "confirmed"
    GENERATED = "generated"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"


class TextCase(Base):
    __tablename__ = "text_case"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # 三栏分栏录入（CONTEXT.md：文本用例固定由三段组成）
    precondition: Mapped[str] = mapped_column(Text, nullable=False)
    steps_text: Mapped[str] = mapped_column(Text, nullable=False)
    expected_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TextCaseStatus] = mapped_column(
        default=TextCaseStatus.DRAFT, nullable=False
    )
    # 扩写问答状态（spec 数据模型 elaboration_qa jsonb）：None 表示从未触发。
    # 结构见 app.elaboration（_new_qa）与 schemas.ElaborationOut。
    elaboration_qa: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 映射作业状态（T5）：None 表示从未触发映射。8 态状态机不新增"映射中"
    # 状态——running 期间用例停留在 elaborating/mapped，作业细粒度状态存于此
    # （running/succeeded/failed），结构见 app.mapping 与 schemas.MappingJobOut。
    mapping_job: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 所需拓扑（T11，故事 16）：{"bbu": 1, "ue": 2, "instrument": [...]} 等
    # BBU/UE/仪表组合声明，execute/recheck 时作为 LASS 三值校验输入；
    # None 表示未声明（LASS 按空拓扑校验）
    required_topology: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 五环追溯锚点（T13，故事 34）：进化用例指向其种子用例；非进化路径
    # （手写用例）为 null——此时追溯视图种子/进化两环为空。T14 进化链路
    # 落地时由创建进化用例的路径填入。
    parent_case_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("text_case.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    @property
    def elaboration(self) -> Optional[dict]:
        """对外视图字段（TextCaseOut.elaboration）直接取 elaboration_qa。"""
        return self.elaboration_qa

    @property
    def mapping(self) -> Optional[dict]:
        """对外视图字段（TextCaseOut.mapping）直接取 mapping_job。"""
        return self.mapping_job

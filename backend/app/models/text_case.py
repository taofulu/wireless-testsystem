"""文本用例（Text Case）ORM 模型。

字段与状态机遵循 spec 数据模型与 8 态用例状态机
（draft → elaborating → mapped → confirmed → generated → queued → running → done）。
T2 纵切片只落库录入字段；origin/血缘/拓扑/扩写 QA 等字段在后续票加入。
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

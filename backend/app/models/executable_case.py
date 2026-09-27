"""可执行用例（Executable Case）ORM 模型（T7，故事 13/15）。

生成的 pytest 代码唯一来源是模板渲染（ADR-0001：LLM 不直接产出代码；
ADR-0004：每个测试步骤渲染为独立 Allure step）。每次生成都追加新版本，
旧版本永不覆盖——上游（文本/映射/确认态）修改后重新生成可回溯任意历史。
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ExecutableCase(Base):
    __tablename__ = "executable_case"
    __table_args__ = (
        UniqueConstraint("text_case_id", "version", name="uq_exec_case_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    text_case_id: Mapped[int] = mapped_column(
        ForeignKey("text_case.id"), nullable=False
    )
    # 同一用例内从 1 连续递增；只追加不覆盖（故事 15）
    version: Mapped[int] = mapped_column(nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

"""调试会话（Debug Run）ORM 模型（T9/T10，ADR-0009）。

调试会话是可执行用例在沙盒中的一次执行记录：关联特定代码版本与执行它的
execution_task，携带三态判决、逐步骤仿真标注、调试预设快照与仿真包版本。

边界（ADR-0009 / spec 数据模型）：
- 独立于正式执行历史与五环追溯——追溯查询只看 execution_target=real
- 仅保留最近 N 次（应用层清理，见 app.sandbox），更早的记录被清除
- preset 仅存于 debug_run 上下文快照，不写入用例正式数据（text_case）
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DebugRun(Base):
    __tablename__ = "debug_run"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    executable_case_id: Mapped[int] = mapped_column(
        ForeignKey("executable_case.id"), nullable=False
    )
    # 执行本次调试的沙盒任务（1:1；任务行同时承载执行队列语义）
    task_id: Mapped[int] = mapped_column(
        ForeignKey("execution_task.id"), unique=True, nullable=False
    )
    # 三态判决：passed / failed / inconclusive（CONTEXT.md；禁止假绿）
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    # 逐步骤标注：[{seq, op, sim_level: simulated|schema_stub|unsimulated,
    #              status: pass|fail|not_run, detail?}]
    step_results: Mapped[list] = mapped_column(JSON, nullable=False)
    # 调试预设快照（虚拟设备初始状态的人工设定；仅属于本会话上下文）
    preset: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 执行 Worker 注册时声明的仿真包版本（仿真与 catalog 漂移排查证据）
    sim_package_version: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

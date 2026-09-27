"""执行域 ORM 模型（T8）：Worker 注册表、执行任务、执行结果。

设计锚点：
- ADR-0003 拉取式 Worker：Worker 主动轮询领取任务，后端不直连 testbed
- ADR-0009：Worker 注册声明能力（sandbox/real）与 sim_package_version；
  execution_task.execution_target 区分沙盒/真实任务，claim 按能力过滤
- spec 数据模型：execution_task(worker_id/claimed_at/finished_at/heartbeat_at)、
  execution_result(verdict/logs/step_results/artifacts)

心跳语义：
- Worker.last_heartbeat 由 /worker/heartbeat 与任务心跳共同续约，超时摘除
- ExecutionTask.heartbeat_at 由任务心跳续约，超时任务可被其他 Worker 重领
  （Worker 崩溃/断网不丢任务，故事 32）

注意：ExecutionTask.worker_id 不设外键——超时摘除会删除 worker 行，
任务行必须能被重领而不受引用约束牵连。
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Worker(Base):
    __tablename__ = "worker"

    # worker_id 由 Worker 侧生成并全局唯一（重复注册视为续约更新）
    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    capabilities: Mapped[list] = mapped_column(JSON, nullable=False)
    sim_package_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    topology_tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class ExecutionTask(Base):
    __tablename__ = "execution_task"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    executable_case_id: Mapped[int] = mapped_column(
        ForeignKey("executable_case.id"), nullable=False
    )
    execution_target: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="queued", nullable=False)
    worker_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # 沙盒调试预设（虚拟设备初始状态；T9）：仅 sandbox 任务携带，回传后
    # 复制进 debug_run 快照；不写入用例正式数据（ADR-0009）
    preset: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 领取后由任务心跳续约；超时即视为 Worker 崩溃，任务可被重领
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    result: Mapped[Optional["ExecutionResult"]] = relationship(
        back_populates="task", lazy="joined"
    )

    @property
    def task_id(self) -> int:
        """对外视图字段（TaskClaimOut.task_id）直接取主键 id。"""
        return self.id


class ExecutionResult(Base):
    __tablename__ = "execution_result"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("execution_task.id"), unique=True, nullable=False
    )
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    logs: Mapped[str] = mapped_column(Text, nullable=False)
    step_results: Mapped[list] = mapped_column(JSON, nullable=False)
    artifacts: Mapped[list] = mapped_column(JSON, nullable=False)
    # 执行 Worker 注册时声明的仿真包版本（ADR-0009：漂移排查证据）
    sim_package_version: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    task: Mapped[ExecutionTask] = relationship(back_populates="result")

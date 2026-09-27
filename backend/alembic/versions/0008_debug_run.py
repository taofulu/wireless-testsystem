"""debug_run 表 + execution_task.preset 列（T9/T10）

调试会话业务记录（ADR-0009）：独立于正式执行历史，仅保留最近 N 次。
execution_task.preset 承载沙盒任务的调试预设，随结果回传复制进 debug_run
快照（任务行是队列语义，debug_run 是业务记录）。

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "execution_task",
        sa.Column("preset", sa.JSON(), nullable=True),
    )
    op.create_table(
        "debug_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("executable_case_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("step_results", sa.JSON(), nullable=False),
        sa.Column("preset", sa.JSON(), nullable=True),
        sa.Column("sim_package_version", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["executable_case_id"], ["executable_case.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["execution_task.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
    )


def downgrade() -> None:
    op.drop_table("debug_run")
    op.drop_column("execution_task", "preset")

"""LASS 三值环境校验（T11）：text_case.required_topology + execution_task 环境校验列

故事 16：用例声明所需拓扑（BBU/UE/仪表组合），作为 LASS 校验输入。
故事 17-22：execute/recheck 触发 LASS 三值校验；阻断任务转 done 并在任务行
留 env_check_result/env_check_detail（spec 数据模型 execution_task 列）。

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "text_case",
        sa.Column("required_topology", sa.JSON(), nullable=True),
    )
    op.add_column(
        "execution_task",
        sa.Column("env_check_result", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "execution_task",
        sa.Column("env_check_detail", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_task", "env_check_detail")
    op.drop_column("execution_task", "env_check_result")
    op.drop_column("text_case", "required_topology")

"""execution_result.allure_report 列（T12）

real Worker 真实执行回传的 Allure 结果原文落库（spec 数据模型
execution_result.allure_report jsonb）；步骤级解析与五环追溯映射在 T13。

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "execution_result",
        sa.Column("allure_report", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_result", "allure_report")

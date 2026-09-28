"""text_case.origin + variable_slots 列（T14 参数化用例进化）

- origin：血缘来源 enum[seed|evolved]，nullable（null=手写用例未标记种子）。
  手工认证标记 origin=seed 后才可作为进化基准；进化产生的新用例 origin=evolved。
- variable_slots：种子用例的可变参数槽位定义（名称→描述），形如
  {"freq_band": "频段", "power": "功率dBm"}。进化时按槽位名提供值，
  复制种子结构化步骤并替换 {{slot_name}} 占位符。

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "text_case",
        sa.Column("origin", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "text_case",
        sa.Column("variable_slots", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("text_case", "variable_slots")
    op.drop_column("text_case", "origin")

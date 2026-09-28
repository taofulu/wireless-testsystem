"""text_case.parent_case_id 列（T13 五环追溯锚点）

进化用例指向其种子用例的外键；非进化路径（手写用例）为 null——追溯视图
种子/进化两环为空。T14 进化链路落地时由创建进化用例的路径填入。

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "text_case",
        sa.Column("parent_case_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_text_case_parent_case_id",
        "text_case",
        "text_case",
        ["parent_case_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_text_case_parent_case_id", "text_case", type_="foreignkey")
    op.drop_column("text_case", "parent_case_id")

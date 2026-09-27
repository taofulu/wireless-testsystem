"""executable_case 表（模板渲染可执行用例，T7）

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "executable_case",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("text_case_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.ForeignKeyConstraint(["text_case_id"], ["text_case.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("text_case_id", "version", name="uq_exec_case_version"),
    )


def downgrade() -> None:
    op.drop_table("executable_case")

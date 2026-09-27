"""结构化步骤表 + text_case.mapping_job（文本映射为结构化步骤，T5）

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("text_case", sa.Column("mapping_job", sa.JSON(), nullable=True))
    op.create_table(
        "structured_step",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("text_case_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("action_text", sa.Text(), nullable=False),
        sa.Column("aw_operation_id", sa.Integer(), nullable=True),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("assertion_text", sa.Text(), nullable=False),
        sa.Column("mapping_status", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["text_case_id"], ["text_case.id"]),
        sa.ForeignKeyConstraint(["aw_operation_id"], ["operation.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("text_case_id", "seq", name="uq_step_case_seq"),
    )


def downgrade() -> None:
    op.drop_table("structured_step")
    op.drop_column("text_case", "mapping_job")

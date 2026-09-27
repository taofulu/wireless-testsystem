"""text_case 增加 elaboration_qa（扩写问答闭环，T4）

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("text_case", sa.Column("elaboration_qa", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("text_case", "elaboration_qa")

"""initial baseline

首个迁移建立版本基线。T1 无领域表；后续票按纵切片各自新增迁移。

Revision ID: 0001
Revises:
Create Date: 2026-09-27
"""
from typing import Sequence, Union

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 基线：alembic_version 表由此建立，领域表在后续迁移中加入
    pass


def downgrade() -> None:
    pass

"""目录域表：操作目录、命令字典、场景库索引（T3）

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("device_target", sa.String(length=20), nullable=False),
        sa.Column("params_schema", sa.JSON(), nullable=False),
        sa.Column("simulatable", sa.String(length=20), nullable=False),
        sa.Column("sim_ref", sa.Text(), nullable=True),
        sa.Column("sim_package_version", sa.String(length=50), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_operation_name"),
    )
    op.create_table(
        "dictionary_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("active_version", sa.String(length=50), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "command_dictionary_entry",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("command", sa.String(length=100), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version", "command", name="uq_dict_version_command"),
    )
    op.create_table(
        "scenario",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scenario_id", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("template_type", sa.String(length=100), nullable=True),
        sa.Column("has_meta", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scenario_id", "version", name="uq_scenario_id_version"),
    )


def downgrade() -> None:
    op.drop_table("scenario")
    op.drop_table("command_dictionary_entry")
    op.drop_table("dictionary_state")
    op.drop_table("operation")

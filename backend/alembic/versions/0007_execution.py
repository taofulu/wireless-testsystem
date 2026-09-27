"""execution 域三表：worker / execution_task / execution_result（T8）

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "worker",
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("sim_package_version", sa.String(length=50), nullable=True),
        sa.Column("topology_tags", sa.JSON(), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_table(
        "execution_task",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("executable_case_id", sa.Integer(), nullable=False),
        sa.Column("execution_target", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("worker_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["executable_case_id"], ["executable_case.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "execution_result",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("logs", sa.Text(), nullable=False),
        sa.Column("step_results", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("sim_package_version", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["execution_task.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
    )


def downgrade() -> None:
    op.drop_table("execution_result")
    op.drop_table("execution_task")
    op.drop_table("worker")

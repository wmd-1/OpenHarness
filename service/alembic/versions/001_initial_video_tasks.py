"""create video_tasks table

Revision ID: 001_initial
Revises:
Create Date: 2026-06-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "video_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("skill", sa.String(64), nullable=False, server_default="hyperframes"),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED",
                name="taskstatus",
            ),
            nullable=False,
            server_default="QUEUED",
            index=True,
        ),
        sa.Column("celery_task_id", sa.String(256), nullable=True),
        sa.Column("workspace_path", sa.String(512), nullable=True),
        sa.Column("output_path", sa.String(512), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("resolution", sa.String(32), nullable=True),
        sa.Column("fps", sa.Integer(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("log_tail", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(256), nullable=True, unique=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="900"),
        sa.Column("extra_oh_args", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("video_tasks")
    op.execute("DROP TYPE IF EXISTS taskstatus")

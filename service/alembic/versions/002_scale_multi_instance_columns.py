"""add ownership/liveness columns for multi-instance scaling

Revision ID: 002_scale_multi_instance
Revises: 001_initial
Create Date: 2026-07-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "002_scale_multi_instance"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    # Newest owner holding the task (NULL until claimed).
    op.add_column(
        "video_tasks", sa.Column("worker_id", sa.String(256), nullable=True)
    )
    # How many times this task has been (re)attempted; backfilled to 0.
    op.add_column(
        "video_tasks",
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
    )
    # Last liveness heartbeat written by the owning worker (R8).
    op.add_column(
        "video_tasks",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Durable cancellation flag persisted alongside the Redis abort key (R9).
    op.add_column(
        "video_tasks",
        sa.Column(
            "cancellation_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Scheduling priority used to route tiered queues in Phase 7.
    op.add_column(
        "video_tasks",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="5"),
    )
    # RETRYING is a new task status used by the reclaim flow (Phase 2 / R7).
    # On sqlite it is a plain Python-side value (no native enum); on Postgres
    # the native enum must be extended. The Python enum member NAME is
    # "RETRYING" (uppercase) and SQLAlchemy persists the member NAME, so the
    # native enum label must match exactly (uppercase) to stay consistent with
    # the labels created in 001 (QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELED).
    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TYPE taskstatus ADD VALUE 'RETRYING'")


def downgrade() -> None:
    op.drop_column("video_tasks", "priority")
    op.drop_column("video_tasks", "cancellation_requested")
    op.drop_column("video_tasks", "heartbeat_at")
    op.drop_column("video_tasks", "attempt")
    op.drop_column("video_tasks", "worker_id")

"""add storage_kind column for multi-instance scaling

Records which storage backend an artifact was written to so the download
endpoint can resolve the matching backend (scale-multi-instance Phase 3, R4).

Revision ID: 003_storage_kind
Revises: 002_scale_multi_instance
Create Date: 2026-07-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "003_storage_kind"
down_revision: Union[str, None] = "002_scale_multi_instance"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    # Which backend the artifact was written to. Legacy/backfilled rows stay
    # "local" and continue to stream (tasks.md:存量不强制回填).
    op.add_column(
        "video_tasks",
        sa.Column(
            "storage_kind",
            sa.String(16),
            nullable=False,
            server_default="local",
        ),
    )


def downgrade() -> None:
    op.drop_column("video_tasks", "storage_kind")

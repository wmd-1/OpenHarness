"""WS-C strict lease: lease_token column + video_lease_fence mapping table.

Adds ``video_tasks.lease_token`` (BIGINT NOT NULL DEFAULT 0) for the strict
lease / fencing-token protocol (R20), and the ``video_lease_fence`` mapping
table that records which lease token's artifact is the authoritative one for a
task. The first claim's ``lease_token = lease_token + 1`` yields 1, avoiding any
``NULL + 1`` ambiguity.

Revision ID: 006_lease_token
Revises: 005_rls
Create Date: 2026-07-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_lease_token"
down_revision: Union[str, None] = "005_rls"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.add_column(
        "video_tasks",
        sa.Column(
            "lease_token",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_table(
        "video_lease_fence",
        sa.Column("task_id", sa.UUID(), primary_key=True),
        sa.Column("accepted_token", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("video_lease_fence")
    op.drop_column("video_tasks", "lease_token")

"""multi-tenancy: tenants, api_keys, quotas, audit_log, video_tasks.tenant_id

Revision ID: 004_tenant
Revises: 003_storage_kind
Create Date: 2026-07-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "004_tenant"
down_revision: Union[str, None] = "003_storage_kind"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_table(
        "quotas",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("max_concurrent", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("daily_submit_limit", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("rate_per_min", sa.Integer(), nullable=False, server_default="10"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("actor_key_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=True),
        sa.Column("target_id", sa.String(255), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("meta_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])
    op.create_index("ix_audit_log_actor_key_id", "audit_log", ["actor_key_id"])
    # Scoped tenancy for video tasks. NOT NULL + server_default backfills
    # legacy/backfilled rows to the `system` tenant.
    op.add_column(
        "video_tasks",
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="system"),
    )
    op.create_index("ix_video_tasks_tenant_id", "video_tasks", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_video_tasks_tenant_id", table_name="video_tasks")
    op.drop_column("video_tasks", "tenant_id")
    op.drop_index("ix_audit_log_actor_key_id", table_name="audit_log")
    op.drop_index("ix_audit_log_tenant_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("quotas")
    op.drop_index("ix_api_keys_tenant_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("tenants")

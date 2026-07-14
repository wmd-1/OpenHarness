"""VideoTask ORM model and TaskStatus enum."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class VideoTask(Base):
    __tablename__ = "video_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    skill: Mapped[str] = mapped_column(String(64), nullable=False, default="hyperframes")
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), nullable=False, default=TaskStatus.QUEUED, index=True
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    output_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(256), nullable=True, unique=True
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    extra_oh_args: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Multi-instance scaling columns (scale-multi-instance, Phase 1) ---
    worker_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    # --- Storage backend (scale-multi-instance Phase 3, R4) ---
    # Which backend the artifact was written to, so the download endpoint can
    # resolve the matching backend (default "local" for legacy/backfilled rows).
    storage_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="local")

    # --- Multi-tenancy (Phase 3, WS-A) ---
    # Owning tenant; "system" is the default/internal tenant used for unkeyed
    # or trusted-internal requests. NOT NULL + server_default keeps legacy and
    # backfilled rows scoped to `system`.
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system", server_default="system", index=True
    )


# ---------------------------------------------------------------------------
# Multi-tenancy models (Phase 3, WS-A)
# ---------------------------------------------------------------------------


class Tenant(Base):
    """A tenant (organization/customer) that owns video tasks and API keys."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # tenant slug
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApiKey(Base):
    """An API key belonging to a tenant.

    Only the SHA-256 hash of the raw key is stored; the raw key is shown once
    at creation time and never persisted.
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Quota(Base):
    """Per-tenant concurrency / submit / rate limits."""

    __tablename__ = "quotas"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    max_concurrent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    daily_submit_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    rate_per_min: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10"
    )


class AuditLog(Base):
    """Append-only audit trail for mutating operations."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor_key_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)

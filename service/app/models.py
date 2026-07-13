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

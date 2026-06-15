"""Pydantic request / response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import TaskStatus


# ---- Request ----

class VideoCreateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    timeout_seconds: int = Field(default=900, ge=30, le=3600)
    extra_oh_args: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None


# ---- Response ----

class TaskLinks(BaseModel):
    self_: str = Field(alias="self")
    file: str
    events: str

    model_config = {"populate_by_name": True}


class VideoCreateResponse(BaseModel):
    task_id: uuid.UUID
    status: TaskStatus
    links: TaskLinks


class VideoTaskResponse(BaseModel):
    task_id: uuid.UUID
    prompt: str
    skill: str
    status: TaskStatus
    timeout_seconds: int
    output_path: str | None = None
    file_size_bytes: int | None = None
    duration_seconds: float | None = None
    resolution: str | None = None
    fps: int | None = None
    exit_code: int | None = None
    error_message: str | None = None
    log_tail: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class VideoDeleteResponse(BaseModel):
    task_id: uuid.UUID
    status: TaskStatus
    message: str


class HealthResponse(BaseModel):
    status: str
    db: str
    redis: str

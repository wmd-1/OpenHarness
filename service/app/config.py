"""Application configuration via pydantic-settings."""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    db_url: str = "postgresql+asyncpg://oh:oh@localhost:5432/oh"
    db_sync_url: str = "postgresql+psycopg://oh:oh@localhost:5432/oh"
    # Async-native URL used for Alembic migrations (matches the async engine).
    db_migration_url: str = "postgresql+asyncpg://oh:oh@postgres:5432/oh"

    # --- Redis / Celery ---
    broker_url: str = "redis://localhost:6379/0"

    # --- Storage ---
    video_dir: Path = Path("/var/openharness/videos")
    workspace_root: Path = Path("/workspaces")
    # Backend selector + S3 settings (scale-multi-instance Phase 3, R4).
    # storage_kind: "local" (NFS/shared volume) or "s3" (S3-compatible bucket).
    storage_kind: str = "local"
    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None

    # --- oh CLI ---
    oh_bin: str = "/root/.local/bin/oh"
    headless_shell_path: str = "/opt/chrome-headless-shell-linux64/chrome-headless-shell"

    # --- Worker ---
    # Explicit worker identity (OH_WORKER_ID). When unset, each worker process
    # generates an ephemeral uuid used for heartbeat/reclaim (scale-multi-instance).
    worker_id: str | None = None
    celery_concurrency: int = 4
    task_timeout_default: int = 900  # seconds
    task_timeout_min: int = 30
    task_timeout_max: int = 3600

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_workers: int = 2

    # --- Log tail ---
    log_tail_bytes: int = 16384  # 16 KB

    # --- Cleanup ---
    cleanup_retention_days: int = 7

    # --- Scheduler backend (scale-multi-instance Phase 6) ---
    # "celery" (default) uses the Celery broker; "temporal" selects the Temporal
    # stub (not wired by default — placeholder for a future migration).
    scheduler_backend: str = "celery"

    # --- Worker queue tiers + concurrency cap (Phase 7) ---
    # Comma-separated queue names consumed by workers, ordered high -> low
    # priority. A task's ``priority`` column (1-10) maps to one of these tiers.
    worker_queues: str = "high,normal,low"
    # Global cap on concurrently running ``oh`` render subprocesses per worker
    # process (protects Chrome/ffmpeg memory under horizontal scale-out).
    max_concurrent_renders: int = 4

    # --- API Key (optional) ---
    api_key: str | None = None

    # --- Multi-tenancy auth (Phase 3, WS-A) ---
    # When True, every external request MUST present a valid X-API-Key; missing
    # or invalid keys are rejected with 401. When False (dev/default), unkeyed
    # requests are scoped to the `system` tenant (no auth enforced yet).
    auth_require_keys: bool = False
    # Header that, when present, marks the request as a trusted internal caller
    # and scopes it to the `system` tenant (bypassing X-API-Key). Intended to be
    # set only by an ingress/proxy that strips it from external traffic.
    auth_trusted_header: str | None = None

    # --- CORS ---
    # Comma-separated explicit origins. Empty => no CORS allowed.
    # Credentials are only enabled when explicit origins are configured
    # (a wildcard + credentials combo reflects any Origin, which is unsafe).
    cors_origins: str = ""


settings = Settings()

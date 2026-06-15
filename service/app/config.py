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

    # --- Redis / Celery ---
    broker_url: str = "redis://localhost:6379/0"

    # --- Storage ---
    video_dir: Path = Path("/var/openharness/videos")
    workspace_root: Path = Path("/workspaces")

    # --- oh CLI ---
    oh_bin: str = "/root/.local/bin/oh"
    headless_shell_path: str = "/opt/chrome-headless-shell-linux64/chrome-headless-shell"

    # --- Worker ---
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

    # --- API Key (optional) ---
    api_key: str | None = None


settings = Settings()

"""Per-process worker identity for multi-instance scaling.

Each worker *process* advertises a stable ``worker_id`` used by the
heartbeat/reclaim flow (scale-multi-instance R7/R8) to tell which replica owns
a running task. The id is generated once per process (or taken from the
``OH_WORKER_ID`` env var the orchestrator may set for deterministic routing).
"""
from __future__ import annotations

import uuid

from app.config import settings

# Process-global cache so a single worker process reuses one identity for the
# lifetime of the process (including across multiple task executions).
_worker_id: str | None = None


def get_worker_id() -> str:
    """Return this process's stable worker identity.

    Honors ``OH_WORKER_ID`` when set, otherwise generates an ephemeral uuid so
    distinct worker processes are always distinguishable.
    """
    global _worker_id
    if _worker_id is None:
        _worker_id = settings.worker_id or f"worker-{uuid.uuid4().hex[:12]}"
    return _worker_id


def set_worker_id(worker_id: str) -> None:
    """Override the process worker id (used by tests / orchestrator hooks)."""
    global _worker_id
    _worker_id = worker_id

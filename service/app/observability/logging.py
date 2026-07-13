"""Structured JSON logging via structlog.

Every log line carries ``task_id`` / ``worker_id`` / ``attempt`` context when
bound, so multi-replica logs stay attributable to a specific render
(scale-multi-instance Phase 5).

Configure once at process startup via :func:`configure_logging`.
"""

from __future__ import annotations

import logging
import sys

import structlog

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Install structlog JSON logging (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = getattr(logging, level, logging.INFO)
    # structlog emits pre-serialized JSON; let stdlib just pass it through.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str | None = None):
    """Return a structlog logger (optionally named)."""
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_task_context(task_id: str | None = None, worker_id: str | None = None, attempt: int | None = None) -> None:
    """Bind task/worker/attempt into the structlog contextvars for this context."""
    ctx: dict[str, object] = {}
    if task_id is not None:
        ctx["task_id"] = str(task_id)
    if worker_id is not None:
        ctx["worker_id"] = worker_id
    if attempt is not None:
        ctx["attempt"] = attempt
    if ctx:
        structlog.contextvars.bind_contextvars(**ctx)

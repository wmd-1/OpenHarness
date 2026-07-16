"""Tenant context propagation (Phase 3, WS-A).

A contextvar carries the current request's ``tenant_id`` from the auth
middleware down into the DB session layer (where PostgreSQL Row-Level
Security is enabled via ``SET LOCAL app.current_tenant``) and into async
workers, so isolation holds on both the HTTP and the async execution paths
(see plan §6.1).
"""
from __future__ import annotations

from contextvars import ContextVar

_current_tenant: ContextVar[str | None] = ContextVar("oh_current_tenant", default=None)


def set_current_tenant(tenant_id: str) -> None:
    """Bind the tenant for the current context (request / worker task)."""
    _current_tenant.set(tenant_id)


def get_current_tenant() -> str | None:
    """Return the tenant bound to the current context, or ``None``."""
    return _current_tenant.get()

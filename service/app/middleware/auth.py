"""Tenant authentication middleware (Phase 3, WS-A).

Resolves the calling tenant from an ``X-API-Key`` header and stores it on
``request.state.tenant_id`` for downstream routers, workers, and storage to
enforce isolation (see plan §6.1). The raw key is never compared directly;
only its SHA-256 hash is matched against the ``api_keys`` table.

Design notes
------------
* A *trusted internal header* (configured via ``auth_trusted_header``) marks a
  request as coming from an internal caller (e.g. the OpenHarness core behind a
  trusted proxy) and scopes it to the ``system`` tenant, bypassing the key
  lookup. The proxy is responsible for stripping this header from external
  traffic.
* When ``require_keys`` is False (dev/default), requests without any key are
  scoped to ``system`` so un-migrated clients keep working. Flip it to True in
  production to enforce API-key auth on every external request.
* ``/healthz`` (and any path in ``skip_paths``) is always allowed without a key
  so liveness probes are not blocked by auth.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

SYSTEM_TENANT = "system"


def hash_api_key(raw_key: str) -> str:
    """Return the hex SHA-256 of a raw API key (constant, no salt needed)."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _is_expired(expires_at: datetime | None) -> bool:
    """Timezone-safe expiry check (sqlite may return naive datetimes)."""
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


class TenantAuthMiddleware(BaseHTTPMiddleware):
    """Resolve and attach the tenant id for every request."""

    def __init__(
        self,
        app,
        sessionmaker,
        require_keys: bool = False,
        trusted_header: str | None = None,
        system_tenant: str = SYSTEM_TENANT,
        skip_paths: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        self.sessionmaker = sessionmaker
        self.require_keys = require_keys
        self.trusted_header = trusted_header
        self.system_tenant = system_tenant
        self.skip_paths = skip_paths or {"/healthz"}

    async def dispatch(self, request: Request, call_next):
        # Liveness / scrape endpoints never require auth.
        if request.url.path in self.skip_paths:
            request.state.tenant_id = self.system_tenant
            return await call_next(request)

        # Trusted internal caller bypass (header set by an upstream proxy).
        if self.trusted_header and request.headers.get(self.trusted_header):
            request.state.tenant_id = self.system_tenant
            return await call_next(request)

        raw_key = request.headers.get("X-API-Key")
        if raw_key:
            tenant_id = await self._resolve(raw_key)
            if tenant_id is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid, revoked, or expired API key"},
                )
            request.state.tenant_id = tenant_id
            return await call_next(request)

        # No key presented.
        if self.require_keys:
            return JSONResponse(
                status_code=401, content={"detail": "API key required"}
            )
        request.state.tenant_id = self.system_tenant
        return await call_next(request)

    async def _resolve(self, raw_key: str) -> str | None:
        """Return the tenant_id for a valid key, else None."""
        from sqlalchemy import select

        from app.models import ApiKey

        key_hash = hash_api_key(raw_key)
        async with self.sessionmaker() as session:
            result = await session.execute(
                select(ApiKey).where(ApiKey.key_hash == key_hash)
            )
            row = result.scalar_one_or_none()
            if row is None or row.revoked:
                return None
            if _is_expired(row.expires_at):
                return None
            return row.tenant_id

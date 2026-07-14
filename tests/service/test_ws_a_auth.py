"""Tests for WS-A tenant auth middleware (Phase 3).

Exercises ``TenantAuthMiddleware`` directly with an isolated aiosqlite engine
so it does not depend on a running Postgres and does not touch the global app.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.middleware.auth import TenantAuthMiddleware, hash_api_key
from app.models import ApiKey, Base, Tenant

SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://"


async def _inner_app(scope, receive, send):
    """Echo the resolved tenant id so the test can assert on it.

    Must be a proper ASGI app: BaseHTTPMiddleware invokes the wrapped app with
    ``(scope, receive, send)`` and the tenant is read back off ``request.state``.
    """

    request = Request(scope, receive, send)
    resp = JSONResponse({"tenant_id": getattr(request.state, "tenant_id", None)})
    await resp(scope, receive, send)


@pytest.fixture
async def sessionmaker():
    engine = create_async_engine(SQLALCHEMY_DATABASE_URL, echo=False)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield sm
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed(sessionmaker, tenant_id: str, *, revoked=False, expires_at=None):
    async with sessionmaker() as s:
        s.add(Tenant(id=tenant_id, name=tenant_id))
        s.add(
            ApiKey(
                tenant_id=tenant_id,
                key_hash=hash_api_key("secret-" + tenant_id),
                revoked=revoked,
                expires_at=expires_at,
            )
        )
        await s.commit()


def _client(app_inner, sm, *, require_keys=True, trusted_header=None):
    app = TenantAuthMiddleware(
        app_inner,
        sessionmaker=sm,
        require_keys=require_keys,
        trusted_header=trusted_header,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


class TestTenantAuth:
    async def test_no_key_require_false_is_system(self, sessionmaker):
        client = _client(_inner_app, sessionmaker, require_keys=False)
        r = await client.get("/x")
        assert r.status_code == 200
        assert r.json()["tenant_id"] == "system"

    async def test_no_key_require_true_is_401(self, sessionmaker):
        client = _client(_inner_app, sessionmaker, require_keys=True)
        r = await client.get("/x")
        assert r.status_code == 401
        assert r.json()["detail"] == "API key required"

    async def test_valid_key_resolves_tenant(self, sessionmaker):
        await _seed(sessionmaker, "acme")
        client = _client(_inner_app, sessionmaker, require_keys=True)
        r = await client.get("/x", headers={"X-API-Key": "secret-acme"})
        assert r.status_code == 200
        assert r.json()["tenant_id"] == "acme"

    async def test_invalid_key_is_401(self, sessionmaker):
        await _seed(sessionmaker, "acme")
        client = _client(_inner_app, sessionmaker, require_keys=True)
        r = await client.get("/x", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

    async def test_revoked_key_is_401(self, sessionmaker):
        await _seed(sessionmaker, "acme", revoked=True)
        client = _client(_inner_app, sessionmaker, require_keys=True)
        r = await client.get("/x", headers={"X-API-Key": "secret-acme"})
        assert r.status_code == 401

    async def test_expired_key_is_401(self, sessionmaker):
        await _seed(
            sessionmaker,
            "acme",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        client = _client(_inner_app, sessionmaker, require_keys=True)
        r = await client.get("/x", headers={"X-API-Key": "secret-acme"})
        assert r.status_code == 401

    async def test_trusted_header_bypass(self, sessionmaker):
        client = _client(
            _inner_app, sessionmaker, require_keys=True, trusted_header="X-OH-Internal"
        )
        r = await client.get("/x", headers={"X-OH-Internal": "1"})
        assert r.status_code == 200
        assert r.json()["tenant_id"] == "system"

    async def test_healthz_skipped_without_key(self, sessionmaker):
        client = _client(_inner_app, sessionmaker, require_keys=True)
        r = await client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["tenant_id"] == "system"

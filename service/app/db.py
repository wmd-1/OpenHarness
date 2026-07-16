"""SQLAlchemy async engine and session factory."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.tenant_ctx import get_current_tenant

engine = create_async_engine(
    settings.db_url,
    echo=False,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_async_session() -> AsyncSession:  # type: ignore[misc]
    """FastAPI dependency that yields an async DB session.

    On PostgreSQL, Row-Level Security is engaged by setting the session-local
    ``app.current_tenant`` to the tenant resolved for this request (see
    plan §6.1). sqlite (tests) has no RLS, so the statement is skipped — the
    explicit ``tenant_id`` filters in routers/workers already isolate there.
    """
    async with async_session() as session:
        tenant = get_current_tenant()
        if tenant is not None and engine.dialect.name == "postgresql":
            await session.execute(
                text("SET LOCAL app.current_tenant = :v"), {"v": tenant}
            )
        try:
            yield session
        finally:
            await session.close()

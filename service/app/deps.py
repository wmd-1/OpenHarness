"""FastAPI dependency injection helpers."""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session
from app.storage.base import VideoStorage
from app.storage.local import LocalVideoStorage


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async DB session (auto-close)."""
    async with async_session() as session:
        yield session


def get_storage() -> VideoStorage:
    """Return the configured video storage backend."""
    return LocalVideoStorage()

"""/healthz endpoint."""

from fastapi import APIRouter
from sqlalchemy import text

from app.db import engine
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Check DB and Redis connectivity."""
    db_status = "ok"
    redis_status = "ok"

    # Check DB
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"

    # Check Redis
    try:
        import redis as redis_lib

        from app.config import settings

        r = redis_lib.from_url(settings.broker_url)
        r.ping()
        r.close()
    except Exception:
        redis_status = "error"

    overall = "ok" if db_status == "ok" and redis_status == "ok" else "degraded"

    return HealthResponse(status=overall, db=db_status, redis=redis_status)

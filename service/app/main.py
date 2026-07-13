"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.observability.logging import configure_logging
from app.observability.metrics import metrics_router
from app.observability.tracing import setup_tracing
from app.routers import health, videos


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup / shutdown hooks."""
    # Startup (Phase 5 observability). `configure_logging` is idempotent;
    # `setup_tracing` is a silent no-op if OpenTelemetry is unavailable, so the
    # service always boots regardless of the tracing dependency state (R8).
    configure_logging()
    setup_tracing(app)
    yield
    # Shutdown
    from app.db import engine

    await engine.dispose()


app = FastAPI(
    title="OpenHarness Video Service",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — explicit origins only. Credentials are enabled solely when origins
# are explicitly configured; otherwise no cross-origin access is granted.
_cors_origins = (
    [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if settings.cors_origins
    else []
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Optional API key middleware
if settings.api_key:

    @app.middleware("http")
    async def api_key_middleware(request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if request.headers.get("X-API-Key") != settings.api_key:
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
        return await call_next(request)


# Register routers
app.include_router(videos.router)
app.include_router(health.router)
# Phase 5 (R8): Prometheus scrape endpoint exposing oh_render_inflight etc.
app.include_router(metrics_router)

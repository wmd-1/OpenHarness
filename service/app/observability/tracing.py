"""OpenTelemetry tracing setup — OPTIONAL and defensive.

Design source R8: tracing can be disabled. If the ``opentelemetry`` packages
are missing or misconfigured (e.g. ``pkg_resources`` dropped from newer
setuptools), this is a silent no-op so the service still boots with OTLP off.
Instrumentation covers FastAPI, Celery, SQLAlchemy, Redis and boto3 when
available.

Scale-multi-instance Phase 5.
"""

from __future__ import annotations

import os


def setup_tracing(app=None, *, otlp_endpoint: str | None = None) -> bool:
    """Instrument the app/worker with OpenTelemetry if available.

    Returns ``True`` if tracing was enabled, ``False`` if skipped (missing or
    broken dependency). Never raises.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: F401
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.boto3 import Boto3Instrumentor
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:
        # OpenTelemetry unavailable (or its transitive pkg_resources dropped) —
        # tracing is optional, skip silently.
        return False

    try:
        endpoint = otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        resource = Resource.create({"service.name": "openharness-video-service"})
        provider = TracerProvider(resource=resource)
        if endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )
        trace.set_tracer_provider(provider)

        if app is not None:
            FastAPIInstrumentor.instrument_app(app)
        CeleryInstrumentor().instrument()
        # These instrumentations are global; guard each independently.
        for instrument in (SQLAlchemyInstrumentor, RedisInstrumentor, Boto3Instrumentor):
            try:
                instrument().instrument()
            except Exception:
                pass
        return True
    except Exception:
        return False

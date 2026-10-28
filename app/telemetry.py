"""OpenTelemetry tracing. W3C traceparent is the default propagator.

Spans are exported only when OTEL_EXPORTER_OTLP_ENDPOINT is set; otherwise incoming
traceparent headers are still honoured and trace ids appear in the logs.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_configured = False


def setup_telemetry(app: FastAPI) -> None:
    global _configured
    if not _configured:
        provider = TracerProvider(
            resource=Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "home-energy")})
        )
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument()
        _configured = True
    # Health probes are not traced.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="livez,readyz")

"""
OpenTelemetry distributed tracing.

Optional. In Autonomous-SWE-Agent every turn, tool call and sandbox operation
opened a span; those call sites were removed with that project's loop and
tools. What remains is the setup: point OTEL_EXPORTER_OTLP_ENDPOINT at a
collector and spans opened with `get_tracer(__name__)` are exported. The OTLP
exporter is imported only when an endpoint is configured, so the package
imports without it.

Usage:
    from observability.tracing import get_tracer, setup_tracing

    # Call once at startup:
    setup_tracing(service_name="swe-agent")

    # In any module:
    tracer = get_tracer(__name__)
    with tracer.start_as_current_span("my.operation") as span:
        span.set_attribute("key", "value")
        ...
"""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
)

_tracer_provider: TracerProvider | None = None


def setup_tracing(
    service_name: str = "swe-agent",
    otlp_endpoint: str | None = None,
    console_fallback: bool = True,
) -> TracerProvider:
    """
    Initialise the global OpenTelemetry tracer provider.

    Sends spans to an OTLP endpoint (Jaeger, Tempo, etc.) if configured.
    Falls back to console output if no endpoint is set and console_fallback=True.

    Call this once at application startup before any modules use get_tracer().
    """
    global _tracer_provider

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    endpoint = otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        otlp_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    elif console_fallback:
        # Lightweight: only print spans that have errors, to avoid log spam
        pass  # ConsoleSpanExporter is too noisy for production; skip unless debugging

    _tracer_provider = provider
    trace.set_tracer_provider(provider)
    return provider


def get_tracer(name: str) -> trace.Tracer:
    """
    Get a named tracer. Safe to call before setup_tracing() — returns a
    no-op tracer in that case (traces are silently dropped).
    """
    if _tracer_provider is None:
        # Auto-initialise with no-op exporter on first use
        setup_tracing(console_fallback=False)
    return trace.get_tracer(name)


def shutdown_tracing() -> None:
    """Flush all pending spans and shut down. Call at application exit."""
    global _tracer_provider
    if _tracer_provider:
        _tracer_provider.shutdown()
        _tracer_provider = None

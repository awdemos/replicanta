"""OpenTelemetry tracing bootstrap for Replicanta.

Tracing is controlled through the standard OTel environment variables:

    OTEL_TRACES_EXPORTER=none            # none (default), console, or otlp
    OTEL_SERVICE_NAME=replicanta
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
    OTEL_EXPORTER_OTLP_HEADERS=key=value,key2=value2
    OTEL_RESOURCE_ATTRIBUTES=env=dev,host=foo

The module is safe to import anywhere; it does not initialise a provider until
``init_telemetry()`` is called. After init, ``get_tracer(name)`` and the
``@span`` decorator can be used at the application seams.
"""

import atexit
import functools
import logging
import os
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace.status import Status, StatusCode

from replicanta import __version__

get_current_span = trace.get_current_span

__all__ = [
    "Status",
    "StatusCode",
    "get_current_span",
    "get_tracer",
    "init_telemetry",
    "span",
]

logger = logging.getLogger(__name__)

_provider: TracerProvider | None = None
_enabled = False


def _parse_headers(raw: str) -> dict[str, str]:
    headers = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            logger.warning("ignoring malformed OTLP header %r", part)
            continue
        key, value = part.split("=", 1)
        headers[key.strip()] = value.strip()
    return headers


def _resource_attributes() -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    raw = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            logger.warning("ignoring malformed resource attribute %r", part)
            continue
        key, value = part.split("=", 1)
        attrs[key.strip()] = value.strip()
    return attrs


def init_telemetry(service_name: str = "replicanta") -> bool:
    """Initialise the global TracerProvider.

    Returns True when tracing is active (exporter is not ``none``). Safe to call
    multiple times; subsequent calls are no-ops.
    """
    global _provider, _enabled

    if _provider is not None:
        return _enabled

    exporter_name = os.environ.get("OTEL_TRACES_EXPORTER", "none").lower()
    if exporter_name == "none":
        _provider = TracerProvider(resource=Resource.create({}))
        trace.set_tracer_provider(_provider)
        _enabled = False
        return _enabled

    resource_attrs = _resource_attributes()
    resource_attrs[SERVICE_NAME] = os.environ.get("OTEL_SERVICE_NAME", service_name)
    resource_attrs[SERVICE_VERSION] = __version__

    _provider = TracerProvider(resource=Resource.create(resource_attrs))

    if exporter_name == "otlp":
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        headers = _parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""))
        exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
    elif exporter_name == "console":
        exporter = ConsoleSpanExporter()
    else:
        logger.warning("unknown OTEL_TRACES_EXPORTER=%r, falling back to console", exporter_name)
        exporter = ConsoleSpanExporter()

    if exporter_name == "otlp":
        _provider.add_span_processor(BatchSpanProcessor(exporter))
    else:
        # Console output is for local debugging: synchronous export avoids a
        # background thread trying to flush to stdout after it has been closed.
        _provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(_provider)
    atexit.register(_provider.shutdown)
    _enabled = True
    logger.debug("telemetry initialised with exporter=%r", exporter_name)
    return _enabled


def get_tracer(name: str) -> trace.Tracer:
    """Return a tracer. Initialises telemetry with defaults if not already done."""
    if _provider is None:
        init_telemetry()
    return trace.get_tracer(name)


def span(name: str | None = None, **attrs):
    """Decorator that starts a span around the wrapped function.

    The span name defaults to ``module.qualname``. Static keyword arguments are
    set as span attributes. Exceptions are recorded and the span status is set
    to ERROR before being re-raised.
    """

    def decorator(func):
        span_name = name or f"{func.__module__}.{func.__qualname__}"
        tracer = get_tracer(func.__module__)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with tracer.start_as_current_span(span_name) as current_span:
                for key, value in attrs.items():
                    current_span.set_attribute(key, value)
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    current_span.record_exception(exc)
                    current_span.set_status(Status(StatusCode.ERROR, description=str(exc)))
                    raise

        return wrapper

    return decorator

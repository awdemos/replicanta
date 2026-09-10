"""Tests for the OpenTelemetry telemetry bootstrap."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from replicanta import telemetry


def _reset_telemetry(monkeypatch):
    """Clear module state and env vars so each test starts fresh."""
    provider = telemetry._provider
    if provider is not None:
        provider.shutdown()
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.setattr(telemetry, "_enabled", False)
    # Reset opentelemetry's global provider so tests can re-initialise it.
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    for key in (
        "OTEL_TRACES_EXPORTER",
        "OTEL_SERVICE_NAME",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_RESOURCE_ATTRIBUTES",
    ):
        monkeypatch.delenv(key, raising=False)


def test_init_returns_tracer(monkeypatch):
    _reset_telemetry(monkeypatch)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    tracer = telemetry.get_tracer("test")
    assert tracer is not None
    span = tracer.start_span("test_span")
    assert span is not None
    span.end()


def test_span_decorator_records_span(monkeypatch):
    _reset_telemetry(monkeypatch)
    exporter = InMemorySpanExporter()
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    telemetry.init_telemetry()
    provider = telemetry._provider
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry._enabled = True

    @telemetry.span("test.decorated")
    def work(x):
        telemetry.get_current_span().set_attribute("work.x", x)
        return x * 2

    assert work(21) == 42

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "test.decorated"
    assert span.attributes["work.x"] == 21


def test_span_decorator_records_exception(monkeypatch):
    _reset_telemetry(monkeypatch)
    exporter = InMemorySpanExporter()
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    telemetry.init_telemetry()
    provider = telemetry._provider
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry._enabled = True

    @telemetry.span("test.fails")
    def boom():
        raise ValueError("expected")

    with pytest.raises(ValueError, match="expected"):
        boom()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == telemetry.StatusCode.ERROR


def test_exporter_none_does_not_crash(monkeypatch):
    _reset_telemetry(monkeypatch)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    assert telemetry.init_telemetry() is False
    assert telemetry.get_tracer("test").start_span("noop").end() is None


def test_default_exporter_is_none(monkeypatch):
    _reset_telemetry(monkeypatch)
    assert telemetry.init_telemetry() is False
    assert telemetry._enabled is False
    _reset_telemetry(monkeypatch)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    first = telemetry.init_telemetry()
    second = telemetry.init_telemetry()
    assert first is second

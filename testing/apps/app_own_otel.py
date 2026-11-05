# SPDX-License-Identifier: Apache-2.0

"""Fixture workload with an OpenTelemetry pipeline of its own, started in code: SDK, OTLP exporter and FastAPI
instrumentation sending to the customer's own collector (OTEL_EXPORTER_OTLP_ENDPOINT)."""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)

from app_asgi import app  # noqa: E402

FastAPIInstrumentor.instrument_app(app)

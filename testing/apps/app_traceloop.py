# SPDX-License-Identifier: Apache-2.0

"""Fixture workload that starts Traceloop (OpenLLMetry) in code, exporting to the customer's own endpoint."""

import os

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from traceloop.sdk import Traceloop

Traceloop.init(
    app_name="support-assistant",
    disable_batch=True,
    telemetry_enabled=False,
    exporter=OTLPSpanExporter(endpoint=os.environ["THEIRS_URL"] + "/v1/traces"),
)

from app_asgi import app  # noqa: E402,F401

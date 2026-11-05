# SPDX-License-Identifier: Apache-2.0

"""OTLP/HTTP JSON helpers on the standard library: value encoding, resource from the environment, endpoint choice
and the POST. Shared by the span exporter and the setup report, so it must not import anything but the stdlib."""

from __future__ import annotations

import base64
import gzip
import http.client
import json
import math
import os
import platform
import ssl
import sys
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from impact_instrumentation._version import __version__
from impact_instrumentation.detect import KIT_ENDPOINT

USER_AGENT = f"impact-instrumentation/{__version__} python/{platform.python_version()}"
# What one attempt to reach the in-cluster gateway may take. The gateway answers in milliseconds when it works; when it
# does not (a NetworkPolicy dropping the packets, a wedged pod) every second here is a second a process holds on to
# its spans, and one the exit waits for.
KIT_TIMEOUT_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 10.0
_INT64 = 2**63


def any_value(value) -> dict:
    """An OTLP JSON AnyValue. 64-bit integers are decimal strings, as the OTLP JSON mapping requires."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)} if -_INT64 <= value < _INT64 else {"stringValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value} if math.isfinite(value) else {"stringValue": str(value)}
    if isinstance(value, bytes | bytearray):
        return {"bytesValue": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Mapping):
        return {"kvlistValue": {"values": key_values(value)}}
    if isinstance(value, Sequence):
        return {"arrayValue": {"values": [any_value(v) for v in value]}}
    return {"stringValue": str(value)}


def key_values(attributes: Mapping | None) -> list[dict]:
    return [{"key": str(k), "value": any_value(v)} for k, v in (attributes or {}).items()]


WORKLOAD_NAME_KEYS = (
    "k8s.deployment.name",
    "k8s.statefulset.name",
    "k8s.daemonset.name",
    "k8s.cronjob.name",
    "k8s.job.name",
    "k8s.pod.name",
)


def _environment_attributes(environ: Mapping[str, str]) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for pair in (environ.get("OTEL_RESOURCE_ATTRIBUTES") or "").split(","):
        key, sep, value = pair.partition("=")
        if sep and key.strip():
            attributes[key.strip()] = urllib.parse.unquote(value.strip())
    return attributes


def default_service_name(environ: Mapping[str, str]) -> str | None:
    """The name of the workload, when nothing names the service. None when OTEL_SERVICE_NAME or service.name is set.

    The kit's Instrumentation leaves OTEL_SERVICE_NAME out (another tracer in the workload would obey it), so the
    service is named after the workload the Operator described in OTEL_RESOURCE_ATTRIBUTES.
    """
    attributes = _environment_attributes(environ)
    if environ.get("OTEL_SERVICE_NAME") or attributes.get("service.name"):
        return None
    for key in WORKLOAD_NAME_KEYS:
        if attributes.get(key):
            return attributes[key]
    return None


def resource_attributes(environ: Mapping[str, str]) -> dict[str, str]:
    """The workload's resource as the OpenTelemetry SDK builds it from OTEL_RESOURCE_ATTRIBUTES / OTEL_SERVICE_NAME.

    The Operator fills these with the k8s.* placement attributes before the process starts.
    """
    attributes = _environment_attributes(environ)
    service = environ.get("OTEL_SERVICE_NAME")
    if service:
        attributes["service.name"] = service
    if not attributes.get("service.name"):
        attributes["service.name"] = default_service_name(environ) or (
            f"unknown_service:{os.path.basename(sys.executable) or 'python'}"
        )
    attributes["telemetry.sdk.language"] = "python"
    attributes["telemetry.distro.name"] = "impact-instrumentation"
    attributes["telemetry.distro.version"] = __version__
    return attributes


def in_kit(environ: Mapping[str, str]) -> bool:
    """True inside the cluster kit, where the package sends to the gateway named by IMPACT_OTLP_ENDPOINT."""
    return bool(environ.get(KIT_ENDPOINT))


def traces_endpoint(environ: Mapping[str, str]) -> str:
    kit = environ.get(KIT_ENDPOINT)
    if kit:
        return kit.rstrip("/") + "/v1/traces"
    specific = environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if specific:
        return specific
    general = environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    return (general.rstrip("/") if general else "http://localhost:4318") + "/v1/traces"


def exporter_headers(environ: Mapping[str, str]) -> dict[str, str]:
    """Standard OTEL_EXPORTER_OTLP_HEADERS, honoured outside the kit only (inside it they are the workload's own)."""
    if in_kit(environ):
        return {}
    raw = environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS") or environ.get("OTEL_EXPORTER_OTLP_HEADERS") or ""
    headers = {}
    for pair in raw.split(","):
        key, sep, value = pair.partition("=")
        if sep and key.strip():
            headers[key.strip()] = urllib.parse.unquote(value.strip())
    return headers


def timeout_seconds(environ: Mapping[str, str]) -> float:
    if in_kit(environ):
        return KIT_TIMEOUT_SECONDS
    raw = environ.get("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT") or environ.get("OTEL_EXPORTER_OTLP_TIMEOUT")
    try:
        return max(float(raw) / 1000.0, 0.1) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class Response:
    status: int
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def post(
    url: str,
    payload: dict,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = KIT_TIMEOUT_SECONDS,
    ca_file: str | None = None,
) -> Response:
    """POST the payload as gzip JSON. Raises OSError (including timeouts) when the endpoint cannot be reached.

    http.client is used directly on purpose: HTTP_PROXY and friends must not redirect traffic to the in-cluster gateway.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"unsupported OTLP endpoint: {url!r}")
    body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), compresslevel=5)
    request_headers = {
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
        "User-Agent": USER_AGENT,
        **(headers or {}),
    }
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(
            parts.hostname, parts.port, timeout=timeout, context=ssl.create_default_context(cafile=ca_file)
        )
    else:
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        path = parts.path or "/"
        connection.request("POST", path + (f"?{parts.query}" if parts.query else ""), body, request_headers)
        response = connection.getresponse()
        response.read()
        retry_after = response.getheader("Retry-After")
        try:
            delay = float(retry_after) if retry_after else None
        except ValueError:
            delay = None
        return Response(response.status, delay)
    finally:
        connection.close()

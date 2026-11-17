#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Turn the OTLP trace exports of a recording into the one OTLP/JSON request a copy carries to impact.ai.

    python3 otlp_json.py canonical.recording.json --source traceloop [--route copy] > interaction.otlp.json

The relay copies an accepted OTLP export unchanged and the gateway adds the source's provenance to every resource
(`impact.source.id`, `impact.source.route`). This does the same to a recording, so that the result is what arrives at
impact.ai's door: every OTLP/HTTP traces request of the recording (protobuf or JSON, gzip or not) is read, the resource
spans of all of them are joined into one ExportTraceServiceRequest in the OTLP/JSON encoding (ids in hex, 64-bit
integers as strings, enums as numbers), the provenance is added, and only the trace that holds the most spans is kept:
the interaction, not the set-up a recording app does before it (an index it fills, for example). Needs
opentelemetry-proto, which every OpenTelemetry Python exporter installs.
"""

from __future__ import annotations

import argparse
import base64
import collections
import gzip
import json
import sys
import zlib

TRACE_PATH_SUFFIXES = ("/v1/traces", "/otel/v1/traces", "/v2/trace/otlp")
ID_FIELDS = ("traceId", "spanId", "parentSpanId")


def header(headers: dict, name: str) -> str:
    return next((value for key, value in headers.items() if key.lower() == name.lower()), "")


def body_of(request: dict) -> bytes:
    body = base64.b64decode(request.get("bodyBase64") or "")
    encoding = (request.get("contentEncoding") or header(request.get("headers") or {}, "Content-Encoding")).lower()
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    if encoding not in ("", "identity"):
        raise SystemExit(f"cannot read content encoding {encoding!r}")
    return body


def hex_ids(value):
    """OTLP/JSON writes trace and span ids in hex; protobuf's JSON mapping writes bytes in base64, and leaves out the
    values of an empty array or map, which this writes as an empty list."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in ID_FIELDS and isinstance(item, str):
                out[key] = base64.b64decode(item).hex() if item else ""
            elif key in ("arrayValue", "kvlistValue") and isinstance(item, dict) and "values" not in item:
                out[key] = {**item, "values": []}
            else:
                out[key] = hex_ids(item)
        return out
    if isinstance(value, list):
        return [hex_ids(item) for item in value]
    return value


def export_of(request: dict) -> dict:
    """One traces request as an OTLP/JSON ExportTraceServiceRequest."""
    body = body_of(request)
    content_type = (request.get("contentType") or header(request.get("headers") or {}, "Content-Type")).lower()
    if content_type.startswith("application/json"):
        return json.loads(body)
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    message = ExportTraceServiceRequest()
    message.ParseFromString(body)
    return hex_ids(MessageToDict(message, use_integers_for_enums=True))


def is_traces(request: dict) -> bool:
    return request.get("method") == "POST" and request.get("path", "").endswith(TRACE_PATH_SUFFIXES)


def door(recording: dict, source: str, route: str) -> dict:
    resource_spans = []
    for request in recording["requests"]:
        if is_traces(request):
            resource_spans += export_of(request).get("resourceSpans") or []
    counts = collections.Counter(
        span["traceId"]
        for resource in resource_spans
        for scope in resource.get("scopeSpans") or []
        for span in scope.get("spans") or []
    )
    if not counts:
        raise SystemExit("the recording holds no spans")
    trace = counts.most_common(1)[0][0]
    kept = []
    for resource in resource_spans:
        scopes = []
        for scope in resource.get("scopeSpans") or []:
            spans = [span for span in scope.get("spans") or [] if span["traceId"] == trace]
            if spans:
                scopes.append({**scope, "spans": spans})
        if not scopes:
            continue
        attributes = [
            entry
            for entry in (resource.get("resource") or {}).get("attributes") or []
            if not entry["key"].startswith("impact.source.")
        ]
        attributes += [
            {"key": "impact.source.id", "value": {"stringValue": source}},
            {"key": "impact.source.route", "value": {"stringValue": route}},
        ]
        kept.append(
            {
                **resource,
                "resource": {**(resource.get("resource") or {}), "attributes": attributes},
                "scopeSpans": scopes,
            }
        )
    return {"resourceSpans": kept}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recording", help="a recording (testing/mock/recording.py), or - for standard input")
    parser.add_argument("--source", required=True, help="the source id the provenance names")
    parser.add_argument("--route", default="copy")
    args = parser.parse_args(argv)
    if args.recording == "-":
        recording = json.load(sys.stdin)
    else:
        with open(args.recording, encoding="utf-8") as file:
            recording = json.load(file)
    json.dump(door(recording, args.source, args.route), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
